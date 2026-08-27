#!/usr/bin/env python3
"""Run the frozen SkillOpt matrix with one sequential queue per endpoint."""

from __future__ import annotations

import argparse
import csv
import fcntl
import fnmatch
import importlib.metadata
import json
import os
import queue
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from artifact_validation import (
    ArtifactValidationError,
    build_completion_receipt,
    validate_completion_receipt,
)
import experiment
from experiment import (
    ATTACKS,
    BENCHMARKS,
    DATA_ROOT,
    EXPERIMENT_ID,
    MODEL_ALIAS,
    MODEL_CONFIG_SHA256,
    MODEL_DIR,
    MODEL_INDEX_SHA256,
    OFFICIAL_ROOT,
    PIPELINE_STATE,
    RUN_ROOT,
    RUNTIME_STATE,
    SERVER_PORTS,
    SERVING_CONTRACT,
    SKILLOPT_PYTHON,
    atomic_write_json,
    completion_config_contract,
    config_path,
    counts_for,
    endpoint_url,
    load_run_index,
    model_identity_contract,
    output_root,
    read_json,
    sha256_file,
    server_receipt_path,
    stable_launch_contract,
    utc_now,
)


STATE_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()
ERROR_LOCK = threading.Lock()
ATTACK_PRIORITY = {attack: index + 2 for index, attack in enumerate(ATTACKS)}
CELL_TIMEOUT_ENV = "SKILLOPT_CELL_TIMEOUT_SECONDS"
DEFAULT_CELL_TIMEOUT_SECONDS = 48 * 60 * 60
TERMINATION_GRACE_SECONDS = 30.0
SIGNAL_TERMINATION_GRACE_SECONDS = 20.0
STOP_POLL_SECONDS = 1.0


