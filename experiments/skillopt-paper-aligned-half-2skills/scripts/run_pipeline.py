#!/usr/bin/env python3
"""Run the corrected six-endpoint SearchQA pipeline unattended."""

from __future__ import annotations

import argparse
import fcntl
import importlib.metadata
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

import experiment
import run_asr_matrix
import run_matrix


PIPELINE_VERSION = f"{experiment.EXPERIMENT_ID}-pipeline-v5-searchqa-only"
EXPECTED_TRAJECTORIES = len(experiment.BENCHMARKS) * len(experiment.CONDITIONS)
EXPECTED_ASR_PROBES = len(experiment.BENCHMARKS) * len(experiment.ATTACKS) * 3
PIPELINE_STATE = experiment.EXPERIMENT_DIR / "records" / "pipeline-state.json"
WORK_STATE = experiment.RUN_ROOT.parent / "records" / "pipeline-state.json"
LOG_ROOT = experiment.RUN_ROOT.parent / "logs"
LOCK_PATH = experiment.RUN_ROOT.parent / ".pipeline.lock"
SERVER_SCRIPT = Path(__file__).with_name("serve_endpoint.py")
MATRIX_SCRIPT = Path(__file__).with_name("run_matrix.py")
ASR_MATRIX_SCRIPT = Path(__file__).with_name("run_asr_matrix.py")
SUMMARY_SCRIPT = Path(__file__).with_name("summarize.py")
SUMMARY_JSON = experiment.EXPERIMENT_DIR / "records" / "live-summary.json"


class PipelineError(RuntimeError):
    pass


def endpoint_ready(spec: experiment.EndpointSpec, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(
            spec.endpoint.rstrip("/") + "/models", timeout=timeout
        ) as response:
            value = json.loads(response.read().decode())
    except Exception:  # noqa: BLE001
        return False
    rows = value.get("data") if isinstance(value, dict) else None
    return spec.model_alias in {
        str(row.get("id")) for row in rows or [] if isinstance(row, dict)
    }


def expected_model_identity(spec: experiment.EndpointSpec) -> dict[str, Any]:
    return (
        experiment.optimizer_model_identity_contract()
        if spec.role == "optimizer"
        else experiment.target_model_identity_contract()
    )


def validate_endpoint_contract(spec: experiment.EndpointSpec) -> int:
    path = experiment.server_receipt_path(spec.port)
    if path.is_symlink() or not path.is_file():
        raise PipelineError(f"endpoint lacks launch receipt: {spec.endpoint}")
    receipt = experiment.read_json(path)
    expected = {
        "schema_version": 3,
        "experiment_id": experiment.EXPERIMENT_ID,
        "role": spec.role,
        "physical_gpu_ids": list(spec.gpu_ids),
        "port": spec.port,
        "endpoint": spec.endpoint,
        "model_identity": expected_model_identity(spec),
        "serving_contract": spec.serving_contract,
    }
    mismatches = [key for key, value in expected.items() if receipt.get(key) != value]
    if mismatches:
        raise PipelineError(f"endpoint receipt mismatch {spec.endpoint}: {mismatches}")
    pid = receipt.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise PipelineError(f"endpoint receipt has invalid pid: {spec.endpoint}")
    proc = Path("/proc") / str(pid)
    try:
        status = (proc / "status").read_text()
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            errors="replace"
        )
        environ = (proc / "environ").read_bytes().split(b"\0")
    except OSError as exc:
        raise PipelineError(f"endpoint pid is not live: {spec.endpoint}") from exc
    uid = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
    if not uid or int(uid.split()[1]) != os.getuid():
        raise PipelineError(f"endpoint process owner differs: {spec.endpoint}")
    chat_template_kwargs: dict[str, Any] = {"enable_thinking": True}
    if spec.serving_contract.get("reasoning_effort"):
        chat_template_kwargs["reasoning_effort"] = spec.serving_contract[
            "reasoning_effort"
        ]
    required = (
        str(spec.model_dir),
        "--served-model-name",
        spec.model_alias,
        "--port",
        str(spec.port),
        "--max-model-len",
        str(spec.serving_contract["max_model_len"]),
        json.dumps(chat_template_kwargs, separators=(",", ":")),
    )
    if any(value not in cmdline for value in required):
        raise PipelineError(f"endpoint command differs: {spec.endpoint}")
    cuda = f"CUDA_VISIBLE_DEVICES={','.join(map(str, spec.gpu_ids))}".encode()
    if cuda not in environ:
        raise PipelineError(f"endpoint GPU binding differs: {spec.endpoint}")
    if not endpoint_ready(spec):
        raise PipelineError(f"endpoint is not ready: {spec.endpoint}")
    return pid


