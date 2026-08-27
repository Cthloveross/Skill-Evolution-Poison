#!/usr/bin/env python3
"""Fail-closed completion receipts for the 20-step SkillOpt trajectories."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiment import (
    ACCUMULATION,
    ANALYST_WORKERS,
    BENCHMARKS,
    DATA_ROOT,
    EPOCHS,
    GRADIENT_MINIBATCH_SIZE,
    MERGE_BATCH_SIZE,
    MODEL_ALIAS,
    OPTIMIZER_MODEL_ALIAS,
    OUTPUT_TOKEN_LIMIT,
    RFINAL_RELATIVE_PATH,
    ROLLOUT_WORKERS,
    SEED,
    batch_size_for,
    counts_for,
    output_root,
)


class ArtifactValidationError(ValueError):
    """Raised when a terminal SkillOpt artifact violates the frozen contract."""


SLOW_UPDATE_START = "<!-- SLOW_UPDATE_START -->"
SLOW_UPDATE_END = "<!-- SLOW_UPDATE_END -->"
_CANDIDATE_ACTIONS = {"accept", "accept_new_best", "force_accept", "reject"}
_SKIP_ACTIONS = {"skip_no_patches", "skip_no_rewrite"}
_KNOWN_ACTIONS = _CANDIDATE_ACTIONS | _SKIP_ACTIONS

_STATIC_RUN_ARTIFACTS = {
    "launch_contract": Path("launch-contract.json"),
    "runtime_config": Path("config.json"),
    "runtime_state": Path("runtime_state.json"),
    "history": Path("history.json"),
    "summary": Path("summary.json"),
    "r0": Path("skills/skill_v0000.md"),
    "rbest": Path("best_skill.md"),
    "baseline_results": Path("test_eval_baseline/results.jsonl"),
    "best_results": Path("test_eval/results.jsonl"),
    "baseline_test_summary": Path("test_eval_baseline/summary.json"),
    "best_test_summary": Path("test_eval/summary.json"),
    "final_test_summary": Path("test_eval_final/summary.json"),
}


def _steps_per_epoch(benchmark: str) -> int:
    counts = counts_for(benchmark)
    return math.ceil(
        counts["train"] / (batch_size_for(benchmark) * ACCUMULATION)
    )


def _total_steps(benchmark: str) -> int:
    return EPOCHS * _steps_per_epoch(benchmark)


def _canonical_json(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"value is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ArtifactValidationError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _reject_json_constant(token: str) -> None:
    raise ArtifactValidationError(f"non-finite JSON constant is forbidden: {token}")


def _parse_json(payload: bytes, label: str) -> Any:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactValidationError(f"{label}: invalid UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except ArtifactValidationError as exc:
        raise ArtifactValidationError(f"{label}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(
            f"{label}: invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArtifactValidationError(f"{label}: expected a JSON object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ArtifactValidationError(f"{label}: expected a JSON array")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactValidationError(f"{label}: expected a non-empty string")
    return value


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactValidationError(f"{label}: expected an integer")
    return value


def _require_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ArtifactValidationError(f"{label}: expected a lowercase SHA256 digest")
    return digest


def _require_short_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if len(digest) != 16 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ArtifactValidationError(
            f"{label}: expected a 16-character lowercase SHA256 prefix"
        )
    return digest


def _hard_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArtifactValidationError(f"{label}: expected a JSON number")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ArtifactValidationError(f"{label}: expected a finite number in [0, 1]")
    return result


def _strict_equal(observed: Any, expected: Any, label: str) -> None:
    if _canonical_json(observed) != _canonical_json(expected):
        raise ArtifactValidationError(f"{label}: observed value differs from contract")


def _read_artifact(path: Path, label: str) -> bytes:
    try:
        if not path.is_file():
            raise ArtifactValidationError(f"{label}: missing file: {path}")
        return path.read_bytes()
    except ArtifactValidationError:
        raise
    except OSError as exc:
        raise ArtifactValidationError(f"{label}: cannot read {path}: {exc}") from exc


def _artifact_record(path: Path, payload: bytes) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "bytes": len(payload),
        "sha256": _sha256(payload),
    }


def _one_terminal_lf(text: str) -> bytes:
    return (text.rstrip(" \t\r\n") + "\n").encode("utf-8")


def normalize_terminal_empty_slow_update(
    payload: bytes, label: str = "skill"
) -> tuple[bytes, dict[str, Any]]:
    """Strip only the exact empty terminal SkillOpt slow-update placeholder.

    Non-empty, duplicated, out-of-order, or non-terminal marker blocks remain in
    the normalized bytes. This makes ambiguity count as substantive content.
    Terminal whitespace is canonicalized to one LF for checkpoint comparison.
    """

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactValidationError(f"{label}: invalid UTF-8") from exc

    start_count = text.count(SLOW_UPDATE_START)
    end_count = text.count(SLOW_UPDATE_END)
    metadata: dict[str, Any] = {
        "applied": False,
        "start_marker_count": start_count,
        "end_marker_count": end_count,
    }
    if start_count == 0 and end_count == 0:
        metadata["reason"] = "no_markers"
        return _one_terminal_lf(text), metadata
    if start_count != 1 or end_count != 1:
        metadata["reason"] = "ambiguous_markers"
        return _one_terminal_lf(text), metadata

    start = text.index(SLOW_UPDATE_START)
    end = text.index(SLOW_UPDATE_END)
    if end < start:
        metadata["reason"] = "out_of_order_markers"
        return _one_terminal_lf(text), metadata

    inner = text[start + len(SLOW_UPDATE_START) : end]
    trailing = text[end + len(SLOW_UPDATE_END) :]
    if inner.strip():
        metadata["reason"] = "nonempty_block"
        return _one_terminal_lf(text), metadata
    if trailing.strip():
        metadata["reason"] = "nonterminal_block"
        return _one_terminal_lf(text), metadata

    prefix = text[:start]
    if not prefix.endswith("\n\n"):
        metadata["reason"] = "noncanonical_empty_block"
        return _one_terminal_lf(text), metadata

    metadata.update({"applied": True, "reason": "stripped_terminal_empty_block"})
    return _one_terminal_lf(prefix[:-2]), metadata


def _checkpoint_record(path: Path, payload: bytes, label: str) -> dict[str, Any]:
    normalized, normalizer = normalize_terminal_empty_slow_update(payload, label)
    record = _artifact_record(path, payload)
    record.update(
        {
            "substantive_sha256": _sha256(normalized),
            "substantive_bytes": len(normalized),
            "normalizer": normalizer,
        }
    )
    return record


def _safe_path(value: str, *, root: Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ArtifactValidationError(f"{label}: expected an absolute path")
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ArtifactValidationError(f"{label}: path escapes {root}") from exc
    return resolved


def _validate_source_hash(path_value: Any, digest_value: Any, label: str) -> Path:
    path = Path(_require_string(path_value, f"{label}.path"))
    if not path.is_absolute():
        raise ArtifactValidationError(f"{label}.path: expected an absolute path")
    payload = _read_artifact(path, label)
    expected = _require_sha256(digest_value, f"{label}.sha256")
    if _sha256(payload) != expected:
        raise ArtifactValidationError(f"{label}: SHA256 differs from launch contract")
    return path.resolve()


def _validate_run_binding(
    run: Mapping[str, Any], launch: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    run_id = _require_string(run.get("run_id"), "run.run_id")
    benchmark = _require_string(run.get("benchmark"), "run.benchmark")
    if benchmark not in BENCHMARKS:
        raise ArtifactValidationError(f"run.benchmark: unsupported benchmark {benchmark!r}")
    condition = _require_string(run.get("condition"), "run.condition")
    attack_value = run.get("attack")
    if attack_value is not None and not isinstance(attack_value, str):
        raise ArtifactValidationError("run.attack: expected null or string")
    expected_condition = "clean" if attack_value is None else "attacked"
    if condition != expected_condition:
        raise ArtifactValidationError("run.condition is inconsistent with run.attack")

    bindings = {
        "run_id": run_id,
        "benchmark": benchmark,
        "condition": condition,
        "attack": attack_value,
        "initial_path": _require_string(run.get("initial_path"), "run.initial_path"),
        "initial_sha256": _require_sha256(
            run.get("initial_sha256"), "run.initial_sha256"
        ),
        "payload_sha256": run.get("payload_sha256"),
    }
    for key, expected in bindings.items():
        if launch.get(key) != expected:
            raise ArtifactValidationError(
                f"launch-contract.json: {key} is not bound to the run index"
            )

    launch_root = Path(
        _require_string(launch.get("output_root"), "launch.output_root")
    ).resolve()
    if launch_root != run_dir:
        raise ArtifactValidationError("launch.output_root differs from trajectory root")
    if "output_root" in run and Path(str(run["output_root"])).resolve() != run_dir:
        raise ArtifactValidationError("run.output_root differs from trajectory root")

    expected_counts = counts_for(benchmark)
    _strict_equal(launch.get("counts"), expected_counts, "launch.counts")
    for key, expected in {
        "batch_size": batch_size_for(benchmark),
        "epochs": EPOCHS,
        "steps_per_epoch": _steps_per_epoch(benchmark),
        "total_steps": _total_steps(benchmark),
        "rfinal_relative_path": RFINAL_RELATIVE_PATH[benchmark],
        "seed": SEED,
    }.items():
        _strict_equal(launch.get(key), expected, f"launch.{key}")

    return {
        "run_id": run_id,
        "benchmark": benchmark,
        "condition": condition,
        "attack": attack_value,
    }


def _derived_config_contract(
    launch: dict[str, Any], run_dir: Path, data_root: Path
) -> dict[str, Any]:
    benchmark = _require_string(launch.get("benchmark"), "launch.benchmark")
    counts = counts_for(benchmark)
    target_identity = _require_object(
        launch.get("target_model_identity"), "launch.target_model_identity"
    )
    optimizer_identity = _require_object(
        launch.get("optimizer_model_identity"), "launch.optimizer_model_identity"
    )
    target_model = _require_string(
        target_identity.get("alias"), "launch.target_model_identity.alias"
    )
    optimizer_model = _require_string(
        optimizer_identity.get("alias"), "launch.optimizer_model_identity.alias"
    )
    if target_model != MODEL_ALIAS or optimizer_model != OPTIMIZER_MODEL_ALIAS:
        raise ArtifactValidationError("launch role model aliases differ from contract")

    return {
        "model_backend": "qwen_chat",
        "optimizer_backend": "qwen_chat",
        "target_backend": "qwen_chat",
        "optimizer_model": optimizer_model,
        "target_model": target_model,
        "qwen_chat_enable_thinking": True,
        "optimizer_qwen_chat_enable_thinking": True,
        "target_qwen_chat_enable_thinking": True,
        "optimizer_qwen_chat_max_tokens": OUTPUT_TOKEN_LIMIT,
        "target_qwen_chat_max_tokens": OUTPUT_TOKEN_LIMIT,
        "num_epochs": EPOCHS,
        "train_size": counts["train"],
        "batch_size": batch_size_for(benchmark),
        "accumulation": ACCUMULATION,
        "seed": SEED,
        "minibatch_size": GRADIENT_MINIBATCH_SIZE,
        "merge_batch_size": MERGE_BATCH_SIZE,
        "analyst_workers": ANALYST_WORKERS,
        "failure_only": False,
        "max_analyst_rounds": 3,
        "use_slow_update": True,
        "slow_update_samples": 20,
        "slow_update_gate_with_selection": True,
        "reasoning_effort": "medium",
        "use_meta_skill": True,
        "use_gate": True,
        "gate_metric": "hard",
        "sel_env_num": counts["val"],
        "test_env_num": counts["test"],
        "eval_test": True,
        "env": benchmark,
        "skill_init": _require_string(launch.get("initial_path"), "launch.initial_path"),
        "out_root": str(run_dir),
        "split_mode": "split_dir",
        "split_seed": SEED,
        "split_dir": str((data_root / benchmark).resolve()),
        "max_completion_tokens": OUTPUT_TOKEN_LIMIT,
        "steps_per_epoch": _steps_per_epoch(benchmark),
        "batches_per_epoch": _steps_per_epoch(benchmark),
        "samples_per_epoch": counts["train"],
        "workers": ROLLOUT_WORKERS,
        "limit": 0,
    }


def _merge_config_contract(
    derived: dict[str, Any], extra: Mapping[str, Any] | None
) -> dict[str, Any]:
    merged = dict(derived)
    if extra is None:
        return merged
    if not isinstance(extra, Mapping):
        raise ArtifactValidationError("expected_config must be a mapping")
    for key, value in extra.items():
        if not isinstance(key, str) or not key:
            raise ArtifactValidationError("expected_config keys must be non-empty strings")
        if key in merged:
            _strict_equal(value, merged[key], f"expected_config.{key}")
        else:
            merged[key] = value
    return merged


def _validate_config(
    config: dict[str, Any], contract: dict[str, Any], summary: dict[str, Any]
) -> None:
    for key, expected in contract.items():
        if key not in config:
            raise ArtifactValidationError(f"config.json: missing contract key {key!r}")
        _strict_equal(config[key], expected, f"config.json.{key}")
    if "config" not in summary:
        raise ArtifactValidationError("summary.json: missing embedded config")
    _strict_equal(summary["config"], config, "summary.json embedded config")


def _manifest_sections(
    manifest: dict[str, Any], expected_counts: dict[str, int]
) -> tuple[dict[str, list[Any]], dict[str, dict[str, Any]]]:
    selection = manifest.get("selection")
    selected = manifest.get("selected")
    counts = manifest.get("counts")
    if isinstance(selection, dict):
        selected = selection.get("selected", selected)
        counts = selection.get("counts", counts)
    selected_obj = _require_object(selected, "split_manifest.json selected")
    counts_obj = _require_object(counts, "split_manifest.json counts")
    _strict_equal(counts_obj, expected_counts, "split_manifest.json counts")

    output = manifest.get("outputs")
    if output is None:
        output = manifest.get("output")
    output_obj = _require_object(output, "split_manifest.json output")
    if isinstance(output_obj.get("files"), dict):
        output_obj = output_obj["files"]

    selected_splits: dict[str, list[Any]] = {}
    descriptors: dict[str, dict[str, Any]] = {}
    for split in ("train", "val", "test"):
        rows = _require_list(selected_obj.get(split), f"split_manifest.json selected.{split}")
        if len(rows) != expected_counts[split]:
            raise ArtifactValidationError(
                f"split_manifest.json selected.{split}: expected "
                f"{expected_counts[split]} rows, got {len(rows)}"
            )
        selected_splits[split] = rows
        descriptors[split] = _require_object(
            output_obj.get(split), f"split_manifest.json output.{split}"
        )
    return selected_splits, descriptors


def _ids_from_selected(rows: list[Any], split: str) -> list[str]:
    ids: list[str] = []
    for index, row in enumerate(rows):
        obj = _require_object(row, f"split_manifest.json selected.{split}[{index}]")
        ids.append(
            _require_string(
                obj.get("id"), f"split_manifest.json selected.{split}[{index}].id"
            )
        )
    if len(ids) != len(set(ids)):
        raise ArtifactValidationError(
            f"split_manifest.json selected.{split}: duplicate IDs"
        )
    return ids


def _split_file_ids(payload: bytes, path: Path, label: str) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        rows = _require_list(_parse_json(payload, label), label)
        return [
            _require_string(
                _require_object(row, f"{label}[{index}]").get("id"),
                f"{label}[{index}].id",
            )
            for index, row in enumerate(rows)
        ]
    if suffix == ".csv":
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactValidationError(f"{label}: invalid UTF-8") from exc
        reader = csv.DictReader(text.splitlines())
        if reader.fieldnames is None or "id" not in reader.fieldnames:
            raise ArtifactValidationError(f"{label}: missing id column")
        return [
            _require_string(row.get("id"), f"{label} row {index}.id")
            for index, row in enumerate(reader, start=2)
        ]
    raise ArtifactValidationError(f"{label}: unsupported split file extension {suffix!r}")


def _validate_split_manifest(
    manifest: dict[str, Any], benchmark_root: Path, expected_counts: dict[str, int]
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    selected, descriptors = _manifest_sections(manifest, expected_counts)
    selected_ids = {
        split: _ids_from_selected(selected[split], split)
        for split in ("train", "val", "test")
    }
    all_ids = [item for split_ids in selected_ids.values() for item in split_ids]
    if len(all_ids) != len(set(all_ids)):
        raise ArtifactValidationError("split_manifest.json: IDs overlap across splits")

    records: dict[str, dict[str, Any]] = {}
    for split, descriptor in descriptors.items():
        if _require_int(descriptor.get("count"), f"output.{split}.count") != expected_counts[split]:
            raise ArtifactValidationError(f"output.{split}.count differs from contract")

        relative = descriptor.get("relative_path")
        declared = descriptor.get("path")
        if relative is not None:
            relative_path = Path(_require_string(relative, f"output.{split}.relative_path"))
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise ArtifactValidationError(f"output.{split}.relative_path is unsafe")
            path = (benchmark_root / relative_path).resolve()
            if declared is not None and Path(str(declared)).resolve() != path:
                raise ArtifactValidationError(f"output.{split}.path conflicts with relative_path")
        else:
            path = _safe_path(
                _require_string(declared, f"output.{split}.path"),
                root=benchmark_root,
                label=f"output.{split}.path",
            )
        try:
            relative_actual = path.relative_to(benchmark_root.resolve())
        except ValueError as exc:
            raise ArtifactValidationError(f"output.{split}.path escapes benchmark root") from exc
        if relative_actual.parent != Path(split) or path.name not in {"items.json", "items.csv"}:
            raise ArtifactValidationError(f"output.{split}.path is not the canonical split file")

        payload = _read_artifact(path, f"split_{split}")
        digest = _require_sha256(descriptor.get("sha256"), f"output.{split}.sha256")
        if _sha256(payload) != digest:
            raise ArtifactValidationError(f"output.{split}: SHA256 differs from manifest")
        if "bytes" in descriptor and _require_int(
            descriptor["bytes"], f"output.{split}.bytes"
        ) != len(payload):
            raise ArtifactValidationError(f"output.{split}.bytes differs from file")

        file_ids = _split_file_ids(payload, path, f"output.{split}")
        if len(file_ids) != expected_counts[split]:
            raise ArtifactValidationError(
                f"output.{split}: expected {expected_counts[split]} rows, got {len(file_ids)}"
            )
        if len(file_ids) != len(set(file_ids)):
            raise ArtifactValidationError(f"output.{split}: duplicate IDs")
        if set(file_ids) != set(selected_ids[split]):
            raise ArtifactValidationError(f"output.{split}: IDs differ from manifest selection")
        records[f"split_{split}"] = _artifact_record(path, payload)
    return selected_ids, records


def _parse_results(
    payload: bytes, label: str, expected_ids: set[str], expected_count: int
) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactValidationError(f"{label}: invalid UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != expected_count:
        raise ArtifactValidationError(
            f"{label}: expected exactly {expected_count} JSONL rows, got {len(lines)}"
        )

    ids: list[str] = []
    hard_values: list[float] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ArtifactValidationError(f"{label}: empty JSONL row {line_number}")
        row = _require_object(
            _parse_json(line.encode("utf-8"), f"{label} row {line_number}"),
            f"{label} row {line_number}",
        )
        ids.append(_require_string(row.get("id"), f"{label} row {line_number}.id"))
        hard_values.append(
            _hard_number(row.get("hard"), f"{label} row {line_number}.hard")
        )
    if len(ids) != len(set(ids)):
        raise ArtifactValidationError(f"{label}: duplicate IDs")
    if set(ids) != expected_ids:
        missing = sorted(expected_ids - set(ids))
        extra = sorted(set(ids) - expected_ids)
        raise ArtifactValidationError(
            f"{label}: IDs do not exactly match manifest; missing={missing}, extra={extra}"
        )
    ordered_ids = sorted(ids)
    return {
        "row_count": len(ids),
        "unique_id_count": len(set(ids)),
        "ids_sha256": _sha256(_canonical_json(ordered_ids)),
        "hard_mean": sum(hard_values) / len(hard_values),
    }


def _validate_test_summary(
    payload: bytes, label: str, expected_count: int, expected_hard: float
) -> dict[str, Any]:
    summary = _require_object(_parse_json(payload, label), label)
    overall = _require_object(summary.get("overall"), f"{label}.overall")
    if _require_int(overall.get("total"), f"{label}.overall.total") != expected_count:
        raise ArtifactValidationError(f"{label}.overall.total differs from contract")
    hard = _hard_number(overall.get("hard_acc"), f"{label}.overall.hard_acc")
    if hard != expected_hard:
        raise ArtifactValidationError(f"{label}.overall.hard_acc differs from results")
    return {"overall_total": expected_count, "overall_hard": hard}


def _edit_apply_counts(value: Any, label: str) -> dict[str, int]:
    summary = _require_object(value, label)
    counts = {
        key: _require_int(summary.get(key), f"{label}.{key}")
        for key in ("total", "applied", "skipped", "errors")
    }
    if any(count < 0 for count in counts.values()):
        raise ArtifactValidationError(f"{label}: counts must be non-negative")
    if counts["applied"] + counts["skipped"] + counts["errors"] != counts["total"]:
        raise ArtifactValidationError(f"{label}: component counts do not sum to total")
    return counts


def _validate_edit_apply_report(
    row: dict[str, Any], step_dir: Path, label: str
) -> dict[str, Any] | None:
    report_path = step_dir / "edit_apply_report.json"
    history_summary = row.get("edit_apply_summary")
    expected_from_history = (
        _edit_apply_counts(history_summary, f"{label}.edit_apply_summary")
        if history_summary is not None
        else None
    )
    if not report_path.exists():
        return None

    payload = _read_artifact(report_path, f"{label} edit_apply_report")
    report = _require_list(
        _parse_json(payload, f"{label} edit_apply_report"),
        f"{label} edit_apply_report",
    )
    status_counts: Counter[str] = Counter()
    applied = skipped = errors = 0
    for index, value in enumerate(report):
        edit = _require_object(value, f"{label} edit_apply_report[{index}]")
        status = _require_string(
            edit.get("status"), f"{label} edit_apply_report[{index}].status"
        )
        status_counts[status] += 1
        if status.startswith("applied_"):
            applied += 1
        elif status.startswith("skipped_"):
            skipped += 1
        elif status == "error":
            errors += 1
        else:
            raise ArtifactValidationError(
                f"{label} edit_apply_report[{index}].status is unsupported: {status!r}"
            )
    observed = {
        "total": len(report),
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
    }
    if expected_from_history is not None:
        _strict_equal(observed, expected_from_history, f"{label}.edit_apply_summary")
    if "n_edits_ranked" in row:
        ranked = _require_int(row["n_edits_ranked"], f"{label}.n_edits_ranked")
        if ranked != len(report):
            raise ArtifactValidationError(
                f"{label}.n_edits_ranked differs from edit_apply_report"
            )
    return {
        "externally_verified": True,
        "artifact": _artifact_record(report_path, payload),
        "counts": observed,
        "status_counts": dict(sorted(status_counts.items())),
    }


def _validate_history(
    history: Any,
    summary: dict[str, Any],
    state: dict[str, Any],
    run_dir: Path,
    benchmark: str,
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    steps_per_epoch = _steps_per_epoch(benchmark)
    total_steps = _total_steps(benchmark)
    rows = _require_list(history, "history.json")
    if len(rows) != total_steps:
        raise ArtifactValidationError(
            f"history.json: expected {total_steps} steps, got {len(rows)}"
        )

    skill_records: dict[str, dict[str, Any]] = {}
    step_records: list[dict[str, Any]] = []
    actions: list[str] = []
    action_counts: Counter[str] = Counter()
    edit_status_counts: Counter[str] = Counter()
    edit_totals = {"total": 0, "applied": 0, "skipped": 0, "errors": 0}
    candidate_steps = candidate_changed_steps = candidate_noop_steps = 0
    edit_report_steps = 0
    for offset, value in enumerate(rows):
        step = offset + 1
        label = f"history.json[{offset}]"
        row = _require_object(value, label)
        expected_epoch = offset // steps_per_epoch + 1
        expected_step_in_epoch = offset % steps_per_epoch
        for key, expected in {
            "step": step,
            "epoch": expected_epoch,
            "step_in_epoch": expected_step_in_epoch,
        }.items():
            if _require_int(row.get(key), f"{label}.{key}") != expected:
                raise ArtifactValidationError(
                    f"{label}.{key} differs from {steps_per_epoch}-step epoch mapping"
                )
        action = _require_string(row.get("action"), f"{label}.action")
        if action not in _KNOWN_ACTIONS:
            raise ArtifactValidationError(f"{label}.action is unsupported: {action!r}")
        actions.append(action)
        action_counts[action] += 1
        skill_path = run_dir / "skills" / f"skill_v{step:04d}.md"
        skill_payload = _read_artifact(skill_path, f"skill_v{step:04d}")
        normalize_terminal_empty_slow_update(skill_payload, f"skill_v{step:04d}")
        skill_records[f"skill_v{step:04d}"] = _artifact_record(skill_path, skill_payload)

        candidate_hash_value = row.get("candidate_hash")
        if action in _CANDIDATE_ACTIONS and candidate_hash_value is None:
            raise ArtifactValidationError(f"{label}.candidate_hash is required for {action}")
        candidate_record: dict[str, Any] | None = None
        candidate_changed: bool | None = None
        if candidate_hash_value is not None:
            candidate_hash = _require_short_sha256(
                candidate_hash_value, f"{label}.candidate_hash"
            )
            candidate_path = run_dir / "steps" / f"step_{step:04d}" / "candidate_skill.md"
            candidate_payload = _read_artifact(
                candidate_path, f"step_{step:04d} candidate"
            )
            if _sha256(candidate_payload)[:16] != candidate_hash:
                raise ArtifactValidationError(
                    f"step_{step:04d} candidate SHA256 differs from history.json"
                )
            if "candidate_skill_len" in row:
                try:
                    candidate_text = candidate_payload.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ArtifactValidationError(
                        f"step_{step:04d} candidate is not valid UTF-8"
                    ) from exc
                if _require_int(
                    row["candidate_skill_len"], f"{label}.candidate_skill_len"
                ) != len(candidate_text):
                    raise ArtifactValidationError(
                        f"{label}.candidate_skill_len differs from candidate artifact"
                    )
            previous_payload = _read_artifact(
                run_dir / "skills" / f"skill_v{step - 1:04d}.md",
                f"skill_v{step - 1:04d}",
            )
            candidate_changed = candidate_payload != previous_payload
            candidate_steps += 1
            candidate_changed_steps += int(candidate_changed)
            candidate_noop_steps += int(not candidate_changed)
            candidate_record = {
                **_artifact_record(candidate_path, candidate_payload),
                "history_hash": candidate_hash,
            }

        scores: dict[str, float] = {}
        for key in (
            "selection_hard",
            "selection_soft",
            "candidate_gate_score",
            "current_score",
            "best_score",
        ):
            if key in row:
                scores[key] = _hard_number(row[key], f"{label}.{key}")

        step_dir = run_dir / "steps" / f"step_{step:04d}"
        edit_apply = _validate_edit_apply_report(row, step_dir, label)
        if edit_apply is not None:
            edit_report_steps += 1
            for key, count in edit_apply["counts"].items():
                edit_totals[key] += count
            edit_status_counts.update(edit_apply["status_counts"])
        step_records.append(
            {
                "step": step,
                "epoch": expected_epoch,
                "step_in_epoch": expected_step_in_epoch,
                "action": action,
                "checkpoint": skill_records[f"skill_v{step:04d}"],
                "candidate": candidate_record,
                "candidate_changed_from_input": candidate_changed,
                "scores": scores,
                "edit_apply": edit_apply,
            }
        )

    if _require_int(state.get("last_completed_step"), "runtime_state.last_completed_step") != total_steps:
        raise ArtifactValidationError(
            f"runtime_state.last_completed_step must be {total_steps}"
        )
    if _require_int(summary.get("total_steps"), "summary.json total_steps") != total_steps:
        raise ArtifactValidationError(f"summary.json total_steps must be {total_steps}")

    epoch_stats = _require_list(summary.get("epoch_stats"), "summary.json epoch_stats")
    if len(epoch_stats) != EPOCHS:
        raise ArtifactValidationError("summary.json epoch_stats must contain four epochs")
    for epoch_index, value in enumerate(epoch_stats, start=1):
        row = _require_object(value, f"summary.json epoch_stats[{epoch_index - 1}]")
        expected_steps = list(
            range(
                (epoch_index - 1) * steps_per_epoch + 1,
                epoch_index * steps_per_epoch + 1,
            )
        )
        if _require_int(row.get("epoch"), f"epoch_stats[{epoch_index - 1}].epoch") != epoch_index:
            raise ArtifactValidationError("summary.json epoch_stats epoch mapping differs")
        _strict_equal(
            row.get("steps"), expected_steps, f"summary.json epoch_stats[{epoch_index - 1}].steps"
        )

    expected_action_counts = {
        "total_accepts": sum("accept" in action for action in actions),
        "total_rejects": sum(action == "reject" for action in actions),
        "total_skips": sum(action == "skip_no_patches" for action in actions),
    }
    for key, expected in expected_action_counts.items():
        if _require_int(summary.get(key), f"summary.json {key}") != expected:
            raise ArtifactValidationError(f"summary.json {key} differs from history")
    return rows, skill_records, step_records, {
        "action_counts": dict(sorted(action_counts.items())),
        "candidate_steps": candidate_steps,
        "candidate_changed_steps": candidate_changed_steps,
        "candidate_noop_steps": candidate_noop_steps,
        "steps_without_candidate": total_steps - candidate_steps,
        "edit_report_steps": edit_report_steps,
        "edit_totals": edit_totals,
        "edit_status_counts": dict(sorted(edit_status_counts.items())),
    }


def _validate_summary_and_state(
    summary: dict[str, Any],
    state: dict[str, Any],
    run_dir: Path,
    rbest_payload: bytes,
    versioned_payloads: dict[int, bytes],
    history_rows: list[dict[str, Any]],
    benchmark: str,
) -> dict[str, Any]:
    total_steps = _total_steps(benchmark)
    steps_per_epoch = _steps_per_epoch(benchmark)
    best_step = _require_int(state.get("best_step"), "runtime_state.best_step")
    if not 0 <= best_step <= total_steps:
        raise ArtifactValidationError(
            f"runtime_state.best_step must be in [0, {total_steps}]"
        )
    if _require_int(summary.get("best_step"), "summary.json best_step") != best_step:
        raise ArtifactValidationError("summary.json best_step differs from runtime_state")

    best_state_path = Path(
        _require_string(state.get("best_skill_path"), "runtime_state.best_skill_path")
    )
    if not best_state_path.is_absolute() or best_state_path.resolve() != (run_dir / "best_skill.md").resolve():
        raise ArtifactValidationError("runtime_state.best_skill_path is not best_skill.md")

    current_origin = _require_string(state.get("current_origin"), "runtime_state.current_origin")
    best_origin = _require_string(state.get("best_origin"), "runtime_state.best_origin")
    if summary.get("current_origin") != current_origin:
        raise ArtifactValidationError("summary.json current_origin differs from runtime_state")
    if summary.get("best_origin") != best_origin:
        raise ArtifactValidationError("summary.json best_origin differs from runtime_state")

    if best_origin == "initial_skill":
        if best_step != 0:
            raise ArtifactValidationError("initial_skill best_origin requires best_step=0")
        best_reference = versioned_payloads[0]
    elif match := re.fullmatch(r"step_(\d{4})", best_origin):
        origin_step = int(match.group(1))
        if origin_step == 0 or origin_step != best_step:
            raise ArtifactValidationError(
                "step best_origin does not match runtime_state.best_step"
            )
        history_row = history_rows[best_step - 1]
        if history_row.get("action") != "accept_new_best":
            raise ArtifactValidationError(
                "step best_origin does not identify an accepted new-best candidate"
            )
        if _require_int(
            history_row.get("best_step"), f"history.json[{best_step - 1}].best_step"
        ) != best_step:
            raise ArtifactValidationError("best-step history row has inconsistent best_step")
        if _require_string(
            history_row.get("best_origin"),
            f"history.json[{best_step - 1}].best_origin",
        ) != best_origin:
            raise ArtifactValidationError("best-step history row has inconsistent best_origin")

        candidate_path = run_dir / "steps" / best_origin / "candidate_skill.md"
        best_reference = _read_artifact(candidate_path, f"{best_origin} candidate")
        candidate_hash = _require_string(
            history_row.get("candidate_hash"),
            f"history.json[{best_step - 1}].candidate_hash",
        )
        if (
            len(candidate_hash) != 16
            or any(character not in "0123456789abcdef" for character in candidate_hash)
            or candidate_hash != _sha256(best_reference)[:16]
        ):
            raise ArtifactValidationError(
                "best-step candidate SHA256 differs from history.json"
            )
    elif match := re.fullmatch(r"slow_update_epoch_(\d{2})", best_origin):
        epoch = int(match.group(1))
        if not 1 <= epoch <= EPOCHS or best_step != epoch * steps_per_epoch:
            raise ArtifactValidationError(
                "slow-update best_origin does not match runtime_state.best_step"
            )
        slow_dir = run_dir / "slow_update" / f"epoch_{epoch:02d}"
        best_reference = _read_artifact(
            slow_dir / "candidate_skill.md", f"slow_update epoch {epoch} candidate"
        )
        slow_result = _require_object(
            _parse_json(
                _read_artifact(
                    slow_dir / "slow_result.json", f"slow_update epoch {epoch} result"
                ),
                f"slow_update epoch {epoch} result",
            ),
            f"slow_update epoch {epoch} result",
        )
        candidate_hash = _require_string(
            slow_result.get("candidate_hash"),
            f"slow_update epoch {epoch} candidate_hash",
        )
        if (
            len(candidate_hash) != 16
            or any(character not in "0123456789abcdef" for character in candidate_hash)
            or candidate_hash != _sha256(best_reference)[:16]
        ):
            raise ArtifactValidationError(
                "slow-update candidate SHA256 differs from slow_result.json"
            )
    else:
        raise ArtifactValidationError(f"unsupported runtime_state.best_origin: {best_origin!r}")

    if rbest_payload != best_reference:
        raise ArtifactValidationError(
            "best_skill.md differs from the artifact identified by runtime_state.best_origin"
        )

    _hard_number(state.get("current_score"), "runtime_state.current_score")
    _hard_number(state.get("best_score"), "runtime_state.best_score")
    return {
        "best_step": best_step,
        "best_origin": best_origin,
        "current_origin": current_origin,
        "last_completed_step": total_steps,
    }


def _validate_utility(
    summary: dict[str, Any], r0_hard: float, rbest_hard: float, rfinal_hard: float
) -> dict[str, Any]:
    fields = {
        "baseline_test_hard": r0_hard,
        "test_hard": rbest_hard,
        "final_test_hard": rfinal_hard,
        "test_delta_hard": rbest_hard - r0_hard,
        "final_test_delta_hard": rfinal_hard - r0_hard,
    }
    for key, expected in fields.items():
        observed = summary.get(key)
        if key.endswith("_hard") and "delta" not in key:
            observed = _hard_number(observed, f"summary.json {key}")
        elif isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isfinite(float(observed)):
            raise ArtifactValidationError(f"summary.json {key} must be a finite number")
        if float(observed) != expected:
            raise ArtifactValidationError(f"summary.json {key} differs from recomputed results")
    return {
        "r0_hard": r0_hard,
        "rbest_hard": rbest_hard,
        "rfinal_hard": rfinal_hard,
        "rbest_delta_hard": rbest_hard - r0_hard,
        "rfinal_delta_hard": rfinal_hard - r0_hard,
    }


def build_completion_receipt(
    run: Mapping[str, Any],
    expected_launch_contract: Mapping[str, Any],
    *,
    expected_config: Mapping[str, Any] | None = None,
    run_dir: str | Path | None = None,
    data_root: str | Path = DATA_ROOT,
    expected_test_count: int | None = None,
) -> dict[str, Any]:
    """Validate a terminal native run and return a deterministic receipt."""

    if not isinstance(run, Mapping):
        raise ArtifactValidationError("run must be a mapping")
    if not isinstance(expected_launch_contract, Mapping):
        raise ArtifactValidationError("expected_launch_contract must be a mapping")
    resolved_run_dir = (
        Path(run_dir) if run_dir is not None else output_root(dict(run))
    ).resolve()
    resolved_data_root = Path(data_root).resolve()

    paths = {
        name: resolved_run_dir / relative
        for name, relative in _STATIC_RUN_ARTIFACTS.items()
    }
    payloads = {name: _read_artifact(path, name) for name, path in paths.items()}
    launch = _require_object(
        _parse_json(payloads["launch_contract"], "launch-contract.json"),
        "launch-contract.json",
    )
    _strict_equal(launch, dict(expected_launch_contract), "launch-contract.json")
    identity = _validate_run_binding(run, launch, resolved_run_dir)
    benchmark = identity["benchmark"]
    counts = counts_for(benchmark)
    steps_per_epoch = _steps_per_epoch(benchmark)
    total_steps = _total_steps(benchmark)
    if expected_test_count is not None and _require_int(
        expected_test_count, "expected_test_count"
    ) != counts["test"]:
        raise ArtifactValidationError("expected_test_count differs from benchmark contract")

    _validate_source_hash(
        launch.get("config_path"), launch.get("config_sha256"), "source_config"
    )
    initial_source = _validate_source_hash(
        launch.get("initial_path"), launch.get("initial_sha256"), "initial_skill"
    )
    if initial_source != Path(str(run["initial_path"])).resolve():
        raise ArtifactValidationError("initial source path differs from run index")

    manifest_path = _validate_source_hash(
        launch.get("split_manifest_path"),
        launch.get("split_manifest_sha256"),
        "split_manifest",
    )
    expected_manifest_path = resolved_data_root / benchmark / "split_manifest.json"
    if manifest_path != expected_manifest_path.resolve():
        raise ArtifactValidationError("split_manifest path differs from benchmark data root")
    paths["split_manifest"] = manifest_path
    payloads["split_manifest"] = _read_artifact(manifest_path, "split_manifest")

    config = _require_object(
        _parse_json(payloads["runtime_config"], "config.json"), "config.json"
    )
    state = _require_object(
        _parse_json(payloads["runtime_state"], "runtime_state.json"),
        "runtime_state.json",
    )
    history = _parse_json(payloads["history"], "history.json")
    summary = _require_object(
        _parse_json(payloads["summary"], "summary.json"), "summary.json"
    )
    manifest = _require_object(
        _parse_json(payloads["split_manifest"], "split_manifest.json"),
        "split_manifest.json",
    )

    config_contract = _merge_config_contract(
        _derived_config_contract(launch, resolved_run_dir, resolved_data_root),
        expected_config,
    )
    _validate_config(config, config_contract, summary)
    selected_ids, split_records = _validate_split_manifest(
        manifest, resolved_data_root / benchmark, counts
    )

    history_rows, trajectory_records, trajectory_steps, edit_diagnostics = _validate_history(
        history, summary, state, resolved_run_dir, benchmark
    )
    versioned_payloads = {
        step: _read_artifact(
            resolved_run_dir / "skills" / f"skill_v{step:04d}.md",
            f"skill_v{step:04d}",
        )
        for step in range(total_steps + 1)
    }

    r0_payload = payloads["r0"]
    rbest_payload = payloads["rbest"]
    if _sha256(r0_payload) != _require_sha256(
        launch.get("initial_sha256"), "launch.initial_sha256"
    ):
        raise ArtifactValidationError("R0 SHA256 differs from launch and run index")
    if r0_payload != initial_source.read_bytes():
        raise ArtifactValidationError("R0 bytes differ from materialized initial skill")

    expected_rfinal = (resolved_run_dir / RFINAL_RELATIVE_PATH[benchmark]).resolve()
    state_rfinal_raw = _require_string(
        state.get("current_skill_path"), "runtime_state.current_skill_path"
    )
    state_rfinal = Path(state_rfinal_raw)
    if not state_rfinal.is_absolute() or state_rfinal.resolve() != expected_rfinal:
        raise ArtifactValidationError(
            "Rfinal must come from runtime_state.current_skill_path and resolve to "
            f"{RFINAL_RELATIVE_PATH[benchmark]}"
        )
    rfinal_payload = _read_artifact(state_rfinal, "rfinal")
    paths["rfinal"] = state_rfinal.resolve()
    payloads["rfinal"] = rfinal_payload

    summary_identity = _validate_summary_and_state(
        summary,
        state,
        resolved_run_dir,
        rbest_payload,
        versioned_payloads,
        history_rows,
        benchmark,
    )

    test_ids = set(selected_ids["test"])
    baseline_result = _parse_results(
        payloads["baseline_results"],
        "test_eval_baseline/results.jsonl",
        test_ids,
        counts["test"],
    )
    best_result = _parse_results(
        payloads["best_results"],
        "test_eval/results.jsonl",
        test_ids,
        counts["test"],
    )
    final_results_path = resolved_run_dir / "test_eval_final" / "results.jsonl"
    if final_results_path.is_file():
        final_results_payload = _read_artifact(final_results_path, "final_results")
        final_results_source = "independent"
    elif rfinal_payload == rbest_payload:
        final_results_path = paths["best_results"]
        final_results_payload = payloads["best_results"]
        final_results_source = "reused_rbest_identical_checkpoint"
    else:
        raise ArtifactValidationError(
            "test_eval_final/results.jsonl is required when Rfinal differs from Rbest"
        )
    final_result = _parse_results(
        final_results_payload,
        "test_eval_final/results.jsonl",
        test_ids,
        counts["test"],
    )
    paths["final_results"] = final_results_path
    payloads["final_results"] = final_results_payload

    test_summaries = {
        "baseline": _validate_test_summary(
            payloads["baseline_test_summary"],
            "test_eval_baseline/summary.json",
            counts["test"],
            baseline_result["hard_mean"],
        ),
        "best": _validate_test_summary(
            payloads["best_test_summary"],
            "test_eval/summary.json",
            counts["test"],
            best_result["hard_mean"],
        ),
        "final": _validate_test_summary(
            payloads["final_test_summary"],
            "test_eval_final/summary.json",
            counts["test"],
            final_result["hard_mean"],
        ),
    }
    utility = _validate_utility(
        summary,
        baseline_result["hard_mean"],
        best_result["hard_mean"],
        final_result["hard_mean"],
    )

    artifacts = {
        name: _artifact_record(paths[name], payloads[name])
        for name in sorted(paths)
    }
    artifacts.update(split_records)
    artifacts["r0"] = _checkpoint_record(paths["r0"], r0_payload, "R0")
    artifacts["rbest"] = _checkpoint_record(
        paths["rbest"], rbest_payload, "Rbest"
    )
    artifacts["rfinal"] = _checkpoint_record(
        paths["rfinal"], rfinal_payload, "Rfinal"
    )

    r0 = artifacts["r0"]
    rbest = artifacts["rbest"]
    rfinal = artifacts["rfinal"]
    return {
        "schema_version": 3,
        "receipt_type": "skillopt-three-checkpoint-completion",
        "run": identity,
        "contracts": {
            "launch": launch,
            "config": config_contract,
            "counts": dict(counts),
            "steps_per_epoch": steps_per_epoch,
            "total_steps": total_steps,
        },
        "artifacts": artifacts,
        "trajectory": {
            "skills": trajectory_records,
            "steps": trajectory_steps,
            "edit_diagnostics": edit_diagnostics,
        },
        "checkpoints": {
            "r0_sha256": r0["sha256"],
            "rbest_sha256": rbest["sha256"],
            "rfinal_sha256": rfinal["sha256"],
            "r0_substantive_sha256": r0["substantive_sha256"],
            "rbest_substantive_sha256": rbest["substantive_sha256"],
            "rfinal_substantive_sha256": rfinal["substantive_sha256"],
            "raw_changes": {
                "r0_to_rbest": r0["sha256"] != rbest["sha256"],
                "r0_to_rfinal": r0["sha256"] != rfinal["sha256"],
                "rbest_to_rfinal": rbest["sha256"] != rfinal["sha256"],
            },
            "substantive_changes": {
                "r0_to_rbest": r0["substantive_sha256"] != rbest["substantive_sha256"],
                "r0_to_rfinal": r0["substantive_sha256"] != rfinal["substantive_sha256"],
                "rbest_to_rfinal": rbest["substantive_sha256"] != rfinal["substantive_sha256"],
            },
        },
        "test_set": {
            "ids": sorted(test_ids),
            "ids_sha256": _sha256(_canonical_json(sorted(test_ids))),
            "r0": baseline_result,
            "rbest": best_result,
            "rfinal": final_result,
            "rfinal_results_source": final_results_source,
            "summaries": test_summaries,
        },
        "utility": utility,
        "summary": {
            **summary_identity,
            "total_accepts": summary.get("total_accepts"),
            "total_rejects": summary.get("total_rejects"),
            "total_skips": summary.get("total_skips"),
            "baseline_test_hard": summary.get("baseline_test_hard"),
            "test_hard": summary.get("test_hard"),
            "final_test_hard": summary.get("final_test_hard"),
            "test_delta_hard": summary.get("test_delta_hard"),
            "final_test_delta_hard": summary.get("final_test_delta_hard"),
        },
    }


def validate_completion_receipt(
    receipt: Mapping[str, Any],
    run: Mapping[str, Any],
    expected_launch_contract: Mapping[str, Any],
    *,
    expected_config: Mapping[str, Any] | None = None,
    run_dir: str | Path | None = None,
    data_root: str | Path = DATA_ROOT,
    expected_test_count: int | None = None,
) -> dict[str, Any]:
    """Revalidate current files and require an exact deterministic receipt."""

    if not isinstance(receipt, Mapping):
        raise ArtifactValidationError("receipt must be a mapping")
    current = build_completion_receipt(
        run,
        expected_launch_contract,
        expected_config=expected_config,
        run_dir=run_dir,
        data_root=data_root,
        expected_test_count=expected_test_count,
    )
    _strict_equal(dict(receipt), current, "completion receipt")
    return current
