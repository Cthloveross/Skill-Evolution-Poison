#!/usr/bin/env python3
"""Safely precompute one SearchQA best-skill test in isolated ID shards."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import experiment


SCHEMA_VERSION = 1
MANIFEST_NAME = "shard-manifest.json"
PROVENANCE_NAME = "acceleration-provenance.json"
RESULTS_NAME = "results.jsonl"
CANONICAL_BEST_DIR = "test_eval"
EVAL_SPLIT = "valid_unseen"


class AccelerationError(RuntimeError):
    """Raised when an acceleration artifact fails its frozen contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise AccelerationError(f"cannot read valid JSON from {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(canonical_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _resolve_existing_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise AccelerationError(f"{label} is not a file: {resolved}")
    return resolved


def _resolve_existing_dir(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise AccelerationError(f"{label} is not a directory: {resolved}")
    return resolved


def validate_evaluate_paths(
    *,
    run_root: Path,
    checkpoint: Path,
    output_dir: Path,
    canonical_destination: Path,
) -> tuple[Path, Path, Path, Path]:
    """Resolve paths and constrain writes to the isolated acceleration tree."""
    resolved_run = _resolve_existing_dir(run_root, "run root")
    resolved_checkpoint = _resolve_existing_file(checkpoint, "checkpoint")
    expected_checkpoint = resolved_run / "best_skill.md"
    if resolved_checkpoint != expected_checkpoint:
        raise AccelerationError(
            f"only the frozen best checkpoint is supported: {expected_checkpoint}"
        )

    resolved_canonical = canonical_destination.expanduser().resolve(strict=False)
    expected_canonical = resolved_run / CANONICAL_BEST_DIR
    if resolved_canonical != expected_canonical:
        raise AccelerationError(
            f"canonical destination must be {expected_canonical}, got {resolved_canonical}"
        )
    if resolved_canonical.exists():
        raise AccelerationError(
            f"canonical destination already exists; native evaluation may own it: "
            f"{resolved_canonical}"
        )

    resolved_output = output_dir.expanduser().resolve(strict=False)
    acceleration_root = resolved_run / "acceleration"
    try:
        resolved_output.relative_to(acceleration_root)
    except ValueError as exc:
        raise AccelerationError(
            f"shard output must be below {acceleration_root}: {resolved_output}"
        ) from exc
    if resolved_output == acceleration_root:
        raise AccelerationError("shard output cannot be the acceleration root itself")
    if resolved_output == resolved_canonical:
        raise AccelerationError("shard output cannot be a canonical result directory")
    return resolved_run, resolved_checkpoint, resolved_output, resolved_canonical


def partition_items(
    items: list[dict[str, Any]], shard_index: int, shard_count: int
) -> list[dict[str, Any]]:
    if shard_count < 1:
        raise AccelerationError("shard-count must be positive")
    if shard_index < 0 or shard_index >= shard_count:
        raise AccelerationError(
            f"shard-index must be in [0, {shard_count}), got {shard_index}"
        )
    ids = [str(item.get("id", "")) for item in items]
    if any(not item_id for item_id in ids):
        raise AccelerationError("every test item must have a non-empty id")
    if len(ids) != len(set(ids)):
        raise AccelerationError("test item IDs are not unique")
    return items[shard_index::shard_count]


def _split_contract(cfg: dict[str, Any], all_ids: list[str]) -> dict[str, Any]:
    split_dir = _resolve_existing_dir(Path(str(cfg.get("split_dir") or "")), "split dir")
    manifest_path = _resolve_existing_file(split_dir / "split_manifest.json", "split manifest")
    test_path = _resolve_existing_file(split_dir / "test" / "items.json", "test items")
    return {
        "split": EVAL_SPLIT,
        "split_dir": str(split_dir),
        "split_manifest_path": str(manifest_path),
        "split_manifest_sha256": sha256_file(manifest_path),
        "test_items_path": str(test_path),
        "test_items_sha256": sha256_file(test_path),
        "all_item_count": len(all_ids),
        "all_item_ids_sha256": hash_json(all_ids),
        "all_item_ids": all_ids,
    }


def _evaluation_contract(cfg: dict[str, Any]) -> dict[str, Any]:
    contract = {
        "environment": cfg.get("env"),
        "target_backend": cfg.get("target_backend"),
        "target_model": cfg.get("target_model"),
        "temperature": cfg.get("target_qwen_chat_temperature"),
        "thinking_enabled": cfg.get("target_qwen_chat_enable_thinking"),
        "api_timeout_seconds": cfg.get("target_qwen_chat_timeout_seconds"),
        "api_max_tokens": cfg.get("target_qwen_chat_max_tokens"),
        "rollout_max_completion_tokens": cfg.get("max_completion_tokens"),
        "exec_timeout_seconds": cfg.get("exec_timeout"),
        "max_turns": cfg.get("max_turns"),
        "workers": cfg.get("workers"),
        "seed": cfg.get("seed"),
        "test_env_num": cfg.get("test_env_num"),
        "split": EVAL_SPLIT,
    }
    required = {
        "environment": "searchqa",
        "target_backend": "qwen_chat",
        "target_model": experiment.TARGET_MODEL_ALIAS,
        "thinking_enabled": True,
    }
    for key, expected in required.items():
        if contract.get(key) != expected:
            raise AccelerationError(
                f"unsupported evaluation contract: {key}={contract.get(key)!r}, "
                f"expected {expected!r}"
            )
    if int(contract["test_env_num"] or 0) <= 0:
        raise AccelerationError("test_env_num must be positive")
    return contract


def _runtime_contract() -> dict[str, Any]:
    root = _resolve_existing_dir(experiment.OFFICIAL_ROOT, "official SkillOpt root")
    relative_files = (
        "skillopt/envs/searchqa/adapter.py",
        "skillopt/envs/searchqa/dataloader.py",
        "skillopt/envs/searchqa/rollout.py",
        "skillopt/envs/searchqa/evaluator.py",
        "skillopt/model/qwen_backend.py",
    )
    return {
        "root": str(root),
        "revision": experiment.OFFICIAL_REVISION,
        "files": [
            {"path": relative, "sha256": sha256_file(root / relative)}
            for relative in relative_files
        ],
    }


def qualify_endpoint(endpoint: str, api_key: str, expected_model: str) -> dict[str, Any]:
    normalized = endpoint.rstrip("/")
    request = urllib.request.Request(
        f"{normalized}/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload_bytes = response.read()
    except Exception as exc:  # noqa: BLE001
        raise AccelerationError(f"endpoint qualification failed for {endpoint}: {exc}") from exc
    try:
        payload = json.loads(payload_bytes)
        model_ids = sorted(
            str(row["id"])
            for row in payload.get("data", [])
            if isinstance(row, dict) and row.get("id")
        )
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AccelerationError(f"malformed /models response from {endpoint}") from exc
    if expected_model not in model_ids:
        raise AccelerationError(
            f"endpoint {endpoint} serves {model_ids}, expected {expected_model!r}"
        )
    return {
        "url": normalized,
        "observed_model_ids": model_ids,
        "expected_model": expected_model,
    }


def _load_official_items(cfg: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    official_root = str(experiment.OFFICIAL_ROOT.resolve())
    if official_root not in sys.path:
        sys.path.insert(0, official_root)

    from skillopt.envs.searchqa.adapter import SearchQAAdapter

    adapter = SearchQAAdapter(
        split_dir=str(cfg.get("split_dir") or ""),
        data_path=str(cfg.get("data_path") or ""),
        split_mode=str(cfg.get("split_mode") or "split_dir"),
        split_seed=int(cfg.get("split_seed", 42)),
        split_output_dir=str(cfg.get("split_output_dir") or ""),
        max_turns=int(cfg.get("max_turns", 1)),
        exec_timeout=int(cfg.get("exec_timeout", 120)),
        workers=int(cfg.get("workers", 1)),
        analyst_workers=int(cfg.get("analyst_workers", 1)),
        failure_only=bool(cfg.get("failure_only", False)),
        minibatch_size=int(cfg.get("minibatch_size", 8)),
        edit_budget=int(cfg.get("edit_budget", 4)),
        seed=int(cfg.get("seed", 42)),
        limit=int(cfg.get("limit", 0)),
        max_completion_tokens=int(cfg.get("max_completion_tokens", 16384)),
    )
    adapter.setup(cfg)
    items = adapter.build_eval_env(
        int(cfg["test_env_num"]), EVAL_SPLIT, int(cfg.get("seed", 42))
    )
    return adapter, items


def _configure_official_target(cfg: dict[str, Any], endpoint: str) -> None:
    official_root = str(experiment.OFFICIAL_ROOT.resolve())
    if official_root not in sys.path:
        sys.path.insert(0, official_root)
    from skillopt.model import (
        configure_qwen_chat,
        set_target_backend,
        set_target_deployment,
    )

    set_target_backend(str(cfg["target_backend"]))
    set_target_deployment(str(cfg["target_model"]))
    configure_qwen_chat(
        target_base_url=endpoint.rstrip("/"),
        target_api_key=str(cfg.get("target_qwen_chat_api_key") or "dummy"),
        target_temperature=cfg.get("target_qwen_chat_temperature"),
        target_timeout_seconds=cfg.get("target_qwen_chat_timeout_seconds"),
        target_max_tokens=cfg.get("target_qwen_chat_max_tokens"),
        target_enable_thinking=cfg.get("target_qwen_chat_enable_thinking"),
    )


def _read_result_rows(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
    if not path.is_file():
        raise AccelerationError(f"missing results file: {path}")
    rows: dict[str, dict[str, Any]] = {}
    raw_rows: dict[str, bytes] = {}
    with path.open("rb") as stream:
        for line_number, raw in enumerate(stream, 1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
                item_id = str(row["id"])
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise AccelerationError(
                    f"malformed result at {path}:{line_number}"
                ) from exc
            if not item_id:
                raise AccelerationError(f"empty result ID at {path}:{line_number}")
            if item_id in rows:
                raise AccelerationError(f"duplicate result ID {item_id!r} in {path}")
            rows[item_id] = row
            raw_rows[item_id] = stripped
    return rows, raw_rows


def _validate_results(
    results_path: Path, expected_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
    rows, raw_rows = _read_result_rows(results_path)
    observed = set(rows)
    expected = set(expected_ids)
    if observed != expected:
        missing = sorted(expected - observed)
        unexpected = sorted(observed - expected)
        raise AccelerationError(
            f"result ID coverage mismatch for {results_path}: "
            f"missing={missing[:5]} unexpected={unexpected[:5]}"
        )
    return rows, raw_rows


def evaluate_shard(args: argparse.Namespace) -> dict[str, Any]:
    run_root, checkpoint, output_dir, canonical_destination = validate_evaluate_paths(
        run_root=args.run_root,
        checkpoint=args.checkpoint,
        output_dir=args.output_dir,
        canonical_destination=args.canonical_destination,
    )
    config_path = _resolve_existing_file(run_root / "config.json", "runtime config")
    cfg = read_json(config_path)
    if Path(str(cfg.get("out_root") or "")).resolve() != run_root:
        raise AccelerationError("runtime config out_root does not match --run-root")
    evaluation_contract = _evaluation_contract(cfg)
    adapter, all_items = _load_official_items(cfg)
    all_ids = [str(item["id"]) for item in all_items]
    shard_items = partition_items(all_items, args.shard_index, args.shard_count)
    shard_ids = [str(item["id"]) for item in shard_items]
    split_contract = _split_contract(cfg, all_ids)
    if len(all_items) != int(cfg["test_env_num"]):
        raise AccelerationError(
            f"official adapter returned {len(all_items)} test items, "
            f"expected {cfg['test_env_num']}"
        )

    endpoint_identity = qualify_endpoint(
        args.endpoint,
        str(cfg.get("target_qwen_chat_api_key") or "dummy"),
        str(cfg["target_model"]),
    )
    binding = {
        "run_root": str(run_root),
        "canonical_destination": str(canonical_destination),
        "config": {
            "path": str(config_path),
            "sha256": sha256_file(config_path),
        },
        "checkpoint": {
            "path": str(checkpoint),
            "sha256": sha256_file(checkpoint),
            "size_bytes": checkpoint.stat().st_size,
        },
        "split": split_contract,
        "evaluation": evaluation_contract,
        "evaluation_sha256": hash_json(evaluation_contract),
        "official_runtime": _runtime_contract(),
        "shard": {
            "index": args.shard_index,
            "count": args.shard_count,
            "item_count": len(shard_ids),
            "item_ids_sha256": hash_json(shard_ids),
            "item_ids": shard_ids,
        },
        "endpoint": endpoint_identity,
    }

    manifest_path = output_dir / MANIFEST_NAME
    if output_dir.exists():
        if not output_dir.is_dir() or not manifest_path.is_file():
            raise AccelerationError(
                f"existing shard output lacks a valid manifest: {output_dir}"
            )
        existing = read_json(manifest_path)
        if existing.get("schema_version") != SCHEMA_VERSION:
            raise AccelerationError(f"unsupported shard manifest: {manifest_path}")
        if existing.get("binding") != binding:
            raise AccelerationError(f"shard resume binding changed: {manifest_path}")
    else:
        output_dir.mkdir(parents=True)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "skillopt_searchqa_test_shard",
        "status": "running",
        "started_at": utc_now(),
        "binding": binding,
    }
    if manifest_path.exists():
        previous = read_json(manifest_path)
        manifest["started_at"] = previous.get("started_at") or manifest["started_at"]
        if previous.get("status") == "completed":
            _validate_results(output_dir / RESULTS_NAME, shard_ids)
            return previous
    atomic_write_json(manifest_path, manifest)

    _configure_official_target(cfg, args.endpoint)
    skill_content = checkpoint.read_text(encoding="utf-8")
    adapter.rollout(shard_items, skill_content, str(output_dir))
    rows, _ = _validate_results(output_dir / RESULTS_NAME, shard_ids)

    unchanged = {
        "config_sha256": sha256_file(config_path),
        "checkpoint_sha256": sha256_file(checkpoint),
        "split_manifest_sha256": sha256_file(Path(split_contract["split_manifest_path"])),
        "test_items_sha256": sha256_file(Path(split_contract["test_items_path"])),
    }
    expected_unchanged = {
        "config_sha256": binding["config"]["sha256"],
        "checkpoint_sha256": binding["checkpoint"]["sha256"],
        "split_manifest_sha256": split_contract["split_manifest_sha256"],
        "test_items_sha256": split_contract["test_items_sha256"],
    }
    if unchanged != expected_unchanged:
        manifest["status"] = "invalidated"
        manifest["invalidated_at"] = utc_now()
        manifest["observed_final_hashes"] = unchanged
        atomic_write_json(manifest_path, manifest)
        raise AccelerationError("frozen inputs changed while the shard was running")

    result_summary = {
        "path": str(output_dir / RESULTS_NAME),
        "sha256": sha256_file(output_dir / RESULTS_NAME),
        "row_count": len(rows),
        "item_ids_sha256": hash_json(shard_ids),
        "hard_correct": sum(int(bool(row.get("hard", 0))) for row in rows.values()),
        "agent_failures": sum(row.get("agent_ok") is False for row in rows.values()),
    }
    with (output_dir / RESULTS_NAME).open("rb") as stream:
        os.fsync(stream.fileno())
    manifest.update(
        {
            "status": "completed",
            "completed_at": utc_now(),
            "result": result_summary,
        }
    )
    atomic_write_json(manifest_path, manifest)
    return manifest


def _load_completed_manifest(shard_dir: Path) -> dict[str, Any]:
    resolved = _resolve_existing_dir(shard_dir, "shard directory")
    manifest = read_json(resolved / MANIFEST_NAME)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise AccelerationError(f"unsupported shard manifest in {resolved}")
    if manifest.get("kind") != "skillopt_searchqa_test_shard":
        raise AccelerationError(f"wrong shard artifact kind in {resolved}")
    if manifest.get("status") != "completed":
        raise AccelerationError(f"shard is not complete: {resolved}")
    manifest["_resolved_dir"] = str(resolved)
    return manifest


def _common_binding(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in binding.items()
        if key not in {"endpoint", "shard"}
    }


def _validate_frozen_common_binding(common: dict[str, Any]) -> None:
    """Re-hash every frozen input immediately before canonical installation."""
    checks = (
        (common["config"], "config"),
        (common["checkpoint"], "checkpoint"),
        (
            {
                "path": common["split"]["split_manifest_path"],
                "sha256": common["split"]["split_manifest_sha256"],
            },
            "split manifest",
        ),
        (
            {
                "path": common["split"]["test_items_path"],
                "sha256": common["split"]["test_items_sha256"],
            },
            "test items",
        ),
    )
    for record, label in checks:
        path = _resolve_existing_file(Path(record["path"]), label)
        if sha256_file(path) != record["sha256"]:
            raise AccelerationError(f"{label} changed after shard evaluation: {path}")
    if hash_json(common["evaluation"]) != common["evaluation_sha256"]:
        raise AccelerationError("evaluation contract hash is invalid")
    if _runtime_contract() != common["official_runtime"]:
        raise AccelerationError("official SkillOpt runtime changed after shard evaluation")


def _validate_merge_destination(run_root: Path, destination: Path) -> tuple[Path, Path]:
    resolved_run = _resolve_existing_dir(run_root, "run root")
    resolved_destination = destination.expanduser().resolve(strict=False)
    expected = resolved_run / CANONICAL_BEST_DIR
    if resolved_destination != expected:
        raise AccelerationError(
            f"merge destination must be {expected}, got {resolved_destination}"
        )
    if resolved_destination.exists():
        raise AccelerationError(f"merge destination already exists: {resolved_destination}")
    if resolved_destination.parent.stat().st_dev != resolved_run.stat().st_dev:
        raise AccelerationError("merge staging and destination must share a filesystem")
    return resolved_run, resolved_destination


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically install a directory without replacing a racing writer."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AccelerationError("renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        rename_noreplace,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number, os.strerror(error_number), str(destination)
        )
    raise OSError(error_number, os.strerror(error_number), str(destination))


def _write_results(path: Path, all_ids: list[str], raw_rows: dict[str, bytes]) -> None:
    with path.open("wb") as stream:
        for item_id in all_ids:
            stream.write(raw_rows[item_id])
            stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _copy_prediction_evidence(
    staging: Path, manifests: Iterable[dict[str, Any]], all_ids: list[str]
) -> int:
    copied = 0
    destination_root = staging / "predictions"
    expected = set(all_ids)
    for manifest in manifests:
        shard_dir = Path(manifest["_resolved_dir"])
        source_root = shard_dir / "predictions"
        if not source_root.is_dir():
            continue
        for child in source_root.iterdir():
            if not child.is_dir() or child.name not in expected:
                continue
            destination = destination_root / child.name
            if destination.exists():
                raise AccelerationError(
                    f"duplicate prediction evidence for ID {child.name!r}"
                )
            destination_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(child, destination, symlinks=True)
            copied += 1
    return copied


def merge_shards(
    *, run_root: Path, destination: Path, shard_dirs: list[Path]
) -> dict[str, Any]:
    resolved_run, resolved_destination = _validate_merge_destination(
        run_root, destination
    )
    if not shard_dirs:
        raise AccelerationError("at least one --shard-dir is required")
    manifests = [_load_completed_manifest(path) for path in shard_dirs]
    bindings = [manifest.get("binding") for manifest in manifests]
    if any(not isinstance(binding, dict) for binding in bindings):
        raise AccelerationError("a shard manifest lacks its binding")
    first_binding = bindings[0]
    common = _common_binding(first_binding)
    if any(_common_binding(binding) != common for binding in bindings[1:]):
        raise AccelerationError("shard experiment/checkpoint/split contracts differ")
    if Path(common["run_root"]).resolve() != resolved_run:
        raise AccelerationError("shard run root differs from merge run root")
    if Path(common["canonical_destination"]).resolve() != resolved_destination:
        raise AccelerationError("shard canonical destination differs from merge destination")
    _validate_frozen_common_binding(common)

    all_ids = list(common["split"]["all_item_ids"])
    if len(all_ids) != len(set(all_ids)):
        raise AccelerationError("common split contains duplicate IDs")
    if hash_json(all_ids) != common["split"]["all_item_ids_sha256"]:
        raise AccelerationError("common split ID hash is invalid")
    if len(all_ids) != int(common["split"]["all_item_count"]):
        raise AccelerationError("common split count is invalid")

    shard_count_values = {int(binding["shard"]["count"]) for binding in bindings}
    if len(shard_count_values) != 1:
        raise AccelerationError("shard-count values differ")
    shard_count = shard_count_values.pop()
    indices = [int(binding["shard"]["index"]) for binding in bindings]
    if sorted(indices) != list(range(shard_count)) or len(indices) != len(set(indices)):
        raise AccelerationError(
            f"expected exactly shard indices 0..{shard_count - 1}, got {sorted(indices)}"
        )

    merged_rows: dict[str, bytes] = {}
    for manifest, binding in zip(manifests, bindings):
        shard = binding["shard"]
        expected_ids = list(shard["item_ids"])
        expected_partition = all_ids[int(shard["index"]) :: shard_count]
        if expected_ids != expected_partition:
            raise AccelerationError(
                f"shard {shard['index']} IDs are not the deterministic partition"
            )
        if hash_json(expected_ids) != shard["item_ids_sha256"]:
            raise AccelerationError(f"shard {shard['index']} ID hash is invalid")
        if len(expected_ids) != int(shard["item_count"]):
            raise AccelerationError(f"shard {shard['index']} count is invalid")
        shard_dir = Path(manifest["_resolved_dir"])
        rows, raw_rows = _validate_results(shard_dir / RESULTS_NAME, expected_ids)
        result = manifest.get("result") or {}
        if result.get("sha256") != sha256_file(shard_dir / RESULTS_NAME):
            raise AccelerationError(f"shard {shard['index']} result hash changed")
        if int(result.get("row_count", -1)) != len(rows):
            raise AccelerationError(f"shard {shard['index']} result count changed")
        overlap = set(merged_rows).intersection(raw_rows)
        if overlap:
            raise AccelerationError(f"result IDs overlap across shards: {sorted(overlap)[:5]}")
        merged_rows.update(raw_rows)

    if set(merged_rows) != set(all_ids):
        missing = sorted(set(all_ids) - set(merged_rows))
        unexpected = sorted(set(merged_rows) - set(all_ids))
        raise AccelerationError(
            f"merged coverage is incomplete: missing={missing[:5]} "
            f"unexpected={unexpected[:5]}"
        )

    staging = resolved_destination.parent / (
        f".{resolved_destination.name}.acceleration-{uuid.uuid4().hex}"
    )
    staging.mkdir(mode=0o755)
    try:
        _write_results(staging / RESULTS_NAME, all_ids, merged_rows)
        prediction_count = _copy_prediction_evidence(staging, manifests, all_ids)
        provenance = {
            "schema_version": SCHEMA_VERSION,
            "kind": "skillopt_searchqa_parallel_test_merge",
            "created_at": utc_now(),
            "run_root": str(resolved_run),
            "destination": str(resolved_destination),
            "checkpoint": common["checkpoint"],
            "config": common["config"],
            "split": {
                key: value
                for key, value in common["split"].items()
                if key != "all_item_ids"
            },
            "evaluation": common["evaluation"],
            "evaluation_sha256": common["evaluation_sha256"],
            "official_runtime": common["official_runtime"],
            "row_count": len(merged_rows),
            "item_ids_sha256": hash_json(all_ids),
            "prediction_directory_count": prediction_count,
            "results_sha256": sha256_file(staging / RESULTS_NAME),
            "shards": [
                {
                    "index": int(manifest["binding"]["shard"]["index"]),
                    "directory": manifest["_resolved_dir"],
                    "manifest_sha256": sha256_file(
                        Path(manifest["_resolved_dir"]) / MANIFEST_NAME
                    ),
                    "endpoint": manifest["binding"]["endpoint"],
                    "result": manifest["result"],
                }
                for manifest in sorted(
                    manifests, key=lambda value: int(value["binding"]["shard"]["index"])
                )
            ],
        }
        atomic_write_json(staging / PROVENANCE_NAME, provenance)
        if resolved_destination.exists():
            raise AccelerationError(
                f"native evaluation claimed destination before merge: {resolved_destination}"
            )
        try:
            _rename_noreplace(staging, resolved_destination)
        except FileExistsError as exc:
            raise AccelerationError(
                f"native evaluation claimed destination during merge: {resolved_destination}"
            ) from exc
        directory_fd = os.open(resolved_destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return provenance
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)

    evaluate = subparsers.add_parser("evaluate", help="run one isolated test shard")
    evaluate.add_argument("--run-root", type=Path, required=True)
    evaluate.add_argument("--checkpoint", type=Path, required=True)
    evaluate.add_argument("--endpoint", required=True)
    evaluate.add_argument("--output-dir", type=Path, required=True)
    evaluate.add_argument("--canonical-destination", type=Path, required=True)
    evaluate.add_argument("--shard-index", type=int, required=True)
    evaluate.add_argument("--shard-count", type=int, required=True)

    merge = subparsers.add_parser("merge", help="validate and atomically install shards")
    merge.add_argument("--run-root", type=Path, required=True)
    merge.add_argument("--destination", type=Path, required=True)
    merge.add_argument("--shard-dir", type=Path, action="append", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mode == "evaluate":
        result = evaluate_shard(args)
    else:
        result = merge_shards(
            run_root=args.run_root,
            destination=args.destination,
            shard_dirs=args.shard_dir,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    exit_code = main()
    if len(sys.argv) > 1 and sys.argv[1] == "evaluate":
        # Official rollout deliberately abandons timed-out HTTP futures. Once the
        # completed manifest and results are durable, do not let Python's global
        # ThreadPoolExecutor exit hook wait for those irrelevant requests.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)
    raise SystemExit(exit_code)