def build_server_command(spec: experiment.EndpointSpec) -> list[str]:
    return [
        str(Path(spec.serving_contract["venv"]) / "bin" / "python"),
        str(SERVER_SCRIPT),
        "--role",
        spec.role,
        "--port",
        str(spec.port),
        "--receipt",
        str(experiment.server_receipt_path(spec.port)),
    ]


def build_matrix_command(run_ids: Sequence[str], timeout: float) -> list[str]:
    command = [
        str(experiment.SKILLOPT_PYTHON),
        str(MATRIX_SCRIPT),
        "--cell-timeout-seconds",
        str(timeout),
    ]
    for spec in experiment.TARGET_ENDPOINT_SPECS:
        command.extend(("--target-endpoint", spec.endpoint))
    for run_id in run_ids:
        command.extend(("--only", run_id))
    return command


def build_asr_command(run_ids: Sequence[str]) -> list[str]:
    command = [
        str(experiment.SKILLOPT_PYTHON),
        str(ASR_MATRIX_SCRIPT),
        "--workers",
        "4",
        "--sandbox-port-base",
        "19450",
        "--trial-timeout-seconds",
        "1800",
        "--max-attempts",
        "2",
    ]
    for spec in experiment.TARGET_ENDPOINT_SPECS:
        command.extend(("--endpoint", spec.endpoint))
    for run_id in run_ids:
        command.extend(("--run-id", run_id))
    return command


def qualification_path(spec: experiment.EndpointSpec) -> Path:
    return experiment.EXPERIMENT_DIR / "records" / f"qualification-{spec.port}.json"


def build_qualification_command(spec: experiment.EndpointSpec) -> list[str]:
    return [
        str(Path(spec.serving_contract["venv"]) / "bin" / "python"),
        str(Path(__file__).with_name("qualify_endpoint.py")),
        "--port",
        str(spec.port),
        "--output",
        str(qualification_path(spec)),
    ]


def static_preflight(index: dict[str, Any]) -> dict[str, Any]:
    if len(index["runs"]) != EXPECTED_TRAJECTORIES:
        raise PipelineError("run index is not the exact active-scope matrix")
    if importlib.metadata.version("json-repair") != "0.63.4":
        raise PipelineError("json-repair must be exactly 0.63.4")
    artifacts = [
        experiment.TARGET_MODEL_DIR / "config.json",
        experiment.TARGET_MODEL_DIR / "model.safetensors.index.json",
        experiment.OPTIMIZER_MODEL_DIR / "config.json",
        experiment.OPTIMIZER_MODEL_DIR / "model.safetensors.index.json",
        experiment.OPTIMIZER_MODEL_DIR / "crc32.txt",
        experiment.DATA_ROOT / "materialize_from_frozen_parent.py",
        experiment.DATA_ROOT / "materialization_audit.json",
        experiment.RUN_INDEX,
        experiment.CONTEXT_CAPACITY_TRANSITION_RECORD,
        experiment.SCOPE_REDUCTION_RECORD,
        (
            experiment.EXPERIMENT_DIR
            / "records/context-capacity-transition-20260827T143414Z/sha256sums.txt"
        ),
        *(
            experiment.OFFICIAL_ROOT / relative
            for relative in experiment.SKILLOPT_RUNTIME_RELATIVE_FILES
        ),
        *(experiment.config_path(name) for name in experiment.BENCHMARKS),
        *(experiment.split_manifest_path(name) for name in experiment.BENCHMARKS),
        *(Path(str(run["initial_path"])) for run in index["runs"]),
    ]
    records: list[dict[str, str]] = []
    for path in artifacts:
        if path.is_symlink() or not path.is_file():
            raise PipelineError(f"missing regular preflight artifact: {path}")
        records.append({"path": str(path.resolve()), "sha256": experiment.sha256_file(path)})
    for run in index["runs"]:
        if experiment.sha256_file(Path(run["initial_path"])) != run["initial_sha256"]:
            raise PipelineError(f"initial hash drift: {run['run_id']}")
    data_audit = experiment.read_json(experiment.DATA_ROOT / "materialization_audit.json")
    observed_counts = data_audit.get("counts")
    if (
        data_audit.get("status") != "passed"
        or observed_counts != experiment.MATERIALIZED_COUNTS_BY_BENCHMARK
    ):
        raise PipelineError("canonical data audit is not passed or has wrong counts")
    for benchmark in experiment.BENCHMARKS:
        if observed_counts.get(benchmark) != experiment.counts_for(benchmark):
            raise PipelineError(f"canonical data audit differs: {benchmark}")
    return {
        "schema_version": 2,
        "status": "static_ready",
        "created_at": experiment.utc_now(),
        "experiment_id": experiment.EXPERIMENT_ID,
        "run_index_sha256": experiment.sha256_file(experiment.RUN_INDEX),
        "trajectory_count": EXPECTED_TRAJECTORIES,
        "asr_probe_count": EXPECTED_ASR_PROBES,
        "json_repair_version": "0.63.4",
        "artifacts": records,
    }


