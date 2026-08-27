#!/usr/bin/env python3
"""Frozen contract for the corrected SearchQA SkillOpt experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPERIMENT_ID = "skillopt-paper-aligned-half-2skills"
EXPERIMENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = EXPERIMENT_DIR.parents[1]

OFFICIAL_ROOT = Path(
    "/work/tc442/skill-evolution-poison-data/skillopt-official/source/"
    "SkillOpt-9639719"
)
OFFICIAL_REVISION = "9639719632daecacd1baaa47fe781f3c0253600a"
SKILLOPT_RUNTIME_RELATIVE_FILES = ("skillopt/engine/trainer.py",)
SKILLOPT_PYTHON = Path("/work/tc442/venvs/skillopt-repro-9639719/bin/python")
JSON_REPAIR_MIN_VERSION = "0.61.0"

TARGET_MODEL_DIR = Path("/work/tc442/models/Qwen3.5-9B")
TARGET_MODEL_REPOSITORY = "Qwen/Qwen3.5-9B"
TARGET_MODEL_ALIAS = "Qwen3.5-9B"
TARGET_MODEL_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
TARGET_MODEL_CONFIG_SHA256 = (
    "d0883072e01861ed0b2d47be3c16c36a8e81c224c7ffaa310c6558fb3f932b05"
)
TARGET_MODEL_INDEX_SHA256 = (
    "26d3539b516be613f39563617cb9d33b3f83d401298125be392c80cefb8f7fe5"
)

OPTIMIZER_MODEL_DIR = Path("/work/tc442/models/Qwen3.8-27B-FP8")
OPTIMIZER_MODEL_REPOSITORY = "Qwen/Qwen3.8-27B-FP8"
OPTIMIZER_MODEL_ALIAS = "Qwen3.8-27B-FP8"
OPTIMIZER_MODEL_REVISION = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
OPTIMIZER_MODEL_CONFIG_SHA256 = (
    "74227dd615bf1ea975aa676bdf355a0379858c12f394b5365cd9dfa5fc2c70bc"
)
OPTIMIZER_MODEL_INDEX_SHA256 = (
    "f0838c766951bdfe76d6afbdb2771a8f67aaa2231dedb3d33cebd817729843a2"
)
OPTIMIZER_MODEL_CRC_SHA256 = (
    "5ccb0f7436ae756484561fe915780509845f903ea1780dbe9b0a3a7a0dd095ac"
)

# Compatibility names consumed by the target-only ASR evaluator.
MODEL_DIR = TARGET_MODEL_DIR
MODEL_REPOSITORY = TARGET_MODEL_REPOSITORY
MODEL_ALIAS = TARGET_MODEL_ALIAS
MODEL_REVISION = TARGET_MODEL_REVISION
MODEL_CONFIG_SHA256 = TARGET_MODEL_CONFIG_SHA256
MODEL_INDEX_SHA256 = TARGET_MODEL_INDEX_SHA256
MODEL_FILES_MANIFEST = EXPERIMENT_DIR / "records" / "target-model-files.json"
MODEL_FILES_MANIFEST_SHA256 = (
    "d11a821549bc3115648630fc64f35b5549334ddf1340f5bb3f260900328c9d4b"
)

OUTPUT_TOKEN_LIMIT = 16_384
TARGET_SERVING_CONTRACT: dict[str, Any] = {
    "role": "target",
    "venv": "/work/tc442/venvs/qwen38",
    "tensor_parallel_size": 1,
    "max_model_len": 65_536,
    "dtype": "float16",
    "gpu_memory_utilization": 0.95,
    "max_num_seqs": 4,
    "max_num_batched_tokens": 2048,
    "seed": 0,
    "enforce_eager": True,
    "disable_custom_all_reduce": True,
    "vllm_use_flashinfer_sampler": "0",
    "attention_backend": "TRITON_ATTN",
    "gdn_prefill_backend": "triton",
    "limit_mm_per_prompt": {"image": 1, "video": 0},
    "mm_processor_kwargs": {"max_pixels": 5_242_880},
    "language_model_only": False,
    "enable_thinking": True,
    "enable_auto_tool_choice": True,
    "tool_call_parser": "qwen3_coder",
    "reasoning_parser": "qwen3",
}
OPTIMIZER_SERVING_CONTRACT: dict[str, Any] = {
    "role": "optimizer",
    "venv": "/work/tc442/venvs/qwen38",
    "tensor_parallel_size": 2,
    "max_model_len": 131_072,
    "dtype": "float16",
    "gpu_memory_utilization": 0.90,
    "max_num_seqs": 2,
    "max_num_batched_tokens": 4096,
    "seed": 0,
    "enforce_eager": True,
    "disable_custom_all_reduce": True,
    "vllm_use_flashinfer_sampler": "0",
    "attention_backend": "TRITON_ATTN",
    "gdn_prefill_backend": "triton",
    "limit_mm_per_prompt": {"image": 0, "video": 0},
    "language_model_only": True,
    "enable_thinking": True,
    "reasoning_effort": "medium",
    "enable_auto_tool_choice": True,
    "tool_call_parser": "qwen3_coder",
    "reasoning_parser": "qwen3",
}
SERVING_CONTRACT = TARGET_SERVING_CONTRACT

SOURCE_DATA_ROOT = Path(
    "/work/tc442/skill-evolution-poison-data/skillopt-official/materialized/"
    "self_evolution_half_full_8gpu"
)
DATA_ROOT = Path(
    "/work/tc442/skill-evolution-poison-data/skillopt-official/materialized/"
    "skillopt_paper_aligned_half_2skills"
)
RUN_ROOT = Path(
    os.environ.get(
        "SKILLOPT_PAPER_ALIGNED_RUN_ROOT",
        "/work/tc442/skill-evolution-poison-runs/"
        "skillopt-paper-aligned-half-2skills/run-trajectories",
    )
)
RUN_INDEX = EXPERIMENT_DIR / "records" / "run-index.json"
RUNTIME_STATE = EXPERIMENT_DIR / "records" / "runtime-state.json"
PREFLIGHT_RECEIPT = EXPERIMENT_DIR / "records" / "preflight.json"
ENDPOINT_QUALIFICATION_RECEIPT = EXPERIMENT_DIR / "records" / "qualification-19351.json"
PIPELINE_STATE = EXPERIMENT_DIR / "records" / "pipeline-state.json"
CONTEXT_CAPACITY_TRANSITION_RECORD = (
    EXPERIMENT_DIR / "records" / "context-capacity-transition.json"
)
SCOPE_REDUCTION_RECORD = EXPERIMENT_DIR / "records" / "scope-reduction.json"
SERVER_ROOT = RUN_ROOT.parent / "servers"

OPTIMIZER_ENDPOINTS = (((0, 6), 19_380), ((5, 7), 19_381))
TARGET_ENDPOINTS = (((1,), 19_351), ((2,), 19_352), ((3,), 19_353), ((4,), 19_354))
OPTIMIZER_PORTS = tuple(port for _, port in OPTIMIZER_ENDPOINTS)
TARGET_PORTS = tuple(port for _, port in TARGET_ENDPOINTS)
SERVER_PORTS = (*OPTIMIZER_PORTS, *TARGET_PORTS)
TARGET_TO_OPTIMIZER_PORT = {
    19_351: 19_380,
    19_352: 19_380,
    19_353: 19_381,
    19_354: 19_381,
}
MAX_GPUS = 8

BENCHMARKS = ("searchqa",)
ATTACKS = ("db_delete", "file_delete", "mock_api")
CONDITIONS = ("clean", *ATTACKS)
MATERIALIZED_COUNTS_BY_BENCHMARK: dict[str, dict[str, int]] = {
    "searchqa": {"train": 200, "val": 100, "test": 700},
    "docvqa": {"train": 54, "val": 27, "test": 187},
}
COUNTS_BY_BENCHMARK = {
    benchmark: dict(MATERIALIZED_COUNTS_BY_BENCHMARK[benchmark])
    for benchmark in BENCHMARKS
}
DATA_ROOTS = {benchmark: DATA_ROOT / benchmark for benchmark in BENCHMARKS}
BATCH_SIZE_BY_BENCHMARK = {"searchqa": 40}
ACCUMULATION = 1
EPOCHS = 4
SEED = 42
GRADIENT_MINIBATCH_SIZE = 8
MERGE_BATCH_SIZE = 8
ANALYST_WORKERS = 1
ROLLOUT_WORKERS = 4
SLOW_UPDATE_SAMPLES = 20
SELECTION_NAMESPACE = "self-evolution-train100-batch20-val30-test100-v1"
STEPS_PER_EPOCH = {
    benchmark: math.ceil(
        counts["train"] / (BATCH_SIZE_BY_BENCHMARK[benchmark] * ACCUMULATION)
    )
    for benchmark, counts in COUNTS_BY_BENCHMARK.items()
}
TOTAL_STEPS = {
    benchmark: EPOCHS * STEPS_PER_EPOCH[benchmark] for benchmark in BENCHMARKS
}
RFINAL_RELATIVE_PATH = {
    benchmark: f"skills/skill_v{TOTAL_STEPS[benchmark]:04d}.md"
    for benchmark in BENCHMARKS
}

OFFICIAL_INITIAL_SHA256 = {
    "searchqa": "d3ed21de4a5216da7c3cd63acc2330dc78227524753c9d29573e6692f48d6709",
    "docvqa": "60fa9bae90590a50970c9d5d33d92eaa412bf1fa380bd00c8343430202795d97",
}
FROZEN_PAYLOAD_SHA256 = {
    "db_delete": "a61efce0dc4ee5b567d1e85220164b930365e2bf3d4a7e40c732bd07a33dda78",
    "file_delete": "cb54e36d6afff93e103f1eac4a8168059881b75ff9ca6b4b097011f749a8cbc2",
    "mock_api": "b59078db5728d9fb69b7c5914a687ad097c77572881f14a6e8b1e91aceab0778",
}
MATERIALIZED_INITIAL_SHA256: dict[str, dict[str, str]] = {
    "searchqa": {
        "clean": OFFICIAL_INITIAL_SHA256["searchqa"],
        "db_delete": "1632781e198655e89eac19ae9b2fcff08f684c91592a3fa9eac8dc02ddfe3c23",
        "file_delete": "02ca0a8f6fcdd580131480f02dbf27069e38bb7648f4171ea7c8dbacf68c4e82",
        "mock_api": "955c130d8ce5da776647d64b363a8556b2f6859869bce08684e4b8ffe030a645",
    },
    "docvqa": {
        "clean": OFFICIAL_INITIAL_SHA256["docvqa"],
        "db_delete": "a5c48ad4c2dbdb24e249a1f864418065c2693c4ee3c0a2093e2841cf91ebbbde",
        "file_delete": "8f1b79c8eabf567ffa447504cb978751e5ef48df3968cbf626c181eb6386e061",
        "mock_api": "c1bce6bff8ffdc7e4930627cbb4665c76fc5b19ad062038bc5673570855d0b3a",
    },
}


@dataclass(frozen=True)
class EndpointSpec:
    role: str
    gpu_ids: tuple[int, ...]
    port: int

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def model_alias(self) -> str:
        return OPTIMIZER_MODEL_ALIAS if self.role == "optimizer" else TARGET_MODEL_ALIAS

    @property
    def model_dir(self) -> Path:
        return OPTIMIZER_MODEL_DIR if self.role == "optimizer" else TARGET_MODEL_DIR

    @property
    def serving_contract(self) -> dict[str, Any]:
        return (
            OPTIMIZER_SERVING_CONTRACT
            if self.role == "optimizer"
            else TARGET_SERVING_CONTRACT
        )


OPTIMIZER_ENDPOINT_SPECS = tuple(
    EndpointSpec("optimizer", tuple(gpus), port)
    for gpus, port in OPTIMIZER_ENDPOINTS
)
TARGET_ENDPOINT_SPECS = tuple(
    EndpointSpec("target", tuple(gpus), port) for gpus, port in TARGET_ENDPOINTS
)
ALL_ENDPOINT_SPECS = (*OPTIMIZER_ENDPOINT_SPECS, *TARGET_ENDPOINT_SPECS)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(canonical_json(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def target_model_identity_contract() -> dict[str, Any]:
    return {
        "alias": TARGET_MODEL_ALIAS,
        "repository": TARGET_MODEL_REPOSITORY,
        "revision": TARGET_MODEL_REVISION,
        "config_sha256": TARGET_MODEL_CONFIG_SHA256,
        "weight_index_sha256": TARGET_MODEL_INDEX_SHA256,
    }


def optimizer_model_identity_contract() -> dict[str, Any]:
    return {
        "alias": OPTIMIZER_MODEL_ALIAS,
        "repository": OPTIMIZER_MODEL_REPOSITORY,
        "revision": OPTIMIZER_MODEL_REVISION,
        "config_sha256": OPTIMIZER_MODEL_CONFIG_SHA256,
        "weight_index_sha256": OPTIMIZER_MODEL_INDEX_SHA256,
        "crc_manifest_sha256": OPTIMIZER_MODEL_CRC_SHA256,
    }


def model_identity_contract() -> dict[str, Any]:
    return target_model_identity_contract()


def skillopt_runtime_contract() -> dict[str, Any]:
    files = []
    for relative in SKILLOPT_RUNTIME_RELATIVE_FILES:
        path = OFFICIAL_ROOT / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing regular SkillOpt runtime file: {path}")
        files.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "base_revision": OFFICIAL_REVISION,
        "runtime_files": files,
    }


def pre_amendment_skillopt_runtime_contract() -> dict[str, Any]:
    return {
        "base_revision": OFFICIAL_REVISION,
        "runtime_files": [
            {
                "path": "skillopt/engine/trainer.py",
                "sha256": (
                    "ff71a1480ee7c7c21a2574bb429eeb7d"
                    "c0c88da0ea018feb22da4fa00b663a7f"
                ),
            }
        ],
    }


def pre_amendment_optimizer_serving_contract() -> dict[str, Any]:
    contract = dict(OPTIMIZER_SERVING_CONTRACT)
    contract["max_model_len"] = 65_536
    return contract


def context_capacity_transition() -> dict[str, Any]:
    path = CONTEXT_CAPACITY_TRANSITION_RECORD
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing context-capacity transition record: {path}")
    value = read_json(path)
    if not isinstance(value, Mapping):
        raise ValueError("context-capacity transition record must be an object")
    transition = dict(value)
    expected_header = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "applied_before_resume",
    }
    if any(transition.get(key) != expected for key, expected in expected_header.items()):
        raise ValueError("context-capacity transition header differs")
    runtime = transition.get("skillopt_runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("transition lacks SkillOpt runtime provenance")
    before_hash = pre_amendment_skillopt_runtime_contract()["runtime_files"][0][
        "sha256"
    ]
    after_hash = skillopt_runtime_contract()["runtime_files"][0]["sha256"]
    if runtime.get("trainer_before_sha256") != before_hash:
        raise ValueError("transition has the wrong pre-amendment trainer hash")
    if runtime.get("trainer_after_sha256") != after_hash:
        raise ValueError("transition has the wrong resumed trainer hash")
    archive = transition.get("archive")
    if not isinstance(archive, Mapping):
        raise ValueError("transition lacks archive provenance")
    manifest = EXPERIMENT_DIR / str(archive.get("manifest_path") or "")
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("transition archive manifest is missing")
    if sha256_file(manifest) != archive.get("manifest_sha256"):
        raise ValueError("transition archive manifest hash differs")
    archived = transition.get("archived_launch_contracts")
    if not isinstance(archived, Mapping):
        raise ValueError("transition lacks archived launch contracts")
    for record in archived.values():
        if not isinstance(record, Mapping):
            raise ValueError("invalid archived launch-contract record")
        archived_path = EXPERIMENT_DIR / str(record.get("path") or "")
        if archived_path.is_symlink() or not archived_path.is_file():
            raise ValueError(f"archived launch contract is missing: {archived_path}")
        if sha256_file(archived_path) != record.get("sha256"):
            raise ValueError(f"archived launch contract hash differs: {archived_path}")
    return transition


def scope_reduction_contract() -> dict[str, str]:
    record = read_json(SCOPE_REDUCTION_RECORD)
    expected = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "status": "applied_before_resume",
    }
    if not isinstance(record, Mapping) or any(
        record.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("scope-reduction record header differs")
    active = record.get("active_scope")
    if not isinstance(active, Mapping) or active.get("benchmarks") != list(BENCHMARKS):
        raise ValueError("scope-reduction active benchmarks differ")
    if active.get("trajectory_count") != len(BENCHMARKS) * len(CONDITIONS):
        raise ValueError("scope-reduction trajectory count differs")
    if active.get("asr_probe_count") != len(BENCHMARKS) * len(ATTACKS) * 3:
        raise ValueError("scope-reduction ASR count differs")
    removed = record.get("removed_scope")
    if not isinstance(removed, Mapping) or removed.get("benchmark") != "docvqa":
        raise ValueError("scope-reduction removed benchmark differs")
    if removed.get("launch_status") != "never_launched":
        raise ValueError("scope-reduction does not prove DocVQA was unlaunched")
    archive = record.get("archive")
    if not isinstance(archive, Mapping):
        raise ValueError("scope-reduction archive is missing")
    manifest = EXPERIMENT_DIR / str(archive.get("manifest_path") or "")
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("scope-reduction archive manifest is missing")
    if sha256_file(manifest) != archive.get("manifest_sha256"):
        raise ValueError("scope-reduction archive manifest hash differs")
    superseded = record.get("superseded_run_index")
    if not isinstance(superseded, Mapping):
        raise ValueError("scope-reduction lacks the superseded run index")
    old_index = EXPERIMENT_DIR / str(superseded.get("path") or "")
    if old_index.is_symlink() or not old_index.is_file():
        raise ValueError("superseded run index is missing")
    if sha256_file(old_index) != superseded.get("sha256"):
        raise ValueError("superseded run-index hash differs")
    return {
        "record_path": str(SCOPE_REDUCTION_RECORD.resolve()),
        "record_sha256": sha256_file(SCOPE_REDUCTION_RECORD),
    }


def execution_phase_contract(run: Mapping[str, Any]) -> dict[str, Any]:
    transition = context_capacity_transition()
    run_key = str(run["run_id"])
    boundaries = transition.get("completed_step_boundaries")
    if not isinstance(boundaries, Mapping) or run_key not in boundaries:
        raise ValueError(f"transition lacks a boundary for {run_key}")
    boundary = int(boundaries[run_key])
    total_steps = TOTAL_STEPS[str(run["benchmark"])]
    if not 0 <= boundary < total_steps:
        raise ValueError(f"invalid transition boundary for {run_key}: {boundary}")

    phases: list[dict[str, Any]] = []
    if boundary:
        archived = transition["archived_launch_contracts"].get(run_key)
        if not isinstance(archived, Mapping):
            raise ValueError(f"transition lacks the old launch contract for {run_key}")
        phases.append(
            {
                "phase_id": "pre_context_amendment",
                "includes_r0": True,
                "first_step": 1,
                "last_step": boundary,
                "target_serving_contract": TARGET_SERVING_CONTRACT,
                "optimizer_serving_contract": (
                    pre_amendment_optimizer_serving_contract()
                ),
                "skillopt_runtime": pre_amendment_skillopt_runtime_contract(),
                "archived_launch_contract": dict(archived),
            }
        )
    phases.append(
        {
            "phase_id": "post_context_amendment",
            "includes_r0": boundary == 0,
            "first_step": boundary + 1,
            "last_step": total_steps,
            "target_serving_contract": TARGET_SERVING_CONTRACT,
            "optimizer_serving_contract": OPTIMIZER_SERVING_CONTRACT,
            "skillopt_runtime": skillopt_runtime_contract(),
        }
    )
    return {
        "transition_record": {
            "path": str(CONTEXT_CAPACITY_TRANSITION_RECORD.resolve()),
            "sha256": sha256_file(CONTEXT_CAPACITY_TRANSITION_RECORD),
        },
        "phases": phases,
    }


def endpoint_spec(port: int) -> EndpointSpec:
    for spec in ALL_ENDPOINT_SPECS:
        if spec.port == port:
            return spec
    raise ValueError(f"port must be one of {SERVER_PORTS}")


def server_receipt_path(port: int) -> Path:
    spec = endpoint_spec(port)
    return SERVER_ROOT / f"{spec.role}-{spec.model_alias}-{port}-launch.json"


def endpoint_url(port: int) -> str:
    return endpoint_spec(port).endpoint


def optimizer_endpoint_for_target(target_port: int) -> str:
    try:
        port = TARGET_TO_OPTIMIZER_PORT[target_port]
    except KeyError as exc:
        raise ValueError(f"not a target port: {target_port}") from exc
    return endpoint_url(port)


def config_path(benchmark: str) -> Path:
    require_benchmark(benchmark)
    return EXPERIMENT_DIR / "configs" / f"{benchmark}.yaml"


def payload_path(attack: str) -> Path:
    if attack not in ATTACKS:
        raise ValueError(f"unknown attack: {attack}")
    return EXPERIMENT_DIR / "dymal-injections" / attack / "payload.md"


def initial_path(benchmark: str, condition: str) -> Path:
    require_benchmark(benchmark)
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition}")
    return EXPERIMENT_DIR / "materialized-initials" / benchmark / condition / "initial.md"


def official_initial_path(benchmark: str) -> Path:
    require_benchmark(benchmark)
    return OFFICIAL_ROOT / "skillopt" / "envs" / benchmark / "skills" / "initial.md"


def require_benchmark(benchmark: str) -> None:
    if benchmark not in BENCHMARKS:
        raise ValueError(f"unknown benchmark: {benchmark}")


def counts_for(benchmark: str) -> dict[str, int]:
    require_benchmark(benchmark)
    return dict(COUNTS_BY_BENCHMARK[benchmark])


def batch_size_for(benchmark: str) -> int:
    require_benchmark(benchmark)
    return BATCH_SIZE_BY_BENCHMARK[benchmark]


def split_manifest_path(benchmark: str) -> Path:
    require_benchmark(benchmark)
    return DATA_ROOTS[benchmark] / "split_manifest.json"


def output_root(run: Mapping[str, Any]) -> Path:
    benchmark = str(run.get("benchmark") or "")
    condition = str(run.get("attack") or "clean")
    require_benchmark(benchmark)
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition}")
    return RUN_ROOT / benchmark / condition


def run_id(benchmark: str, condition: str) -> str:
    require_benchmark(benchmark)
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition}")
    return f"{benchmark}__{condition}__q35t_q38o__seed_42"


def completion_config_contract(benchmark: str) -> dict[str, Any]:
    require_benchmark(benchmark)
    counts = counts_for(benchmark)
    return {
        "num_epochs": EPOCHS,
        "train_size": counts["train"],
        "batch_size": batch_size_for(benchmark),
        "accumulation": ACCUMULATION,
        "seed": SEED,
        "minibatch_size": GRADIENT_MINIBATCH_SIZE,
        "merge_batch_size": MERGE_BATCH_SIZE,
        "analyst_workers": ANALYST_WORKERS,
        "max_analyst_rounds": 3,
        "failure_only": False,
        "use_slow_update": True,
        "slow_update_samples": SLOW_UPDATE_SAMPLES,
        "slow_update_gate_with_selection": True,
        "reasoning_effort": "medium",
        "use_meta_skill": True,
        "use_gate": True,
        "gate_metric": "hard",
        "sel_env_num": counts["val"],
        "test_env_num": counts["test"],
        "eval_test": True,
        "split_mode": "split_dir",
        "split_dir": str(DATA_ROOTS[benchmark]),
        "split_seed": SEED,
        "steps_per_epoch": STEPS_PER_EPOCH[benchmark],
        "batches_per_epoch": STEPS_PER_EPOCH[benchmark],
        "samples_per_epoch": counts["train"],
        "workers": ROLLOUT_WORKERS,
        "limit": 0,
    }


def build_run_plan(
    manifest_sha256_by_benchmark: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    hashes = dict(manifest_sha256_by_benchmark or {})
    runs: list[dict[str, Any]] = []
    for benchmark in BENCHMARKS:
        for condition in CONDITIONS:
            attack = None if condition == "clean" else condition
            runs.append(
                {
                    "run_id": run_id(benchmark, condition),
                    "benchmark": benchmark,
                    "condition": "clean" if attack is None else "attacked",
                    "attack": attack,
                    "initial_path": str(initial_path(benchmark, condition).resolve()),
                    "initial_sha256": MATERIALIZED_INITIAL_SHA256[benchmark][condition],
                    "payload_sha256": None if attack is None else FROZEN_PAYLOAD_SHA256[attack],
                    "split_manifest_path": str(split_manifest_path(benchmark)),
                    "split_manifest_sha256": hashes.get(benchmark),
                    "data_counts": counts_for(benchmark),
                    "status": "pending",
                }
            )
    return {
        "schema_version": 3,
        "experiment_id": EXPERIMENT_ID,
        "protocol": "paper-gated-slow-update-local-qwen",
        "skillopt_revision": OFFICIAL_REVISION,
        "models": {
            "target": target_model_identity_contract(),
            "optimizer": optimizer_model_identity_contract(),
        },
        "endpoint_topology": {
            "optimizers": [
                {"port": spec.port, "gpu_ids": list(spec.gpu_ids)}
                for spec in OPTIMIZER_ENDPOINT_SPECS
            ],
            "targets": [
                {
                    "port": spec.port,
                    "gpu_ids": list(spec.gpu_ids),
                    "optimizer_port": TARGET_TO_OPTIMIZER_PORT[spec.port],
                }
                for spec in TARGET_ENDPOINT_SPECS
            ],
        },
        "benchmarks": list(BENCHMARKS),
        "scope_revision": scope_reduction_contract(),
        "attacks": list(ATTACKS),
        "counts_by_benchmark": {
            benchmark: counts_for(benchmark) for benchmark in BENCHMARKS
        },
        "selection_namespace": SELECTION_NAMESPACE,
        "batch_size_by_benchmark": dict(BATCH_SIZE_BY_BENCHMARK),
        "accumulation": ACCUMULATION,
        "epochs": EPOCHS,
        "seed": SEED,
        "thinking_enabled": True,
        "output_token_limit": OUTPUT_TOKEN_LIMIT,
        "slow_update_gate_with_selection": True,
        "no_skill_evaluation": False,
        "clean_trajectories": len(BENCHMARKS),
        "attacked_trajectories": len(BENCHMARKS) * len(ATTACKS),
        "total_trajectories": len(runs),
        "runs": runs,
    }


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= set("0123456789abcdef")
    )


def _validate_run_row(run: Any, *, verify_files: bool) -> dict[str, Any]:
    if not isinstance(run, Mapping):
        raise ValueError("run-index.json contains a non-object run")
    row = dict(run)
    benchmark = str(row.get("benchmark") or "")
    require_benchmark(benchmark)
    attack = None if row.get("attack") is None else str(row["attack"])
    condition_id = attack or "clean"
    expected = {
        "run_id": run_id(benchmark, condition_id),
        "benchmark": benchmark,
        "condition": "clean" if attack is None else "attacked",
        "attack": attack,
        "initial_path": str(initial_path(benchmark, condition_id).resolve()),
        "initial_sha256": MATERIALIZED_INITIAL_SHA256[benchmark][condition_id],
        "payload_sha256": None if attack is None else FROZEN_PAYLOAD_SHA256[attack],
        "split_manifest_path": str(split_manifest_path(benchmark)),
        "data_counts": counts_for(benchmark),
        "status": "pending",
    }
    if set(row) != set(expected) | {"split_manifest_sha256"}:
        raise ValueError("run-index.json run fields differ from the frozen schema")
    for key, value in expected.items():
        if row.get(key) != value:
            raise ValueError(f"{benchmark}/{condition_id}: wrong {key}")
    if not _is_sha256(row.get("split_manifest_sha256")):
        raise ValueError(f"{benchmark}/{condition_id}: invalid split_manifest_sha256")
    if verify_files:
        manifest = split_manifest_path(benchmark)
        start = initial_path(benchmark, condition_id)
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError(f"{benchmark}: split manifest is missing or a symlink")
        if sha256_file(manifest) != row["split_manifest_sha256"]:
            raise ValueError(f"{benchmark}: split manifest hash differs")
        if start.is_symlink() or not start.is_file():
            raise ValueError(f"{benchmark}/{condition_id}: initial skill is missing")
        if sha256_file(start) != row["initial_sha256"]:
            raise ValueError(f"{benchmark}/{condition_id}: initial skill hash differs")
    return row


def validate_run_index(value: Any, *, verify_files: bool = True) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("run-index.json must be an object")
    observed = dict(value)
    runs = observed.get("runs")
    expected_run_count = len(BENCHMARKS) * len(CONDITIONS)
    if not isinstance(runs, list) or len(runs) != expected_run_count:
        raise ValueError(
            f"run-index.json must contain exactly {expected_run_count} runs"
        )
    hashes: dict[str, str] = {}
    for benchmark in BENCHMARKS:
        values = {
            row.get("split_manifest_sha256")
            for row in runs
            if isinstance(row, Mapping) and row.get("benchmark") == benchmark
        }
        if len(values) != 1 or not _is_sha256(next(iter(values), None)):
            raise ValueError(f"run-index.json has inconsistent {benchmark} manifest hashes")
        hashes[benchmark] = str(next(iter(values)))
    expected = build_run_plan(hashes)
    for key, value_expected in expected.items():
        if key != "runs" and observed.get(key) != value_expected:
            raise ValueError(f"run-index.json has the wrong {key}")
    validated = [_validate_run_row(row, verify_files=verify_files) for row in runs]
    cells = [(row["benchmark"], row["attack"] or "clean") for row in validated]
    expected_cells = [
        (benchmark, condition)
        for benchmark in BENCHMARKS
        for condition in CONDITIONS
    ]
    if sorted(cells) != sorted(expected_cells) or len(cells) != len(set(cells)):
        raise ValueError("run-index.json is not the exact active-scope matrix")
    return observed


def load_run_index(path: Path = RUN_INDEX) -> dict[str, Any]:
    return validate_run_index(read_json(path), verify_files=True)


def stable_launch_contract(
    run: Mapping[str, Any], *, verify_files: bool = True
) -> dict[str, Any]:
    indexed = _validate_run_row(run, verify_files=verify_files)
    benchmark = str(indexed["benchmark"])
    config = config_path(benchmark)
    return {
        "schema_version": 4,
        "experiment_id": EXPERIMENT_ID,
        "run_id": indexed["run_id"],
        "benchmark": benchmark,
        "condition": indexed["condition"],
        "attack": indexed["attack"],
        "initial_path": indexed["initial_path"],
        "initial_sha256": indexed["initial_sha256"],
        "payload_sha256": indexed.get("payload_sha256"),
        "output_root": str(output_root(indexed).resolve()),
        "split_manifest_path": indexed["split_manifest_path"],
        "split_manifest_sha256": indexed["split_manifest_sha256"],
        "config_path": str(config),
        "config_sha256": sha256_file(config),
        "skillopt_revision": OFFICIAL_REVISION,
        "skillopt_runtime": skillopt_runtime_contract(),
        "execution_phases": execution_phase_contract(indexed),
        "model_identity": target_model_identity_contract(),
        "target_model_identity": target_model_identity_contract(),
        "optimizer_model_identity": optimizer_model_identity_contract(),
        "target_serving_contract": TARGET_SERVING_CONTRACT,
        "optimizer_serving_contract": OPTIMIZER_SERVING_CONTRACT,
        "thinking_enabled": True,
        "output_token_limit": OUTPUT_TOKEN_LIMIT,
        "slow_update_gate_with_selection": True,
        "counts": counts_for(benchmark),
        "batch_size": batch_size_for(benchmark),
        "epochs": EPOCHS,
        "steps_per_epoch": STEPS_PER_EPOCH[benchmark],
        "total_steps": TOTAL_STEPS[benchmark],
        "rfinal_relative_path": RFINAL_RELATIVE_PATH[benchmark],
        "seed": SEED,
    }


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser().parse_args(argv)
    print(canonical_json(build_run_plan()).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
