#!/usr/bin/env python3
"""Launch one role-bound optimizer or target endpoint."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import experiment


VLLM = Path(experiment.TARGET_SERVING_CONTRACT["venv"]) / "bin" / "vllm"


def effective_serving_contract(spec: experiment.EndpointSpec) -> dict[str, Any]:
    serving = dict(spec.serving_contract)
    return serving


def server_command(*, spec: experiment.EndpointSpec) -> list[str]:
    serving = effective_serving_contract(spec)
    chat_template_kwargs: dict[str, Any] = {"enable_thinking": True}
    if serving.get("reasoning_effort"):
        chat_template_kwargs["reasoning_effort"] = serving["reasoning_effort"]
    command = [
        str(VLLM),
        "serve",
        str(spec.model_dir),
        "--host",
        "127.0.0.1",
        "--port",
        str(spec.port),
        "--served-model-name",
        spec.model_alias,
        "--tensor-parallel-size",
        str(serving["tensor_parallel_size"]),
        "--max-model-len",
        str(serving["max_model_len"]),
        "--dtype",
        str(serving["dtype"]),
        "--gpu-memory-utilization",
        str(serving["gpu_memory_utilization"]),
        "--max-num-seqs",
        str(serving["max_num_seqs"]),
        "--max-num-batched-tokens",
        str(serving["max_num_batched_tokens"]),
        "--seed",
        str(serving["seed"]),
        "--enforce-eager",
        "--disable-custom-all-reduce",
        "--attention-backend",
        str(serving["attention_backend"]),
        "--gdn-prefill-backend",
        str(serving["gdn_prefill_backend"]),
    ]
    if serving["language_model_only"]:
        command.append("--language-model-only")
    else:
        command.extend(
            [
                "--limit-mm-per-prompt",
                '{"image":1,"video":0}',
                "--mm-processor-kwargs",
                '{"max_pixels":5242880}',
            ]
        )
    command.extend(
        [
            "--enable-auto-tool-choice",
            "--tool-call-parser",
            str(serving["tool_call_parser"]),
            "--reasoning-parser",
            str(serving["reasoning_parser"]),
            "--default-chat-template-kwargs",
            json.dumps(chat_template_kwargs, separators=(",", ":")),
        ]
    )
    return command


def model_identity(spec: experiment.EndpointSpec) -> dict[str, Any]:
    return (
        experiment.optimizer_model_identity_contract()
        if spec.role == "optimizer"
        else experiment.target_model_identity_contract()
    )


def validate_local_model(spec: experiment.EndpointSpec) -> None:
    identity = model_identity(spec)
    checks = {
        spec.model_dir / "config.json": identity["config_sha256"],
        spec.model_dir / "model.safetensors.index.json": identity[
            "weight_index_sha256"
        ],
    }
    if spec.role == "optimizer":
        checks[spec.model_dir / "crc32.txt"] = identity["crc_manifest_sha256"]
    for path, expected in checks.items():
        if not path.is_file() or experiment.sha256_file(path) != expected:
            raise ValueError(f"frozen model identity mismatch: {path}")
    if not VLLM.is_file():
        raise ValueError(f"vLLM executable is missing: {VLLM}")


def launch_contract(*, spec: experiment.EndpointSpec) -> dict[str, Any]:
    command = server_command(spec=spec)
    serving = effective_serving_contract(spec)
    return {
        "schema_version": 3,
        "experiment_id": experiment.EXPERIMENT_ID,
        "created_at_utc": experiment.utc_now(),
        "status": "launching",
        "pid": os.getpid(),
        "role": spec.role,
        "physical_gpu_ids": list(spec.gpu_ids),
        "port": spec.port,
        "endpoint": spec.endpoint,
        "model_identity": model_identity(spec),
        "serving_contract": serving,
        "command": command,
        "command_sha256": experiment.sha256_bytes(experiment.canonical_json(command)),
        "environment": {
            "CUDA_VISIBLE_DEVICES": ",".join(str(gpu) for gpu in spec.gpu_ids),
            "VLLM_USE_FLASHINFER_SAMPLER": str(
                serving["vllm_use_flashinfer_sampler"]
            ),
            "NCCL_IB_DISABLE": "1",
            "NCCL_NET": "Socket",
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("optimizer", "target"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    spec = experiment.endpoint_spec(args.port)
    if spec.role != args.role:
        parser.error(f"port {args.port} belongs to role {spec.role}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spec = experiment.endpoint_spec(args.port)
    validate_local_model(spec)
    receipt_path = args.receipt or experiment.server_receipt_path(spec.port)
    receipt = launch_contract(spec=spec)
    experiment.atomic_write_json(receipt_path, receipt)
    environment = os.environ.copy()
    environment.update(receipt["environment"])
    environment["PYTHONUNBUFFERED"] = "1"
    os.execve(str(VLLM), receipt["command"], environment)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
