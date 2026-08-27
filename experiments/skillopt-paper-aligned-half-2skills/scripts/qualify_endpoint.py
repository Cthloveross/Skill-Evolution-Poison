#!/usr/bin/env python3
"""Qualify one role-bound endpoint with thinking enabled."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import experiment


QUALIFICATION_PROFILES: dict[str, dict[str, int | str]] = {
    "target": {
        "prompt_tokens": 40_000,
        "max_tokens": 2_048,
        "expected_answer": "CTX65_OK",
        "scope": "role_bound_65k_thinking_qualification",
    },
    "optimizer": {
        "prompt_tokens": 96_000,
        "max_tokens": 16_384,
        "expected_answer": "CTX131_OK",
        "scope": "role_bound_131k_thinking_qualification",
    },
}


def request_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=1800) as response:
        value = json.loads(response.read().decode())
    if not isinstance(value, dict):
        raise ValueError(f"{url} returned non-object JSON")
    return value


def completion_text(value: dict[str, Any]) -> str:
    choices = value.get("choices")
    message = choices[0].get("message") if isinstance(choices, list) and choices else None
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("completion response has no text content")
    return message["content"].strip()


def rendered_token_count(tokenizer: Any, content: str) -> int:
    value = tokenizer.apply_chat_template(
        [{"role": "user", "content": content}],
        tokenize=True,
        add_generation_prompt=True,
        enable_thinking=True,
    )
    ids = value.get("input_ids") if isinstance(value, Mapping) else value
    if not isinstance(ids, list):
        raise ValueError("chat template did not return token IDs")
    return len(ids)


def qualification_profile(role: str) -> dict[str, int | str]:
    try:
        return QUALIFICATION_PROFILES[role]
    except KeyError as exc:
        raise ValueError(f"unsupported endpoint role: {role}") from exc


def build_long_content(
    tokenizer: Any,
    *,
    prompt_tokens: int,
    expected_answer: str,
) -> tuple[str, int, int]:
    suffix = f"\nReply with exactly {expected_answer}."
    low, high = 1, prompt_tokens * 2
    while low < high:
        middle = (low + high) // 2
        content = ("qualification " * middle) + suffix
        if rendered_token_count(tokenizer, content) < prompt_tokens:
            low = middle + 1
        else:
            high = middle
    content = ("qualification " * low) + suffix
    return content, rendered_token_count(tokenizer, content), low


def gpu_observations(gpu_ids: tuple[int, ...]) -> list[dict[str, int]]:
    output = subprocess.check_output(
        [
            "nvidia-smi",
            "--query-gpu=index,memory.used,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        text=True,
    )
    rows: dict[int, dict[str, int]] = {}
    for line in output.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) == 4:
            index = int(fields[0])
            rows[index] = {
                "physical_gpu_id": index,
                "memory_used_mib": int(fields[1]),
                "memory_free_mib": int(fields[2]),
                "utilization_percent": int(fields[3]),
            }
    return [rows[gpu] for gpu in gpu_ids]


def main(argv: list[str] | None = None) -> int:
    from transformers import AutoTokenizer

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = experiment.endpoint_spec(args.port)
    profile = qualification_profile(spec.role)
    base = spec.endpoint.rstrip("/")
    models = request_json(base + "/models")
    rows = [row for row in models.get("data", []) if isinstance(row, dict)]
    model_row = next((row for row in rows if row.get("id") == spec.model_alias), None)
    expected_context = spec.serving_contract["max_model_len"]
    if model_row is None or model_row.get("max_model_len") != expected_context:
        raise SystemExit(
            f"endpoint does not expose the required {expected_context}-token model: {rows}"
        )

    tokenizer = AutoTokenizer.from_pretrained(spec.model_dir, local_files_only=True)
    expected_answer = str(profile["expected_answer"])
    content, local_tokens, repetitions = build_long_content(
        tokenizer,
        prompt_tokens=int(profile["prompt_tokens"]),
        expected_answer=expected_answer,
    )
    response = request_json(
        base + "/chat/completions",
        {
            "model": spec.model_alias,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": int(profile["max_tokens"]),
            "chat_template_kwargs": {"enable_thinking": True},
        },
    )
    answer = completion_text(response)
    if answer != expected_answer:
        raise SystemExit(f"long-context answer mismatch: {answer!r}")
    identity = (
        experiment.optimizer_model_identity_contract()
        if spec.role == "optimizer"
        else experiment.target_model_identity_contract()
    )
    receipt = {
        "schema_version": 3,
        "experiment_id": experiment.EXPERIMENT_ID,
        "created_at": experiment.utc_now(),
        "status": "passed",
        "scope": str(profile["scope"]),
        "role": spec.role,
        "endpoint": spec.endpoint,
        "gpu_ids": list(spec.gpu_ids),
        "model_identity": identity,
        "serving_contract": spec.serving_contract,
        "runtime_packages": {
            package: importlib.metadata.version(package)
            for package in ("vllm", "torch", "transformers")
        },
        "long_request": {
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "repetitions": repetitions,
            "local_prompt_tokens": local_tokens,
            "requested_output_tokens": int(profile["max_tokens"]),
            "enable_thinking": True,
            "answer": answer,
            "usage": response.get("usage"),
        },
        "observed_after_request": gpu_observations(spec.gpu_ids),
    }
    experiment.atomic_write_json(args.output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
