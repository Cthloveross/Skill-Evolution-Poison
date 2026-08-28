#!/usr/bin/env python3
"""Resume a trainer if its final-test acceleration coordinator disappears."""

from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def process_exists(pid: int) -> bool:
    return (Path("/proc") / str(pid)).is_dir()


def process_state(pid: int) -> str | None:
    try:
        lines = (Path("/proc") / str(pid) / "status").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("State:"):
            return line.split(":", 1)[1].strip()
    return None


def timestamp_age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        observed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())


def validate_trainer(pid: int, run_root: Path) -> None:
    try:
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError as exc:
        raise RuntimeError(f"trainer PID {pid} is unavailable") from exc
    text = cmdline.replace(b"\0", b" ").decode(errors="replace")
    if "scripts/train.py" not in text or str(run_root.resolve()) not in text:
        raise RuntimeError(f"PID {pid} is not the expected trainer")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinator-pid", type=int, required=True)
    parser.add_argument("--trainer-pid", type=int, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--stale-heartbeat-seconds", type=float, default=300.0)
    parser.add_argument("--max-trainer-pause-seconds", type=float, default=900.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_root = args.run_root.resolve(strict=True)
    state_path = run_root / "acceleration" / "watchdog-state.json"
    coordinator_state_path = run_root / "acceleration" / "coordinator-state.json"
    validate_trainer(args.trainer_pid, run_root)
    state: dict[str, Any] = {
        "schema_version": 1,
        "kind": "skillopt_acceleration_trainer_watchdog",
        "status": "watching",
        "started_at": utc_now(),
        "coordinator_pid": args.coordinator_pid,
        "trainer_pid": args.trainer_pid,
        "run_root": str(run_root),
    }
    atomic_write_json(state_path, state)

    forced_resume_reason: str | None = None
    while process_exists(args.coordinator_pid):
        trainer_state = process_state(args.trainer_pid)
        if trainer_state is not None and trainer_state.startswith("T"):
            try:
                coordinator_state = json.loads(coordinator_state_path.read_text())
            except (OSError, json.JSONDecodeError):
                coordinator_state = {}
            heartbeat_age = timestamp_age_seconds(
                coordinator_state.get("last_checked_at")
            )
            pause_age = timestamp_age_seconds(
                coordinator_state.get("trainer_paused_at")
            )
            if heartbeat_age is not None and heartbeat_age > args.stale_heartbeat_seconds:
                forced_resume_reason = (
                    f"coordinator heartbeat stale for {heartbeat_age:.1f}s"
                )
            elif pause_age is not None and pause_age > args.max_trainer_pause_seconds:
                forced_resume_reason = f"trainer paused for {pause_age:.1f}s"
            if forced_resume_reason:
                validate_trainer(args.trainer_pid, run_root)
                os.kill(args.trainer_pid, signal.SIGCONT)
                state["forced_resume_reason"] = forced_resume_reason
                state["trainer_resumed_at"] = utc_now()
                state["status"] = "forced_resume_requested"
                atomic_write_json(state_path, state)
                try:
                    os.kill(args.coordinator_pid, signal.SIGTERM)
                    state["coordinator_sigterm_sent_at"] = utc_now()
                except ProcessLookupError:
                    pass
                time.sleep(1)
                state["trainer_state_after_action"] = process_state(args.trainer_pid)
                state["status"] = "forced_resume_completed"
                state["completed_at"] = utc_now()
                atomic_write_json(state_path, state)
                return 0
        time.sleep(args.poll_seconds)

    state["coordinator_disappeared_at"] = utc_now()
    before = process_state(args.trainer_pid)
    state["trainer_state_before_action"] = before
    if before is None:
        state["status"] = "trainer_already_exited"
    elif before.startswith("T"):
        validate_trainer(args.trainer_pid, run_root)
        os.kill(args.trainer_pid, signal.SIGCONT)
        time.sleep(1)
        after = process_state(args.trainer_pid)
        state["trainer_state_after_action"] = after
        if after is not None and after.startswith("T"):
            state["status"] = "resume_failed"
            atomic_write_json(state_path, state)
            raise RuntimeError(f"trainer {args.trainer_pid} remained stopped")
        state["status"] = "trainer_resumed"
        state["trainer_resumed_at"] = utc_now()
    else:
        state["status"] = "no_resume_needed"
    state["completed_at"] = utc_now()
    atomic_write_json(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