class TrainerRegistry:
    """Track trainer process groups so matrix signals cannot orphan them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[Any]] = {}

    def register(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes[process.pid] = process

    def discard(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes.pop(process.pid, None)

    def signal_all(self, signum: int) -> None:
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass


def install_stop_handlers(
    registry: TrainerRegistry, stop_requested: threading.Event
) -> tuple[dict[str, int | None], dict[signal.Signals, Any]]:
    received: dict[str, int | None] = {"signum": None}

    def request_stop(signum: int, _frame: Any) -> None:
        if received["signum"] is None:
            received["signum"] = signum
        stop_requested.set()
        registry.signal_all(signal.SIGTERM)

    previous: dict[signal.Signals, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, request_stop)
    return received, previous


def restore_signal_handlers(previous: dict[signal.Signals, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def endpoint_ready(endpoint: str, expected_alias: str | None = None) -> bool:
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/models", timeout=10) as response:
            value = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return (expected_alias or MODEL_ALIAS) in {
        str(row.get("id")) for row in value.get("data", []) if isinstance(row, dict)
    }


def validate_endpoint_receipt(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port not in SERVER_PORTS:
        raise ValueError(f"endpoint is outside the frozen local port set: {endpoint}")
    port = int(parsed.port)
    spec = experiment.endpoint_spec(port)
    path = server_receipt_path(port)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"endpoint launch receipt is missing: {path}")
    receipt = read_json(path)
    expected = {
        "schema_version": 3,
        "experiment_id": EXPERIMENT_ID,
        "role": spec.role,
        "physical_gpu_ids": list(spec.gpu_ids),
        "port": port,
        "endpoint": spec.endpoint,
        "model_identity": (
            experiment.optimizer_model_identity_contract()
            if spec.role == "optimizer"
            else experiment.target_model_identity_contract()
        ),
    }
    mismatches = [key for key, value in expected.items() if receipt.get(key) != value]
    if mismatches:
        raise ValueError(f"endpoint launch receipt differs: {endpoint}: {mismatches}")
    pid = receipt.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValueError(f"endpoint launch receipt has invalid pid: {endpoint}")
    try:
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ")
        environ = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    except OSError as exc:
        raise ValueError(f"endpoint launch pid is not live: {endpoint}") from exc
    serving = receipt.get("serving_contract")
    if not isinstance(serving, dict) or serving.get("enable_thinking") is not True:
        raise ValueError(f"endpoint receipt does not enable thinking: {endpoint}")
    if serving.get("max_model_len") != spec.serving_contract["max_model_len"]:
        raise ValueError(f"endpoint context length is outside contract: {endpoint}")
    required = (
        str(spec.model_dir).encode(),
        str(port).encode(),
        str(serving["max_model_len"]).encode(),
    )
    if any(value not in cmdline for value in required):
        raise ValueError(f"endpoint launch pid command differs: {endpoint}")
    expected_cuda = ",".join(str(gpu) for gpu in spec.gpu_ids)
    if f"CUDA_VISIBLE_DEVICES={expected_cuda}".encode() not in environ:
        raise ValueError(f"endpoint launch pid is bound to the wrong GPU: {endpoint}")


def validate_split_artifacts(benchmark: str, split_root: Path | None = None) -> None:
    """Validate the exact files declared by one frozen split manifest."""
    root = (split_root or DATA_ROOT / benchmark).resolve()
    manifest_path = root / "split_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"missing regular split manifest: {manifest_path}")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"split manifest is not an object: {manifest_path}")
    counts = counts_for(benchmark)
    if manifest.get("counts") != counts:
        raise ValueError(f"split manifest counts differ for {benchmark}")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError(f"split manifest has no output descriptors: {manifest_path}")

    for split, expected_count in counts.items():
        descriptor = outputs.get(split)
        if not isinstance(descriptor, dict):
            raise ValueError(f"missing output descriptor for {benchmark}/{split}")
        raw_path = descriptor.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(f"invalid output path for {benchmark}/{split}")
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError(f"output path is not absolute for {benchmark}/{split}: {path}")
        if path.resolve().parent != (root / split).resolve() or path.name not in {
            "items.json",
            "items.csv",
        }:
            raise ValueError(f"output path escapes the frozen split for {benchmark}/{split}: {path}")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing regular frozen data artifact: {path}")
        if descriptor.get("count") != expected_count:
            raise ValueError(f"manifest output count differs for {benchmark}/{split}")
        if descriptor.get("bytes") != path.stat().st_size:
            raise ValueError(f"manifest output byte count differs for {benchmark}/{split}")
        if descriptor.get("sha256") != sha256_file(path):
            raise ValueError(f"manifest output hash differs for {benchmark}/{split}")

        if path.suffix == ".json":
            rows = read_json(path)
            if not isinstance(rows, list):
                raise ValueError(f"frozen JSON split is not a list: {path}")
            actual_count = len(rows)
        elif path.suffix == ".csv":
            with path.open(encoding="utf-8", newline="") as stream:
                actual_count = sum(1 for _ in csv.DictReader(stream))
        else:  # The filename allow-list above makes this defensive.
            raise ValueError(f"unsupported frozen split format: {path}")
        if actual_count != expected_count:
            raise ValueError(
                f"frozen row count differs for {benchmark}/{split}: "
                f"{actual_count} != {expected_count}"
            )


def require_preflight(routes: list[tuple[str, str]]) -> None:
    """Fail before GPU work if any frozen local input has drifted."""
    target_endpoints = sorted({target for target, _ in routes})
    optimizer_endpoints = sorted({optimizer for _, optimizer in routes})
    unavailable = [
        endpoint
        for endpoint in target_endpoints
        if not endpoint_ready(endpoint, experiment.TARGET_MODEL_ALIAS)
    ] + [
        endpoint
        for endpoint in optimizer_endpoints
        if not endpoint_ready(endpoint, experiment.OPTIMIZER_MODEL_ALIAS)
    ]
    if unavailable:
        raise SystemExit(f"endpoints not ready for {MODEL_ALIAS}: {unavailable}")
    for endpoint in (*target_endpoints, *optimizer_endpoints):
        try:
            validate_endpoint_receipt(endpoint)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"invalid endpoint contract: {exc}") from exc
    model_files = {
        experiment.TARGET_MODEL_DIR / "config.json": experiment.TARGET_MODEL_CONFIG_SHA256,
        experiment.TARGET_MODEL_DIR / "model.safetensors.index.json": experiment.TARGET_MODEL_INDEX_SHA256,
        experiment.OPTIMIZER_MODEL_DIR / "config.json": experiment.OPTIMIZER_MODEL_CONFIG_SHA256,
        experiment.OPTIMIZER_MODEL_DIR / "model.safetensors.index.json": experiment.OPTIMIZER_MODEL_INDEX_SHA256,
        experiment.OPTIMIZER_MODEL_DIR / "crc32.txt": experiment.OPTIMIZER_MODEL_CRC_SHA256,
    }
    for path, expected in model_files.items():
        if not path.is_file() or sha256_file(path) != expected:
            raise SystemExit(f"frozen model identity mismatch: {path}")
    if importlib.metadata.version("json-repair") != "0.63.4":
        raise SystemExit("json-repair must be exactly 0.63.4 in the SkillOpt environment")
    for benchmark in BENCHMARKS:
        if not config_path(benchmark).is_file():
            raise SystemExit(f"missing config: {benchmark}")
        try:
            validate_split_artifacts(benchmark)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"invalid frozen data for {benchmark}: {exc}") from exc
    for run in load_run_index()["runs"]:
        initial = Path(str(run["initial_path"]))
        if (
            not initial.is_file()
            or initial.is_symlink()
            or sha256_file(initial) != run["initial_sha256"]
        ):
            raise SystemExit(f"frozen initial mismatch: {run['run_id']}")


def live_trainer_pids(run: dict[str, Any]) -> list[int]:
    expected_root = str(output_root(run))
    matches: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if "scripts/train.py" in command and f"env.out_root={expected_root}" in command:
            matches.append(int(entry.name))
    return sorted(matches)


def optimizer_for_target_endpoint(target_endpoint: str) -> str:
    parsed = urlparse(target_endpoint)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError(f"invalid target endpoint: {target_endpoint}")
    return experiment.optimizer_endpoint_for_target(parsed.port)


def build_command(
    run: dict[str, Any], target_endpoint: str, optimizer_endpoint: str
) -> list[str]:
    root = output_root(run)
    return [
        str(SKILLOPT_PYTHON),
        str(OFFICIAL_ROOT / "scripts" / "train.py"),
        "--config",
        str(config_path(str(run["benchmark"]))),
        "--cfg-options",
        f"env.skill_init={run['initial_path']}",
        f"env.out_root={root}",
        f"model.qwen_chat_base_url={target_endpoint}",
        f"model.optimizer_qwen_chat_base_url={optimizer_endpoint}",
        f"model.target_qwen_chat_base_url={target_endpoint}",
    ]


def validate_completed(run: dict[str, Any]) -> bool:
    root = output_root(run)
    receipt_path = root / "completion-receipt.json"
    if not receipt_path.is_file():
        return False
    try:
        receipt = read_json(receipt_path)
        validate_completion_receipt(
            receipt,
            run,
            stable_launch_contract(run),
            expected_config=completion_config_contract(str(run["benchmark"])),
        )
    except (OSError, json.JSONDecodeError, ArtifactValidationError, ValueError):
        return False
    return True


def seal_completed(run: dict[str, Any]) -> dict[str, Any]:
    path = output_root(run) / "completion-receipt.json"
    if path.is_file():
        return validate_completion_receipt(
            read_json(path),
            run,
            stable_launch_contract(run),
            expected_config=completion_config_contract(str(run["benchmark"])),
        )
    receipt = build_completion_receipt(
        run,
        stable_launch_contract(run),
        expected_config=completion_config_contract(str(run["benchmark"])),
    )
    atomic_write_json(path, receipt)
    return validate_completion_receipt(
        read_json(path),
        run,
        stable_launch_contract(run),
        expected_config=completion_config_contract(str(run["benchmark"])),
    )


def load_state() -> dict[str, Any]:
    pipeline_started_at: str | None = None
    if PIPELINE_STATE.is_file():
        try:
            pipeline = read_json(PIPELINE_STATE)
            if isinstance(pipeline, dict) and isinstance(
                pipeline.get("started_at"), str
            ):
                pipeline_started_at = pipeline["started_at"]
        except (OSError, json.JSONDecodeError):
            pass
    if RUNTIME_STATE.is_file():
        try:
            value = read_json(RUNTIME_STATE)
            if (
                isinstance(value, dict)
                and isinstance(value.get("runs"), dict)
                and value.get("pipeline_started_at") == pipeline_started_at
            ):
                return value
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "schema_version": 2,
        "created_at": utc_now(),
        "pipeline_started_at": pipeline_started_at,
        "runs": {},
    }


def update_state(state: dict[str, Any], run_id: str, **fields: Any) -> None:
    with STATE_LOCK:
        record = state["runs"].setdefault(run_id, {})
        record.update(fields)
        state["updated_at"] = utc_now()
        atomic_write_json(RUNTIME_STATE, state)


def ensure_launch_receipt(run: dict[str, Any]) -> Path:
    root = output_root(run)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "launch-contract.json"
    expected = stable_launch_contract(run)
    if path.exists():
        observed = read_json(path)
        if observed != expected:
            raise ValueError(f"{run['run_id']}: existing launch contract differs")
    else:
        atomic_write_json(path, expected)
    initial = root / "skills" / "skill_v0000.md"
    if initial.exists() and sha256_file(initial) != run["initial_sha256"]:
        raise ValueError(f"{run['run_id']}: existing R0 has the wrong hash")
    return path


def terminate_process_group(
    process: subprocess.Popen[Any], grace_seconds: float = TERMINATION_GRACE_SECONDS
) -> int:
    """Stop a trainer and any subprocesses it created."""
    return_code = process.poll()
    if return_code is not None:
        return int(return_code)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return int(process.wait(timeout=grace_seconds))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return int(process.wait())


def wait_for_trainer(
    process: subprocess.Popen[Any],
    timeout_seconds: float,
    stop_requested: threading.Event | None,
    termination_grace_seconds: float,
) -> tuple[int, bool]:
    """Wait for one trainer with a hard deadline and responsive matrix shutdown."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if stop_requested is not None and stop_requested.is_set():
            signal_grace = min(
                termination_grace_seconds, SIGNAL_TERMINATION_GRACE_SECONDS
            )
            return terminate_process_group(process, signal_grace), False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return terminate_process_group(process, termination_grace_seconds), True
        try:
            return int(process.wait(timeout=min(STOP_POLL_SECONDS, remaining))), False
        except subprocess.TimeoutExpired:
            continue


