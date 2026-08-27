#!/usr/bin/env python3
"""Run or inspect the active R0/Rbest/Rfinal ASR matrix."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

import experiment
import run_matrix
import run_asr_probe as probe


MATRIX_ID = "self-evolution-searchqa-3attacks-asr-9-v1"
CHECKPOINTS = ("initial", "best", "final")
TERMINAL_STATUSES = frozenset(
    {"valid", "indeterminate_fail_closed", "unobservable"}
)
DEFAULT_STATE = experiment.RUN_ROOT.parent / "records" / "asr-matrix-state.json"
DEFAULT_LOG_ROOT = experiment.RUN_ROOT.parent / "logs" / "asr"
DEFAULT_ARCHIVE_ROOT = experiment.RUN_ROOT.parent / "_asr-attempt-archive"
DEFAULT_BRIDGE_ROOT = Path("/tmp/skillopt-self-evolution-asr-bridges")
BRIDGE_SCRIPT = (
    experiment.EXPERIMENT_DIR
    / "asr-runtime"
    / "scripts"
    / "support"
    / "model_socket_bridge.py"
)
AF_UNIX_PATH_MAX_BYTES = 107


class MatrixError(RuntimeError):
    """The matrix cannot continue without weakening its evidence contract."""


@dataclass(frozen=True)
class ProbeTask:
    run_id: str
    benchmark: str
    attack: str
    checkpoint: str

    @property
    def key(self) -> str:
        return f"{self.run_id}::{self.checkpoint}"


@dataclass(frozen=True)
class Inspection:
    disposition: str
    reason: str
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class WorkerSpec:
    slot: int
    endpoint: str
    sandbox_port: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


MATRIX_SCRIPT_SHA256 = sha256_file(Path(__file__).resolve())


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_endpoint(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise argparse.ArgumentTypeError(
            "endpoint must be an explicit loopback HTTP URL ending in /v1"
        )
    return f"http://127.0.0.1:{parsed.port}/v1"


def endpoint_port(endpoint: str) -> int:
    port = urlparse(endpoint).port
    if port is None:
        raise MatrixError(f"endpoint has no TCP port: {endpoint}")
    return port


def check_endpoint(endpoint: str, timeout: float = 10.0) -> None:
    try:
        with urllib.request.urlopen(
            endpoint.rstrip("/") + "/models", timeout=timeout
        ) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ) as exc:
        raise MatrixError(f"endpoint is unavailable: {endpoint}: {exc}") from exc
    rows = value.get("data") if isinstance(value, dict) else None
    aliases = {
        str(row.get("id"))
        for row in rows or []
        if isinstance(row, dict) and row.get("id") is not None
    }
    if experiment.MODEL_ALIAS not in aliases:
        raise MatrixError(
            f"endpoint {endpoint} serves {sorted(aliases)!r}, not {experiment.MODEL_ALIAS}"
        )


def ensure_secure_bridge_root(root: Path) -> None:
    if not root.is_absolute():
        raise MatrixError(f"ASR bridge root must be absolute: {root}")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        pass
    try:
        stat_result = root.lstat()
    except OSError as exc:
        raise MatrixError(f"cannot inspect ASR bridge root {root}: {exc}") from exc
    if root.is_symlink() or not root.is_dir():
        raise MatrixError(f"ASR bridge root is not a regular directory: {root}")
    if stat_result.st_uid != os.getuid():
        raise MatrixError(f"ASR bridge root is not owned by this user: {root}")
    try:
        os.chmod(root, 0o700)
    except OSError as exc:
        raise MatrixError(f"cannot secure ASR bridge root {root}: {exc}") from exc


def build_tasks(runs: Sequence[dict[str, Any]]) -> list[ProbeTask]:
    tasks = [
        ProbeTask(
            run_id=str(run["run_id"]),
            benchmark=str(run["benchmark"]),
            attack=str(run["attack"]),
            checkpoint=checkpoint,
        )
        for run in runs
        if run.get("benchmark") in experiment.BENCHMARKS
        and run.get("condition") == "attacked"
        and run.get("attack") in experiment.ATTACKS
        for checkpoint in CHECKPOINTS
    ]
    tasks.sort(
        key=lambda task: (
            task.benchmark,
            task.attack,
            CHECKPOINTS.index(task.checkpoint),
        )
    )
    expected = {
        (benchmark, attack, checkpoint)
        for benchmark in experiment.BENCHMARKS
        for attack in experiment.ATTACKS
        for checkpoint in CHECKPOINTS
    }
    observed = {(task.benchmark, task.attack, task.checkpoint) for task in tasks}
    expected_count = len(experiment.BENCHMARKS) * len(experiment.ATTACKS) * len(CHECKPOINTS)
    if (
        len(tasks) != expected_count
        or observed != expected
        or len({task.key for task in tasks}) != expected_count
    ):
        raise MatrixError("run index does not produce the exact active ASR matrix")
    return tasks


def task_run_map(runs: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(run["run_id"]): run for run in runs}


def probe_root(run: dict[str, Any], checkpoint: str) -> Path:
    return experiment.output_root(run).resolve() / "asr" / checkpoint


def inspect_task(task: ProbeTask, run: dict[str, Any]) -> Inspection:
    root = probe_root(run, task.checkpoint)
    result_path = root / "result.json"
    if not result_path.is_file() or result_path.is_symlink():
        return Inspection("missing", "formal result is absent")
    try:
        value = probe.read_json(result_path, "formal ASR result")
        if not isinstance(value, dict):
            raise probe.ProbeError("formal ASR result is not an object")
        if value.get("output_root") != str(root):
            raise probe.ProbeError("formal ASR output root differs from its matrix slot")
        probe.verify_runtime_provenance()
        source = probe.checkpoint_for(run, task.checkpoint)
        raw_value = value.get("raw_result_path")
        raw_path = Path(raw_value) if isinstance(raw_value, str) else None
        probe.validate_receipt_binding(
            value,
            run,
            source,
            raw_path,
            require_execution_provenance=True,
        )
        if value.get("status") not in TERMINAL_STATUSES:
            raise probe.ProbeError(f"unsupported formal status: {value.get('status')!r}")
    except (OSError, ValueError, json.JSONDecodeError, probe.ProbeError) as exc:
        return Inspection("invalid", f"formal receipt validation failed: {exc}")
    return Inspection(str(value["status"]), "deep receipt validation passed", value)


def archive_incomplete_probe(
    task: ProbeTask,
    run: dict[str, Any],
    archive_root: Path,
    attempt: int,
) -> Path | None:
    root = probe_root(run, task.checkpoint)
    if not root.exists():
        return None
    if root.is_symlink() or not root.is_dir():
        raise MatrixError(f"probe root is not a regular directory: {root}")
    destination = archive_root / task.run_id / task.checkpoint / f"attempt_{attempt:04d}"
    if destination.exists():
        raise MatrixError(f"probe archive already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root), str(destination))
    return destination


def build_probe_command(
    task: ProbeTask,
    *,
    endpoint: str,
    socket_path: Path,
    sandbox_port: int,
    trial_timeout_seconds: int,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("run_asr_probe.py")),
        "--run-id",
        task.run_id,
        "--checkpoint",
        task.checkpoint,
        "--endpoint",
        endpoint,
        "--matrix-script-sha256",
        MATRIX_SCRIPT_SHA256,
        "--sandbox-port",
        str(sandbox_port),
        "--trial-timeout-seconds",
        str(trial_timeout_seconds),
    ]
    if task.attack != "gpu_hijack":
        command.extend(("--host-unix-socket", str(socket_path)))
    return command


class Bridge:
    def __init__(self, spec: WorkerSpec, root: Path, log_root: Path):
        self.spec = spec
        self.root = root
        self.directory: Path | None = None
        self.socket_path = root / "not-started.sock"
        self.log_path = log_root / "bridges" / f"slot-{spec.slot}.log"
        self.process: subprocess.Popen[Any] | None = None
        self.log_stream: Any = None

    def __enter__(self) -> "Bridge":
        ensure_secure_bridge_root(self.root)
        self.directory = Path(
            tempfile.mkdtemp(prefix=f"s{self.spec.slot}-", dir=self.root)
        )
        self.socket_path = self.directory / "m.sock"
        try:
            socket_path_bytes = len(os.fsencode(str(self.socket_path)))
            if socket_path_bytes > AF_UNIX_PATH_MAX_BYTES:
                raise MatrixError(
                    "ASR bridge socket exceeds AF_UNIX path limit: "
                    f"{socket_path_bytes}>{AF_UNIX_PATH_MAX_BYTES}: {self.socket_path}"
                )
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_stream = self.log_path.open("ab")
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    str(BRIDGE_SCRIPT),
                    "listen-unix",
                    "--unix-socket",
                    str(self.socket_path),
                    "--connect-host",
                    "127.0.0.1",
                    "--connect-port",
                    str(endpoint_port(self.spec.endpoint)),
                    "--socket-mode",
                    "600",
                ],
                stdin=subprocess.DEVNULL,
                stdout=self.log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise MatrixError(
                        f"slot {self.spec.slot} bridge exited before becoming ready"
                    )
                if self.socket_path.is_socket():
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    client.settimeout(1.0)
                    try:
                        client.connect(str(self.socket_path))
                        return self
                    except OSError:
                        pass
                    finally:
                        client.close()
                time.sleep(0.1)
            raise MatrixError(f"slot {self.spec.slot} bridge did not become ready")
        except Exception:
            self.__exit__()
            raise

    def __exit__(self, *_args: object) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait()
        if self.log_stream is not None:
            self.log_stream.close()
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)


class State:
    def __init__(self, path: Path, task_count: int):
        self.path = path
        self.lock = threading.RLock()
        self.value: dict[str, Any] = {
            "schema_version": 1,
            "matrix_id": MATRIX_ID,
            "status": "running",
            "expected_probe_count": task_count,
            "started_at": utc_now(),
            "updated_at": utc_now(),
            "tasks": {},
        }
        if path.is_file():
            try:
                prior = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                prior = None
            if isinstance(prior, dict) and prior.get("matrix_id") == MATRIX_ID:
                self.value["started_at"] = prior.get("started_at", self.value["started_at"])
                if isinstance(prior.get("tasks"), dict):
                    self.value["tasks"] = prior["tasks"]

    def record(self, task: ProbeTask, inspection: Inspection, **extra: Any) -> None:
        with self.lock:
            result = inspection.result or {}
            result_path = (
                probe_root(extra.pop("run"), task.checkpoint) / "result.json"
                if "run" in extra
                else None
            )
            prior = self.value["tasks"].get(task.key)
            prior_fields = dict(prior) if isinstance(prior, dict) else {}
            self.value["tasks"][task.key] = {
                **prior_fields,
                "run_id": task.run_id,
                "benchmark": task.benchmark,
                "attack": task.attack,
                "checkpoint": task.checkpoint,
                "disposition": inspection.disposition,
                "reason": inspection.reason,
                "asr": result.get("asr"),
                "receipt_path": str(result_path) if result_path else None,
                "receipt_sha256": (
                    sha256_file(result_path) if result_path and result_path.is_file() else None
                ),
                "updated_at": utc_now(),
                **extra,
            }
            self.value["updated_at"] = utc_now()
            atomic_write_json(self.path, self.value)

    def prior_attempts(self, task: ProbeTask) -> int:
        with self.lock:
            row = self.value.get("tasks", {}).get(task.key)
            value = row.get("attempts", 0) if isinstance(row, dict) else 0
            return value if type(value) is int and value >= 0 else 0

    def finish(self, status: str) -> None:
        with self.lock:
            self.value["status"] = status
            self.value["finished_at"] = utc_now()
            self.value["updated_at"] = self.value["finished_at"]
            atomic_write_json(self.path, self.value)


def next_attempt_number(task: ProbeTask, archive_root: Path, state: State) -> int:
    prior = state.prior_attempts(task)
    directory = archive_root / task.run_id / task.checkpoint
    archived = 0
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise MatrixError(f"probe archive root is not a regular directory: {directory}")
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_dir():
                raise MatrixError(f"unexpected probe archive entry: {path}")
            prefix = "attempt_"
            suffix = path.name[len(prefix) :] if path.name.startswith(prefix) else ""
            if not suffix.isdigit() or int(suffix) <= 0:
                raise MatrixError(f"unexpected probe archive entry: {path}")
            archived = max(archived, int(suffix))
    return max(prior, archived) + 1


def run_one(
    task: ProbeTask,
    run: dict[str, Any],
    spec: WorkerSpec,
    bridge: Bridge,
    *,
    max_attempts: int,
    trial_timeout_seconds: int,
    archive_root: Path,
    log_root: Path,
    state: State,
    stop: threading.Event,
) -> Inspection:
    existing = inspect_task(task, run)
    if existing.disposition in TERMINAL_STATUSES:
        state.record(task, existing, run=run, resumed=True)
        return existing
    last = existing
    first_attempt = next_attempt_number(task, archive_root, state)
    last_attempt = first_attempt - 1
    for attempt_offset in range(max_attempts):
        attempt = first_attempt + attempt_offset
        last_attempt = attempt
        if stop.is_set():
            return Inspection("stopped", "matrix stop requested")
        archive = archive_incomplete_probe(task, run, archive_root, attempt)
        command = build_probe_command(
            task,
            endpoint=spec.endpoint,
            socket_path=bridge.socket_path,
            sandbox_port=spec.sandbox_port,
            trial_timeout_seconds=trial_timeout_seconds,
        )
        log_path = log_root / task.run_id / f"{task.checkpoint}-attempt-{attempt}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as stream:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        last = inspect_task(task, run)
        state.record(
            task,
            last,
            run=run,
            attempts=attempt,
            returncode=completed.returncode,
            archive_path=str(archive) if archive else None,
            log_path=str(log_path),
        )
        if last.disposition in TERMINAL_STATUSES:
            return last
    return Inspection(
        "infrastructure_failure",
        "no formal receipt after "
        f"{max_attempts} attempts in this invocation "
        f"(attempt IDs {first_attempt}-{last_attempt}): {last.reason}",
    )


def dry_run_plan(
    tasks: Sequence[ProbeTask],
    runs: dict[str, dict[str, Any]],
    *,
    selection: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for task in tasks:
        inspection = inspect_task(task, runs[task.run_id])
        counts[inspection.disposition] = counts.get(inspection.disposition, 0) + 1
        rows.append(
            {
                "key": task.key,
                "run_id": task.run_id,
                "benchmark": task.benchmark,
                "attack": task.attack,
                "checkpoint": task.checkpoint,
                "probe_root": str(probe_root(runs[task.run_id], task.checkpoint)),
                "disposition": inspection.disposition,
                "reason": inspection.reason,
            }
        )
    return {
        "schema_version": 1,
        "matrix_id": MATRIX_ID,
        "selection": selection,
        "dry_run": True,
        "writes_performed": False,
        "expected_probe_count": len(build_tasks(list(runs.values()))),
        "selected_probe_count": len(tasks),
        "dispositions": counts,
        "tasks": rows,
    }


def execute(args: argparse.Namespace) -> dict[str, Any]:
    run_index = experiment.load_run_index()
    runs = run_index["runs"]
    all_tasks = build_tasks(runs)
    by_id = task_run_map(runs)
    if args.pilot:
        selection = "pilot"
        tasks = [task for task in all_tasks if task.attack == "db_delete"]
    elif args.run_all:
        selection = "all"
        tasks = all_tasks
    else:
        selection = "run-id-filter"
        tasks = [task for task in all_tasks if task.run_id in args.run_id]
        unknown = set(args.run_id) - {task.run_id for task in all_tasks}
        if unknown:
            raise MatrixError(f"unknown or non-runnable attacked run IDs: {sorted(unknown)}")
    if args.dry_run:
        return dry_run_plan(tasks, by_id, selection=selection)
    if not args.endpoint:
        raise MatrixError("at least one --endpoint is required outside --dry-run")

    endpoints = list(dict.fromkeys(args.endpoint))
    workers = min(args.workers, len(endpoints), len(tasks))
    if workers <= 0:
        raise MatrixError("the selected matrix has no tasks")
    specs = [
        WorkerSpec(slot=index, endpoint=endpoints[index], sandbox_port=args.sandbox_port_base + index)
        for index in range(workers)
    ]
    if specs[-1].sandbox_port > 65535:
        raise MatrixError("sandbox port range exceeds 65535")
    for endpoint in endpoints[:workers]:
        check_endpoint(endpoint)
        run_matrix.validate_endpoint_receipt(endpoint)

    args.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_stream = args.lock_path.open("a+")
    try:
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MatrixError(f"another ASR matrix owns {args.lock_path}") from exc
        state = State(args.state_path, len(tasks))
        pending: queue.Queue[ProbeTask] = queue.Queue()
        for task in tasks:
            pending.put(task)
        results: dict[str, Inspection] = {}
        results_lock = threading.Lock()
        stop = threading.Event()

        def worker(spec: WorkerSpec) -> None:
            try:
                with Bridge(spec, args.bridge_root, args.log_root) as bridge:
                    while not stop.is_set():
                        try:
                            task = pending.get_nowait()
                        except queue.Empty:
                            return
                        try:
                            result = run_one(
                                task,
                                by_id[task.run_id],
                                spec,
                                bridge,
                                max_attempts=args.max_attempts,
                                trial_timeout_seconds=args.trial_timeout_seconds,
                                archive_root=args.archive_root,
                                log_root=args.log_root,
                                state=state,
                                stop=stop,
                            )
                            with results_lock:
                                results[task.key] = result
                        finally:
                            pending.task_done()
            except Exception as exc:  # noqa: BLE001
                stop.set()
                with results_lock:
                    results[f"worker-{spec.slot}"] = Inspection(
                        "worker_failure", f"{type(exc).__name__}: {exc}"
                    )

        threads = [threading.Thread(target=worker, args=(spec,)) for spec in specs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final = {task.key: inspect_task(task, by_id[task.run_id]) for task in tasks}
        for task in tasks:
            state.record(task, final[task.key], run=by_id[task.run_id], final=True)
        terminal = sum(item.disposition in TERMINAL_STATUSES for item in final.values())
        invalid = sum(item.disposition == "invalid" for item in final.values())
        formal_unknown = sum(
            item.disposition == "indeterminate_fail_closed" for item in final.values()
        )
        status = (
            "completed"
            if terminal == len(tasks) and formal_unknown == 0
            else "completed_with_formal_unknowns"
            if terminal == len(tasks)
            else "failed"
        )
        state.finish(status)
        return {
            "schema_version": 1,
            "matrix_id": MATRIX_ID,
            "selection": selection,
            "status": status,
            "expected_probe_count": len(all_tasks),
            "selected_probe_count": len(tasks),
            "terminal_receipts": terminal,
            "formal_unknowns": formal_unknown,
            "invalid_receipts": invalid,
            "state_path": str(args.state_path),
        }
    finally:
        lock_stream.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", action="append", type=normalize_endpoint, default=[])
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--sandbox-port-base", type=int, default=19300)
    parser.add_argument("--trial-timeout-seconds", type=int, default=1200)
    parser.add_argument("--max-attempts", type=int, default=3)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--pilot",
        action="store_true",
        help="run the active db_delete trajectories at all checkpoints",
    )
    selection.add_argument(
        "--all",
        dest="run_all",
        action="store_true",
        help="run the exact active-scope receipt matrix",
    )
    selection.add_argument(
        "--run-id",
        action="append",
        default=[],
        help="run all three checkpoints for one attacked runnable trajectory; repeatable",
    )
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    parser.add_argument("--bridge-root", type=Path, default=DEFAULT_BRIDGE_ROOT)
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=experiment.RUN_ROOT.parent / ".asr-matrix.lock",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= experiment.MAX_GPUS:
        parser.error(f"--workers must be in [1, {experiment.MAX_GPUS}]")
    if not 1024 <= args.sandbox_port_base <= 65535:
        parser.error("--sandbox-port-base must be in [1024, 65535]")
    if args.trial_timeout_seconds <= 0:
        parser.error("--trial-timeout-seconds must be positive")
    if args.max_attempts <= 0:
        parser.error("--max-attempts must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = execute(parse_args(argv))
    except (MatrixError, probe.ProbeError, ValueError) as exc:
        raise SystemExit(f"ASR matrix failed: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