def atomic_state(state: dict[str, Any]) -> None:
    state["updated_at"] = experiment.utc_now()
    experiment.atomic_write_json(PIPELINE_STATE, state)
    experiment.atomic_write_json(WORK_STATE, state)


class OwnedProcesses:
    def __init__(self) -> None:
        self.servers: list[subprocess.Popen[Any]] = []
        self.streams: list[Any] = []
        self.active_stage: subprocess.Popen[Any] | None = None

    @staticmethod
    def terminate(process: subprocess.Popen[Any], grace: float = 30.0) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def close(self) -> None:
        if self.active_stage is not None:
            self.terminate(self.active_stage)
        for process in reversed(self.servers):
            self.terminate(process)
        for stream in self.streams:
            stream.close()


def launch_endpoints(owner: OwnedProcesses, timeout: float) -> None:
    pending: list[tuple[experiment.EndpointSpec, subprocess.Popen[Any]]] = []
    for spec in experiment.ALL_ENDPOINT_SPECS:
        if endpoint_ready(spec):
            validate_endpoint_contract(spec)
            continue
        path = LOG_ROOT / "servers" / f"{spec.role}-{spec.port}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("ab")
        process = subprocess.Popen(
            build_server_command(spec),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        owner.servers.append(process)
        owner.streams.append(stream)
        pending.append((spec, process))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        failed = [(spec.port, proc.returncode) for spec, proc in pending if proc.poll() is not None]
        if failed:
            raise PipelineError(f"endpoint exited during startup: {failed}")
        if all(endpoint_ready(spec) for spec in experiment.ALL_ENDPOINT_SPECS):
            for spec in experiment.ALL_ENDPOINT_SPECS:
                validate_endpoint_contract(spec)
            return
        time.sleep(5)
    missing = [spec.endpoint for spec in experiment.ALL_ENDPOINT_SPECS if not endpoint_ready(spec)]
    raise PipelineError(f"endpoint startup timed out: {missing}")


def run_stage(
    name: str,
    command: Sequence[str],
    state: dict[str, Any],
    owner: OwnedProcesses,
    *,
    attempts: int,
    before_attempt: Callable[[], None] | None = None,
) -> int:
    path = LOG_ROOT / "pipeline" / f"{name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    returncode = 1
    for attempt in range(1, attempts + 1):
        if before_attempt:
            before_attempt()
        state["stages"][name] = {
            "status": "running",
            "attempt": attempt,
            "command": list(command),
            "started_at": experiment.utc_now(),
            "log": str(path),
        }
        atomic_state(state)
        with path.open("ab") as stream:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            owner.active_stage = process
            returncode = int(process.wait())
            owner.active_stage = None
        state["stages"][name].update(
            {
                "status": "completed" if returncode == 0 else "failed",
                "returncode": returncode,
                "finished_at": experiment.utc_now(),
            }
        )
        atomic_state(state)
        if returncode == 0:
            return 0
    return returncode


def dry_run(index: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    run_ids = [str(run["run_id"]) for run in index["runs"]]
    attacked = [str(run["run_id"]) for run in index["runs"] if run["attack"]]
    return {
        "schema_version": 2,
        "pipeline_version": PIPELINE_VERSION,
        "dry_run": True,
        "writes_performed": False,
        "servers": [
            {
                "role": spec.role,
                "gpu_ids": list(spec.gpu_ids),
                "endpoint": spec.endpoint,
                "command": build_server_command(spec),
            }
            for spec in experiment.ALL_ENDPOINT_SPECS
        ],
        "target_optimizer_routes": [
            {
                "target": spec.endpoint,
                "optimizer": experiment.optimizer_endpoint_for_target(spec.port),
            }
            for spec in experiment.TARGET_ENDPOINT_SPECS
        ],
        "stages": {
            "qualification-target": build_qualification_command(
                experiment.TARGET_ENDPOINT_SPECS[0]
            ),
            "qualification-optimizer": build_qualification_command(
                experiment.OPTIMIZER_ENDPOINT_SPECS[0]
            ),
            "trajectories": build_matrix_command(run_ids, args.cell_timeout_seconds),
            "asr": build_asr_command(attacked),
            "summary": [str(experiment.SKILLOPT_PYTHON), str(SUMMARY_SCRIPT)],
        },
        "trajectory_count": len(run_ids),
        "asr_probe_count": len(attacked) * 3,
    }


def execute(args: argparse.Namespace) -> dict[str, Any]:
    index = experiment.load_run_index()
    if args.dry_run:
        return dry_run(index, args)
    preflight = static_preflight(index)
    experiment.atomic_write_json(experiment.PREFLIGHT_RECEIPT, preflight)
    state = {
        "schema_version": 2,
        "pipeline_version": PIPELINE_VERSION,
        "experiment_id": experiment.EXPERIMENT_ID,
        "status": "running",
        "started_at": experiment.utc_now(),
        "stages": {},
        "incidents": [],
    }
    atomic_state(state)
    owner = OwnedProcesses()
    ensure = lambda: launch_endpoints(owner, args.endpoint_ready_timeout_seconds)  # noqa: E731
    try:
        ensure()
        qualifications: list[dict[str, Any]] = []
        for spec in (
            experiment.TARGET_ENDPOINT_SPECS[0],
            experiment.OPTIMIZER_ENDPOINT_SPECS[0],
        ):
            name = f"qualification-{spec.role}"
            code = run_stage(
                name,
                build_qualification_command(spec),
                state,
                owner,
                attempts=1,
                before_attempt=ensure,
            )
            if code:
                raise PipelineError(f"{name} failed")
            receipt = experiment.read_json(qualification_path(spec))
            if receipt.get("status") != "passed" or receipt.get("role") != spec.role:
                raise PipelineError(f"{name} receipt failed validation")
            qualifications.append(receipt)
        preflight.update(
            {
                "status": "ready",
                "endpoint_receipts": [
                    {
                        "endpoint": spec.endpoint,
                        "pid": validate_endpoint_contract(spec),
                        "path": str(experiment.server_receipt_path(spec.port)),
                        "sha256": experiment.sha256_file(
                            experiment.server_receipt_path(spec.port)
                        ),
                    }
                    for spec in experiment.ALL_ENDPOINT_SPECS
                ],
                "qualifications": qualifications,
            }
        )
        experiment.atomic_write_json(experiment.PREFLIGHT_RECEIPT, preflight)

        runs = list(index["runs"])
        trajectory_code = run_stage(
            "trajectories",
            build_matrix_command(
                [str(run["run_id"]) for run in runs], args.cell_timeout_seconds
            ),
            state,
            owner,
            attempts=args.stage_attempts,
            before_attempt=ensure,
        )
        completed = [run for run in runs if run_matrix.validate_completed(run)]
        attacked = [run for run in completed if run["attack"]]
        asr_code: int | None = None
        if attacked:
            asr_code = run_stage(
                "asr",
                build_asr_command([str(run["run_id"]) for run in attacked]),
                state,
                owner,
                attempts=args.stage_attempts,
                before_attempt=ensure,
            )
        summary_code = run_stage(
            "summary",
            [str(experiment.SKILLOPT_PYTHON), str(SUMMARY_SCRIPT)],
            state,
            owner,
            attempts=1,
        )
        state["completed_trajectories"] = len(completed)
        state["stage_returncodes"] = {
            "trajectories": trajectory_code,
            "asr": asr_code,
            "summary": summary_code,
        }
        state["status"] = (
            "completed"
            if len(completed) == EXPECTED_TRAJECTORIES
            and trajectory_code == 0
            and asr_code == 0
            and summary_code == 0
            else "completed_partial"
        )
        state["finished_at"] = experiment.utc_now()
        atomic_state(state)
        return state
    except Exception as exc:
        state["status"] = "failed"
        state["finished_at"] = experiment.utc_now()
        state["incidents"].append(
            {"at": experiment.utc_now(), "message": f"{type(exc).__name__}: {exc}"}
        )
        atomic_state(state)
        raise
    finally:
        owner.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cell-timeout-seconds", type=float, default=48 * 60 * 60)
    parser.add_argument("--endpoint-ready-timeout-seconds", type=float, default=30 * 60)
    parser.add_argument("--stage-attempts", type=int, default=2)
    args = parser.parse_args(argv)
    if args.cell_timeout_seconds <= 0 or args.endpoint_ready_timeout_seconds <= 0:
        parser.error("timeouts must be positive")
    if not 1 <= args.stage_attempts <= 3:
        parser.error("--stage-attempts must be in [1, 3]")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        print(json.dumps(execute(args), ensure_ascii=False, indent=2))
        return 0
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit(f"another pipeline owns {LOCK_PATH}") from exc
        result = execute(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