def record_internal_failure(
    run: dict[str, Any], endpoint: str, state: dict[str, Any], exc: Exception
) -> None:
    """Persist a worker failure as an attempt so the outer retry budget advances."""
    failed_at = utc_now()
    error = f"{type(exc).__name__}: {exc}"
    root = output_root(run)
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        root / "last-attempt.json",
        {
            "started_at": failed_at,
            "finished_at": failed_at,
            "endpoint": endpoint,
            "pid": None,
            "command": None,
            "returncode": None,
            "status": "failed_internal",
            "error": error,
        },
    )
    update_state(
        state,
        str(run["run_id"]),
        status="failed_internal",
        endpoint=endpoint,
        pid=None,
        finished_at=failed_at,
        output_root=str(root),
        error=error,
    )


def run_one(
    run: dict[str, Any],
    endpoint: str,
    state: dict[str, Any],
    *,
    cell_timeout_seconds: float = DEFAULT_CELL_TIMEOUT_SECONDS,
    termination_grace_seconds: float = TERMINATION_GRACE_SECONDS,
    trainer_registry: TrainerRegistry | None = None,
    stop_requested: threading.Event | None = None,
) -> str:
    run_id = str(run["run_id"])
    root = output_root(run)
    live_pids = live_trainer_pids(run)
    if live_pids:
        update_state(
            state,
            run_id,
            status="blocked_live_trainer",
            pids=live_pids,
            output_root=str(root),
        )
        return "blocked_live_trainer"
    if (root / "runtime_state.json").is_file():
        update_state(state, run_id, status="resuming_native_runtime_state")
    try:
        ensure_launch_receipt(run)
    except Exception as exc:  # noqa: BLE001
        update_state(state, run_id, status="failed_contract", error=str(exc))
        return "failed_contract"
    optimizer_endpoint = optimizer_for_target_endpoint(endpoint)
    command = build_command(run, endpoint, optimizer_endpoint)
    log_path = root / "run.log"
    attempt_path = root / "last-attempt.json"
    started = utc_now()
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"\n===== launch {started} target={endpoint} "
            f"optimizer={optimizer_endpoint} =====\n"
        )
        log.flush()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            command,
            cwd=OFFICIAL_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        if trainer_registry is not None:
            trainer_registry.register(process)
        try:
            attempt = {
                "started_at": started,
                "endpoint": endpoint,
                "optimizer_endpoint": optimizer_endpoint,
                "pid": process.pid,
                "command": command,
                "timeout_seconds": cell_timeout_seconds,
            }
            atomic_write_json(attempt_path, attempt)
            update_state(
                state,
                run_id,
                status="running",
                benchmark=run["benchmark"],
                attack=run.get("attack"),
                endpoint=endpoint,
                optimizer_endpoint=optimizer_endpoint,
                pid=process.pid,
                started_at=started,
                output_root=str(root),
                log=str(log_path),
                timeout_seconds=cell_timeout_seconds,
            )
            with PRINT_LOCK:
                print(
                    f"START {run_id} target={endpoint} optimizer={optimizer_endpoint} "
                    f"pid={process.pid}",
                    flush=True,
                )
            return_code, timed_out = wait_for_trainer(
                process,
                cell_timeout_seconds,
                stop_requested,
                termination_grace_seconds,
            )
        except BaseException:
            if process.poll() is None:
                terminate_process_group(process, termination_grace_seconds)
            raise
        finally:
            if trainer_registry is not None:
                trainer_registry.discard(process)

    finished = utc_now()
    attempt.update(
        {
            "finished_at": finished,
            "returncode": return_code,
            "status": "failed_timeout" if timed_out else "finished",
            "timed_out": timed_out,
        }
    )
    atomic_write_json(attempt_path, attempt)
    seal_error: str | None = None
    complete = False
    if return_code == 0 and not timed_out:
        try:
            seal_completed(run)
            complete = validate_completed(run)
        except (OSError, json.JSONDecodeError, ArtifactValidationError, ValueError) as exc:
            seal_error = str(exc)
    terminal_status = "completed" if complete else "failed_timeout" if timed_out else "failed"
    update_state(
        state,
        run_id,
        status=terminal_status,
        finished_at=finished,
        returncode=return_code,
        pid=None,
        timed_out=timed_out,
        timeout_seconds=cell_timeout_seconds,
        **({"error": seal_error} if seal_error else {}),
    )
    with PRINT_LOCK:
        print(
            f"{'DONE' if complete else 'TIMEOUT' if timed_out else 'FAIL'} "
            f"{run_id} returncode={return_code}",
            flush=True,
        )
    return terminal_status


