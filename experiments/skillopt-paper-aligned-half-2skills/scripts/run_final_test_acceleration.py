#!/usr/bin/env python3
"""Coordinate three isolated file_delete Rbest shards and install them safely."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINTS = (
    "http://127.0.0.1:19352/v1",
    "http://127.0.0.1:19355/v1",
    "http://127.0.0.1:19356/v1",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def count_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("rb") as stream:
        return sum(1 for line in stream if line.strip())


def find_trainer(run_root: Path) -> int | None:
    marker = str(run_root.resolve())
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            cmdline = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except (OSError, UnicodeDecodeError):
            continue
        if "scripts/train.py" in cmdline and marker in cmdline:
            return int(proc_dir.name)
    return None


def validate_trainer(pid: int, run_root: Path) -> None:
    try:
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError as exc:
        raise RuntimeError(f"trainer PID {pid} is no longer available") from exc
    text = cmdline.replace(b"\0", b" ").decode(errors="replace")
    if "scripts/train.py" not in text or str(run_root.resolve()) not in text:
        raise RuntimeError(f"PID {pid} is not the expected file_delete trainer")


def endpoint_process(endpoint: str) -> dict[str, Any]:
    port = endpoint.rstrip("/").split(":")[-1].split("/")[0]
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            raw_cmdline = (proc_dir / "cmdline").read_bytes()
            cmdline = raw_cmdline.replace(b"\0", b" ").decode()
        except (OSError, UnicodeDecodeError):
            continue
        if "vllm serve" not in cmdline or f"--port {port}" not in cmdline:
            continue
        gpu = None
        try:
            environ = (proc_dir / "environ").read_bytes().split(b"\0")
            for entry in environ:
                if entry.startswith(b"CUDA_VISIBLE_DEVICES="):
                    gpu = entry.split(b"=", 1)[1].decode(errors="replace")
                    break
        except OSError:
            pass
        return {
            "endpoint": endpoint,
            "pid": int(proc_dir.name),
            "cuda_visible_devices": gpu,
            "cmdline": cmdline,
        }
    raise RuntimeError(f"cannot bind endpoint {endpoint} to a live vLLM process")


def terminate_children(children: list[subprocess.Popen[bytes]]) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 30
    for child in children:
        if child.poll() is not None:
            continue
        try:
            child.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--endpoint", action="append", default=[])
    parser.add_argument("--pause-at-baseline-rows", type=int, default=625)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_root = args.run_root.resolve(strict=True)
    checkpoint = run_root / "best_skill.md"
    canonical = run_root / "test_eval"
    baseline_results = run_root / "test_eval_baseline" / "results.jsonl"
    acceleration_root = run_root / "acceleration"
    logs_root = acceleration_root / "logs"
    state_path = acceleration_root / "coordinator-state.json"
    logs_root.mkdir(parents=True, exist_ok=True)
    endpoints = tuple(args.endpoint) if args.endpoint else DEFAULT_ENDPOINTS
    if len(endpoints) != 3:
        raise RuntimeError("exactly three target endpoints are required")
    if canonical.exists():
        raise RuntimeError(f"canonical result directory already exists: {canonical}")

    shard_script = Path(__file__).with_name("run_parallel_test_shard.py")
    endpoint_receipts = [endpoint_process(endpoint) for endpoint in endpoints]
    trainer_pid = find_trainer(run_root)
    if trainer_pid is None:
        raise RuntimeError("cannot find the live file_delete trainer")
    validate_trainer(trainer_pid, run_root)

    state: dict[str, Any] = {
        "schema_version": 1,
        "kind": "skillopt_final_test_acceleration_coordinator",
        "status": "starting",
        "started_at": utc_now(),
        "run_root": str(run_root),
        "checkpoint": str(checkpoint),
        "canonical_destination": str(canonical),
        "trainer_pid": trainer_pid,
        "pause_at_baseline_rows": args.pause_at_baseline_rows,
        "endpoint_processes": endpoint_receipts,
        "shards": [],
    }
    atomic_write_json(state_path, state)

    children: list[subprocess.Popen[bytes]] = []
    log_streams: list[Any] = []
    trainer_paused = False

    def interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGHUP, interrupt)

    try:
        for index, endpoint in enumerate(endpoints):
            shard_dir = acceleration_root / "shards" / f"rbest-{index}-of-3"
            log_path = logs_root / f"rbest-{index}-of-3.log"
            command = [
                sys.executable,
                str(shard_script),
                "evaluate",
                "--run-root",
                str(run_root),
                "--checkpoint",
                str(checkpoint),
                "--endpoint",
                endpoint,
                "--output-dir",
                str(shard_dir),
                "--canonical-destination",
                str(canonical),
                "--shard-index",
                str(index),
                "--shard-count",
                "3",
            ]
            log_stream = log_path.open("ab", buffering=0)
            log_streams.append(log_stream)
            child = subprocess.Popen(
                command,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            children.append(child)
            state["shards"].append(
                {
                    "index": index,
                    "endpoint": endpoint,
                    "directory": str(shard_dir),
                    "log": str(log_path),
                    "pid": child.pid,
                    "command": command,
                }
            )
        state["status"] = "evaluating"
        atomic_write_json(state_path, state)

        while True:
            return_codes = [child.poll() for child in children]
            state["baseline_rows_observed"] = count_rows(baseline_results)
            state["shard_return_codes"] = return_codes
            state["last_checked_at"] = utc_now()

            failed = [code for code in return_codes if code not in (None, 0)]
            if failed:
                state["status"] = "failed"
                state["failure"] = f"shard process failed with return code {failed[0]}"
                atomic_write_json(state_path, state)
                raise RuntimeError(state["failure"])
            if all(code == 0 for code in return_codes):
                break
            if canonical.exists():
                state["status"] = "superseded_by_native_evaluation"
                atomic_write_json(state_path, state)
                raise RuntimeError("native trainer claimed canonical test_eval before shards finished")
            if (
                not trainer_paused
                and state["baseline_rows_observed"] >= args.pause_at_baseline_rows
            ):
                validate_trainer(trainer_pid, run_root)
                os.kill(trainer_pid, signal.SIGSTOP)
                trainer_paused = True
                state["trainer_paused_at"] = utc_now()
                state["status"] = "evaluating_with_trainer_paused"
            atomic_write_json(state_path, state)
            time.sleep(args.poll_seconds)

        if not trainer_paused:
            validate_trainer(trainer_pid, run_root)
            os.kill(trainer_pid, signal.SIGSTOP)
            trainer_paused = True
            state["trainer_paused_at"] = utc_now()

        merge_command = [
            sys.executable,
            str(shard_script),
            "merge",
            "--run-root",
            str(run_root),
            "--destination",
            str(canonical),
        ]
        for index in range(3):
            merge_command.extend(
                [
                    "--shard-dir",
                    str(acceleration_root / "shards" / f"rbest-{index}-of-3"),
                ]
            )
        merge_log_path = logs_root / "merge.log"
        state["status"] = "merging"
        state["merge_command"] = merge_command
        state["merge_log"] = str(merge_log_path)
        atomic_write_json(state_path, state)
        with merge_log_path.open("ab", buffering=0) as merge_log:
            subprocess.run(
                merge_command,
                stdout=merge_log,
                stderr=subprocess.STDOUT,
                check=True,
            )

        state["status"] = "completed"
        state["completed_at"] = utc_now()
        state["canonical_rows"] = count_rows(canonical / "results.jsonl")
        atomic_write_json(state_path, state)
        return 0
    except BaseException as exc:
        terminate_children(children)
        if state.get("status") not in {"failed", "superseded_by_native_evaluation"}:
            state["status"] = "failed"
        state["failure"] = f"{type(exc).__name__}: {exc}"
        state["failed_at"] = utc_now()
        atomic_write_json(state_path, state)
        raise
    finally:
        if trainer_paused:
            try:
                validate_trainer(trainer_pid, run_root)
                os.kill(trainer_pid, signal.SIGCONT)
                state["trainer_resumed_at"] = utc_now()
                atomic_write_json(state_path, state)
            except (OSError, RuntimeError):
                pass
        for stream in log_streams:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