def priority(run: dict[str, Any]) -> tuple[int, int, int]:
    benchmark = str(run["benchmark"])
    attack = run.get("attack")
    treatment_rank = 0 if attack is None else ATTACK_PRIORITY.get(str(attack), 99)
    return (
        treatment_rank,
        BENCHMARKS.index(benchmark),
        ATTACKS.index(attack) if attack else -1,
    )


def selected_runs(
    runs: list[dict[str, Any]], patterns: list[str], pilot: bool, max_jobs: int
) -> list[dict[str, Any]]:
    if pilot:
        patterns = [
            "searchqa__clean__q35t_q38o__seed_42",
            "searchqa__db_delete__q35t_q38o__seed_42",
        ]
    unmatched = [
        pattern
        for pattern in patterns
        if not any(fnmatch.fnmatch(str(run["run_id"]), pattern) for run in runs)
    ]
    if unmatched:
        raise ValueError(f"run-id patterns matched no runs: {unmatched}")
    selected = [
        run
        for run in runs
        if not patterns or any(fnmatch.fnmatch(str(run["run_id"]), pattern) for pattern in patterns)
    ]
    if not selected:
        raise ValueError("run selection is empty")
    selected.sort(key=priority)
    if max_jobs:
        selected = selected[:max_jobs]
    return selected


def positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def target_endpoint_for_run(run: dict[str, Any], endpoints: list[str]) -> str:
    if not endpoints:
        raise ValueError("target endpoint list is empty")
    condition = str(run.get("attack") or "clean")
    try:
        slot = experiment.CONDITIONS.index(condition)
    except ValueError as exc:
        raise ValueError(f"unknown condition: {condition}") from exc
    return endpoints[slot % len(endpoints)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target-endpoint",
        action="append",
        default=[],
        help="Qwen3.5 target /v1 endpoint; repeat for four fixed queues",
    )
    parser.add_argument("--only", action="append", default=[], help="run-id glob")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--max-jobs", type=int, default=0)
    parser.add_argument(
        "--cell-timeout-seconds",
        type=positive_seconds,
        default=os.environ.get(CELL_TIMEOUT_ENV, str(DEFAULT_CELL_TIMEOUT_SECONDS)),
        help=(
            "hard timeout for one SkillOpt trajectory; defaults to 48 hours and can also "
            f"be set with {CELL_TIMEOUT_ENV}"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.max_jobs < 0:
        parser.error("--max-jobs must be non-negative")

    endpoints = args.target_endpoint or [
        endpoint_url(port) for port in experiment.TARGET_PORTS
    ]
    index = load_run_index()
    try:
        runs = selected_runs(index["runs"], args.only, args.pilot, args.max_jobs)
    except ValueError as exc:
        parser.error(str(exc))

    if args.dry_run:
        rows = []
        for run in runs:
            endpoint = target_endpoint_for_run(run, endpoints)
            optimizer_endpoint = optimizer_for_target_endpoint(endpoint)
            rows.append(
                {
                    "run_id": run["run_id"],
                    "status": "already_completed" if validate_completed(run) else "pending",
                    "output_root": str(output_root(run)),
                    "target_endpoint": endpoint,
                    "optimizer_endpoint": optimizer_endpoint,
                    "command": build_command(run, endpoint, optimizer_endpoint),
                }
            )
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    unavailable = [
        endpoint
        for endpoint in endpoints
        if not endpoint_ready(endpoint, experiment.TARGET_MODEL_ALIAS)
    ]
    if unavailable:
        raise SystemExit(f"endpoints not ready for {MODEL_ALIAS}: {unavailable}")
    routes = [
        (endpoint, optimizer_for_target_endpoint(endpoint)) for endpoint in endpoints
    ]
    require_preflight(routes)

    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    lock_stream = (RUN_ROOT / ".matrix.lock").open("w")
    try:
        fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit("another matrix runner holds the experiment lock") from exc

    state = load_state()
    pending: list[dict[str, Any]] = []
    for run in runs:
        run_id = str(run["run_id"])
        if validate_completed(run):
            update_state(
                state,
                run_id,
                status="completed",
                output_root=str(output_root(run)),
            )
        elif (output_root(run) / "summary.json").is_file():
            try:
                seal_completed(run)
            except (OSError, json.JSONDecodeError, ArtifactValidationError, ValueError) as exc:
                update_state(
                    state,
                    run_id,
                    status="invalid_terminal_output",
                    error=str(exc),
                    output_root=str(output_root(run)),
                )
                pending.append(run)
            else:
                update_state(
                    state,
                    run_id,
                    status="completed",
                    output_root=str(output_root(run)),
                )
        else:
            pending.append(run)

    work_by_endpoint: dict[str, queue.Queue[dict[str, Any]]] = {
        endpoint: queue.Queue() for endpoint in endpoints
    }
    for run in pending:
        work_by_endpoint[target_endpoint_for_run(run, endpoints)].put(run)
    errors: list[dict[str, str]] = []
    trainer_registry = TrainerRegistry()
    stop_requested = threading.Event()
    received_signal, previous_signal_handlers = install_stop_handlers(
        trainer_registry, stop_requested
    )

    def worker(endpoint: str) -> None:
        work = work_by_endpoint[endpoint]
        while not stop_requested.is_set():
            try:
                run = work.get_nowait()
            except queue.Empty:
                return
            try:
                if stop_requested.is_set():
                    work.put(run)
                    return
                optimizer_endpoint = optimizer_for_target_endpoint(endpoint)
                if not endpoint_ready(
                    endpoint, experiment.TARGET_MODEL_ALIAS
                ) or not endpoint_ready(
                    optimizer_endpoint, experiment.OPTIMIZER_MODEL_ALIAS
                ):
                    update_state(
                        state,
                        str(run["run_id"]),
                        status="pending_endpoint_retry",
                        endpoint=endpoint,
                    )
                    work.put(run)
                    with ERROR_LOCK:
                        errors.append(
                            {
                                "run_id": str(run["run_id"]),
                                "status": "endpoint_unavailable",
                                "endpoint": endpoint,
                            }
                        )
                    return
                internal_error: str | None = None
                record_error: str | None = None
                try:
                    status = run_one(
                        run,
                        endpoint,
                        state,
                        cell_timeout_seconds=args.cell_timeout_seconds,
                        trainer_registry=trainer_registry,
                        stop_requested=stop_requested,
                    )
                except Exception as exc:  # noqa: BLE001
                    status = "failed_internal"
                    internal_error = f"{type(exc).__name__}: {exc}"
                    try:
                        record_internal_failure(run, endpoint, state, exc)
                    except Exception as record_exc:  # noqa: BLE001
                        record_error = f"{type(record_exc).__name__}: {record_exc}"
                    with PRINT_LOCK:
                        print(
                            f"INTERNAL_FAIL {run['run_id']} error={internal_error}"
                            + (f" record_error={record_error}" if record_error else ""),
                            flush=True,
                        )
                if status != "completed":
                    with ERROR_LOCK:
                        error_row = {"run_id": str(run["run_id"]), "status": status}
                        if status == "failed_internal":
                            error_row["error"] = internal_error or "unknown internal failure"
                            if record_error:
                                error_row["record_error"] = record_error
                        errors.append(error_row)
                    # A failed cell consumes this invocation's one bounded attempt.
                    # Keep draining independent cells; the pipeline retries the stage.
            finally:
                work.task_done()

    threads = [threading.Thread(target=worker, args=(endpoint,)) for endpoint in endpoints]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        restore_signal_handlers(previous_signal_handlers)
    queued = sum(work.qsize() for work in work_by_endpoint.values())
    if received_signal["signum"] is not None:
        with PRINT_LOCK:
            print(
                json.dumps(
                    {
                        "status": "interrupted",
                        "signal": received_signal["signum"],
                        "queued": queued,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        return 128 + int(received_signal["signum"])
    if queued or errors:
        with PRINT_LOCK:
            print(
                json.dumps(
                    {"status": "incomplete", "queued": queued, "errors": errors},
                    sort_keys=True,
                ),
                flush=True,
            )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
