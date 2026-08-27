#!/usr/bin/env python3
"""Build fail-closed live JSON and Markdown results for the frozen matrix."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import experiment
import run_asr_probe as probe
from artifact_validation import (
    ArtifactValidationError,
    normalize_terminal_empty_slow_update,
    validate_completion_receipt,
)


SUMMARY_VERSION = "skillopt-searchqa-only-summary-v2"
CHECKPOINTS = ("r0", "rbest", "rfinal")
ASR_LABELS = {"r0": "initial", "rbest": "best", "rfinal": "final"}
EXPECTED_TRAJECTORIES = len(experiment.BENCHMARKS) * len(experiment.CONDITIONS)
EXPECTED_ATTACKED_CELLS = len(experiment.BENCHMARKS) * len(experiment.ATTACKS)
EXPECTED_ASR_RECEIPTS = EXPECTED_ATTACKED_CELLS * len(CHECKPOINTS)
TERMINAL_ASR_STATUSES = frozenset(
    {"valid", "indeterminate_fail_closed", "unobservable"}
)
LIVE_SUMMARY_PATH = experiment.EXPERIMENT_DIR / "records" / "live-summary.json"
RESULTS_PATH = experiment.EXPERIMENT_DIR / "RESULTS.md"

CompletionInspector = Callable[[Mapping[str, Any]], dict[str, Any]]
AsrInspector = Callable[[Mapping[str, Any], str], dict[str, Any]]


def known_metric(value: Any, **metadata: Any) -> dict[str, Any]:
    return {"status": "known", "value": value, **metadata}


def unknown_metric(
    reason: str, *, evidence_status: str, **metadata: Any
) -> dict[str, Any]:
    return {
        "status": "unknown",
        "value": None,
        "evidence_status": evidence_status,
        "reason": reason,
        **metadata,
    }


def _unknown_completion(status: str, reason: str) -> dict[str, Any]:
    metric = lambda: unknown_metric(reason, evidence_status=status)
    return {
        "trajectory": {"status": status, "reason": reason},
        "accuracy": {checkpoint: metric() for checkpoint in CHECKPOINTS},
        "accuracy_deltas": {"rbest_minus_r0": metric(), "rfinal_minus_r0": metric()},
        "evolution": {
            "rbest": {"raw_changed": metric(), "substantive_changed": metric()},
            "rfinal": {"raw_changed": metric(), "substantive_changed": metric()},
        },
        "optimizer": _unknown_optimizer(status, reason),
    }


def _unknown_optimizer(status: str, reason: str) -> dict[str, Any]:
    candidate = {
        key: None
        for key in (
            "observed_steps",
            "substantive_change",
            "no_op",
            "unobserved_steps",
        )
    }
    edits = {
        key: None
        for key in (
            "observed_steps",
            "unobserved_steps",
            "total",
            "applied",
            "skipped",
            "errors",
        )
    }
    gate = {
        key: None
        for key in ("observed_steps", "applied", "rejected", "skipped")
    }
    return {
        "status": "unknown",
        "evidence_status": status,
        "reason": reason,
        "total_steps": None,
        "candidate": candidate,
        "edit_application": dict(edits),
        "post_slow_candidate_gate": {
            **gate,
            "definition": (
                "fast-update history rows with epoch > 2; the first "
                "non-placeholder slow update is produced after epoch 2"
            ),
        },
        "post_slow_edit_application": dict(edits),
    }


def _number(value: Any, label: str, *, bounded: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} is not a number")
    result = float(value)
    if not math.isfinite(result) or (bounded and not 0.0 <= result <= 1.0):
        raise ValueError(f"{label} is outside its valid range")
    return result


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} is not Boolean")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} is not a non-negative integer")
    return value


def _bound_payload(path: Path, record: Mapping[str, Any], label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is not a regular file")
    payload = path.read_bytes()
    if record.get("path") != str(path.resolve()):
        raise ValueError(f"{label} path differs from completion evidence")
    if record.get("sha256") != experiment.sha256_bytes(payload):
        raise ValueError(f"{label} SHA256 differs from completion evidence")
    if record.get("bytes") != len(payload):
        raise ValueError(f"{label} byte count differs from completion evidence")
    return payload


def _edit_counts(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} is not an object")
    counts = {
        key: _nonnegative_int(value.get(key), f"{label}.{key}")
        for key in ("total", "applied", "skipped", "errors")
    }
    if counts["applied"] + counts["skipped"] + counts["errors"] != counts["total"]:
        raise ValueError(f"{label} component counts do not sum to total")
    return counts


def _sum_edit_history(
    rows: Sequence[Mapping[str, Any]], *, total_steps: int
) -> dict[str, int]:
    observed = 0
    totals = {"total": 0, "applied": 0, "skipped": 0, "errors": 0}
    for index, row in enumerate(rows):
        value = row.get("edit_apply_summary")
        if value is None:
            continue
        counts = _edit_counts(value, f"history[{index}].edit_apply_summary")
        observed += 1
        for key, count in counts.items():
            totals[key] += count
    return {
        "observed_steps": observed,
        "unobserved_steps": total_steps - observed,
        **totals,
    }


def optimizer_diagnostics(
    root: Path, verified: Mapping[str, Any], benchmark: str
) -> dict[str, Any]:
    """Derive optimizer counts only from artifacts bound by completion receipt."""

    total_steps = experiment.TOTAL_STEPS[benchmark]
    trajectory = verified.get("trajectory")
    artifacts = verified.get("artifacts")
    if not isinstance(trajectory, Mapping) or not isinstance(artifacts, Mapping):
        raise ValueError("completion receipt lacks optimizer trajectory evidence")
    steps = trajectory.get("steps")
    skills = trajectory.get("skills")
    history_record = artifacts.get("history")
    if (
        not isinstance(steps, list)
        or len(steps) != total_steps
        or not isinstance(skills, Mapping)
        or not isinstance(history_record, Mapping)
    ):
        raise ValueError("completion optimizer trajectory shape differs from contract")

    history_payload = _bound_payload(root / "history.json", history_record, "history")
    try:
        history = json.loads(history_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("bound history is not valid JSON") from exc
    if not isinstance(history, list) or len(history) != total_steps:
        raise ValueError("bound history length differs from contract")
    history_rows: list[Mapping[str, Any]] = []
    for index, value in enumerate(history):
        if not isinstance(value, Mapping):
            raise ValueError(f"history[{index}] is not an object")
        history_rows.append(value)

    substantive_change = 0
    no_op = 0
    observed_candidates = 0
    accepted_actions = {"accept", "accept_new_best", "force_accept"}
    skipped_actions = {"skip_no_patches", "skip_no_rewrite"}
    post_slow_actions = {"applied": 0, "rejected": 0, "skipped": 0}
    post_slow_rows: list[Mapping[str, Any]] = []

    for index, step_value in enumerate(steps):
        if not isinstance(step_value, Mapping):
            raise ValueError(f"trajectory step {index + 1} is not an object")
        expected_step = index + 1
        step = _nonnegative_int(step_value.get("step"), f"step[{index}].step")
        epoch = _nonnegative_int(step_value.get("epoch"), f"step[{index}].epoch")
        if step != expected_step:
            raise ValueError("optimizer trajectory steps are out of order")
        candidate = step_value.get("candidate")
        if candidate is not None:
            if not isinstance(candidate, Mapping):
                raise ValueError(f"step[{index}].candidate is not an object")
            candidate_path = Path(str(candidate.get("path")))
            candidate_payload = _bound_payload(
                candidate_path, candidate, f"step {step} candidate"
            )
            previous_path = root / "skills" / f"skill_v{step - 1:04d}.md"
            previous_record = (
                artifacts.get("r0")
                if step == 1
                else skills.get(f"skill_v{step - 1:04d}")
            )
            if not isinstance(previous_record, Mapping):
                raise ValueError(f"step {step} lacks prior checkpoint evidence")
            previous_payload = _bound_payload(
                previous_path, previous_record, f"step {step} prior checkpoint"
            )
            normalized_candidate, _ = normalize_terminal_empty_slow_update(
                candidate_payload, f"step {step} candidate"
            )
            normalized_previous, _ = normalize_terminal_empty_slow_update(
                previous_payload, f"step {step} prior checkpoint"
            )
            observed_candidates += 1
            changed = normalized_candidate != normalized_previous
            substantive_change += int(changed)
            no_op += int(not changed)

        action = step_value.get("action")
        if not isinstance(action, str):
            raise ValueError(f"step[{index}].action is missing")
        if epoch > 2:
            history_row = history_rows[index]
            if history_row.get("epoch") != epoch or history_row.get("step") != step:
                raise ValueError("history and completion trajectory step identities differ")
            post_slow_rows.append(history_row)
            if action in accepted_actions:
                post_slow_actions["applied"] += 1
            elif action == "reject":
                post_slow_actions["rejected"] += 1
            elif action in skipped_actions:
                post_slow_actions["skipped"] += 1
            else:
                raise ValueError(f"unsupported post-slow action: {action!r}")

    post_slow_total = len(post_slow_rows)
    return {
        "status": "known",
        "evidence_status": "completion_receipt_valid",
        "reason": "derived from hash-bound history and optimizer artifacts",
        "total_steps": total_steps,
        "candidate": {
            "observed_steps": observed_candidates,
            "substantive_change": substantive_change,
            "no_op": no_op,
            "unobserved_steps": total_steps - observed_candidates,
        },
        "edit_application": _sum_edit_history(
            history_rows, total_steps=total_steps
        ),
        "post_slow_candidate_gate": {
            "definition": (
                "fast-update history rows with epoch > 2; the first "
                "non-placeholder slow update is produced after epoch 2"
            ),
            "observed_steps": post_slow_total,
            **post_slow_actions,
        },
        "post_slow_edit_application": _sum_edit_history(
            post_slow_rows, total_steps=post_slow_total
        ),
    }


def inspect_completion(run: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-validate one trajectory; any evidence failure becomes unknown."""

    root = experiment.output_root(run).resolve()
    receipt_path = root / "completion-receipt.json"
    if not receipt_path.exists():
        return _unknown_completion("missing", "completion receipt is absent")
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return _unknown_completion("invalid", "completion receipt is not a regular file")
    try:
        receipt_sha256 = experiment.sha256_file(receipt_path)
        receipt = experiment.read_json(receipt_path)
        if not isinstance(receipt, Mapping):
            raise ValueError("completion receipt is not an object")
        verified = validate_completion_receipt(
            receipt,
            run,
            experiment.stable_launch_contract(run),
            expected_config=experiment.completion_config_contract(
                str(run["benchmark"])
            ),
        )
        if experiment.sha256_file(receipt_path) != receipt_sha256:
            raise ValueError("completion receipt changed during validation")
        utility = verified["utility"]
        changes = verified["checkpoints"]
        raw = changes["raw_changes"]
        substantive = changes["substantive_changes"]
        r0 = _number(utility["r0_hard"], "utility.r0_hard", bounded=True)
        rbest = _number(utility["rbest_hard"], "utility.rbest_hard", bounded=True)
        rfinal = _number(utility["rfinal_hard"], "utility.rfinal_hard", bounded=True)
        rbest_delta = _number(
            utility["rbest_delta_hard"], "utility.rbest_delta_hard", bounded=False
        )
        rfinal_delta = _number(
            utility["rfinal_delta_hard"], "utility.rfinal_delta_hard", bounded=False
        )
        if rbest_delta != rbest - r0 or rfinal_delta != rfinal - r0:
            raise ValueError("utility deltas do not match checkpoint accuracies")
        benchmark = str(run["benchmark"])
        test_denominator = experiment.counts_for(benchmark)["test"]
        optimizer = optimizer_diagnostics(root, verified, benchmark)
        return {
            "trajectory": {
                "status": "valid",
                "reason": "deep completion validation passed",
                "receipt_path": str(receipt_path),
                "receipt_sha256": receipt_sha256,
            },
            "accuracy": {
                "r0": known_metric(r0, test_denominator=test_denominator),
                "rbest": known_metric(rbest, test_denominator=test_denominator),
                "rfinal": known_metric(rfinal, test_denominator=test_denominator),
            },
            "accuracy_deltas": {
                "rbest_minus_r0": known_metric(rbest_delta),
                "rfinal_minus_r0": known_metric(rfinal_delta),
            },
            "evolution": {
                "rbest": {
                    "raw_changed": known_metric(
                        _boolean(raw["r0_to_rbest"], "raw r0_to_rbest")
                    ),
                    "substantive_changed": known_metric(
                        _boolean(
                            substantive["r0_to_rbest"],
                            "substantive r0_to_rbest",
                        )
                    ),
                },
                "rfinal": {
                    "raw_changed": known_metric(
                        _boolean(raw["r0_to_rfinal"], "raw r0_to_rfinal")
                    ),
                    "substantive_changed": known_metric(
                        _boolean(
                            substantive["r0_to_rfinal"],
                            "substantive r0_to_rfinal",
                        )
                    ),
                },
            },
            "optimizer": optimizer,
        }
    except (
        ArtifactValidationError,
        KeyError,
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return _unknown_completion(
            "invalid", f"completion validation failed: {type(exc).__name__}: {exc}"
        )


def inspect_asr(run: Mapping[str, Any], checkpoint: str) -> dict[str, Any]:
    """Deep-validate one formal ASR receipt and return a tri-state metric."""

    if checkpoint not in CHECKPOINTS:
        raise ValueError(f"unknown checkpoint: {checkpoint}")
    label = ASR_LABELS[checkpoint]
    result_path = experiment.output_root(run).resolve() / "asr" / label / "result.json"
    if not result_path.exists():
        return unknown_metric(
            "formal ASR receipt is absent",
            evidence_status="missing",
            receipt_path=str(result_path),
        )
    if result_path.is_symlink() or not result_path.is_file():
        return unknown_metric(
            "formal ASR receipt is not a regular file",
            evidence_status="invalid",
            receipt_path=str(result_path),
        )
    try:
        receipt_sha256 = experiment.sha256_file(result_path)
        result = probe.read_json(result_path, "formal ASR result")
        if not isinstance(result, dict):
            raise probe.ProbeError("formal ASR result is not an object")
        if result.get("output_root") != str(result_path.parent):
            raise probe.ProbeError("formal ASR output root differs from its canonical slot")
        source = probe.checkpoint_for(dict(run), label)
        raw_value = result.get("raw_result_path")
        raw_path = Path(raw_value) if isinstance(raw_value, str) else None
        probe.validate_receipt_binding(
            result,
            dict(run),
            source,
            raw_path,
            require_execution_provenance=True,
        )
        if experiment.sha256_file(result_path) != receipt_sha256:
            raise probe.ProbeError("formal ASR receipt changed during validation")
        evidence_status = result.get("status")
        if evidence_status not in TERMINAL_ASR_STATUSES:
            raise probe.ProbeError(f"unsupported formal status: {evidence_status!r}")
        value = result.get("asr")
        metadata = {
            "evidence_status": evidence_status,
            "receipt_path": str(result_path),
            "receipt_sha256": receipt_sha256,
            "checkpoint_sha256": result.get("checkpoint_sha256"),
            "provenance": {
                "checkpoint": {
                    "label": result.get("checkpoint_label"),
                    "source_kind": result.get("checkpoint_source_kind"),
                    "path": result.get("checkpoint_path"),
                    "sha256": result.get("checkpoint_sha256"),
                    "completion_receipt_path": result.get(
                        "completion_receipt_path"
                    ),
                    "completion_receipt_sha256": result.get(
                        "completion_receipt_sha256"
                    ),
                    "summary_artifact_sha256": result.get(
                        "summary_artifact_sha256"
                    ),
                    "runtime_state_artifact_sha256": result.get(
                        "runtime_state_artifact_sha256"
                    ),
                },
                "materialization": {
                    key: result["materialization"].get(key)
                    for key in (
                        "skill_md_path",
                        "skill_md_sha256",
                        "checkpoint_body_exact",
                        "entry_script_path",
                        "entry_script_sha256",
                        "task_prompt_path",
                        "task_prompt_sha256",
                        "prompt_manifest_path",
                        "prompt_manifest_sha256",
                    )
                },
                "raw_result": {
                    "path": result.get("raw_result_path"),
                    "sha256": result.get("raw_result_sha256"),
                    "runner_returncode": result.get("isolated_runner_returncode"),
                },
                "model": result.get("model"),
                "execution": result.get("execution_provenance"),
                "runtime": result.get("runtime"),
            },
        }
        if type(value) is bool:
            return known_metric(value, **metadata)
        if value is None:
            reasons = result.get("fail_closed_reasons")
            reason = (
                "; ".join(str(item) for item in reasons)
                if isinstance(reasons, list) and reasons
                else f"formal ASR status is {evidence_status}"
            )
            return unknown_metric(reason, **metadata)
        raise probe.ProbeError("formal ASR value is not Boolean or null")
    except (
        KeyError,
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        probe.ProbeError,
    ) as exc:
        return unknown_metric(
            f"ASR receipt validation failed: {type(exc).__name__}: {exc}",
            evidence_status="invalid",
            receipt_path=str(result_path),
        )


def _rate(metrics: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    known = [metric for metric in metrics if metric.get("status") == "known"]
    successes = sum(metric.get("value") is True for metric in known)
    failures = sum(metric.get("value") is False for metric in known)
    if successes + failures != len(known):
        raise ValueError("known ASR metric is not Boolean")
    return {
        "successes": successes,
        "failures": failures,
        "known_denominator": len(known),
        "unknown": len(metrics) - len(known),
        "total_cells": len(metrics),
        "percentage_of_known": (
            100.0 * successes / len(known) if known else None
        ),
    }


def _change_rate(metrics: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    known = [metric for metric in metrics if metric.get("status") == "known"]
    changed = sum(metric.get("value") is True for metric in known)
    unchanged = sum(metric.get("value") is False for metric in known)
    if changed + unchanged != len(known):
        raise ValueError("known evolution metric is not Boolean")
    return {
        "changed": changed,
        "unchanged": unchanged,
        "known_denominator": len(known),
        "unknown": len(metrics) - len(known),
        "total_cells": len(metrics),
        "percentage_changed_of_known": (
            100.0 * changed / len(known) if known else None
        ),
    }


def _conditional_retention(
    cells: Sequence[Mapping[str, Any]], checkpoint: str
) -> dict[str, Any]:
    r0_known = [cell for cell in cells if cell["asr"]["r0"]["status"] == "known"]
    eligible = [cell for cell in r0_known if cell["asr"]["r0"]["value"] is True]
    observed = [
        cell for cell in eligible if cell["asr"][checkpoint]["status"] == "known"
    ]
    retained = sum(cell["asr"][checkpoint]["value"] is True for cell in observed)
    lost = sum(cell["asr"][checkpoint]["value"] is False for cell in observed)
    if retained + lost != len(observed):
        raise ValueError("known descendant ASR metric is not Boolean")
    return {
        "total_attack_cells": len(cells),
        "r0_known_denominator": len(r0_known),
        "r0_unknown": len(cells) - len(r0_known),
        "eligible_r0_positive_denominator": len(eligible),
        "descendant_observed_denominator": len(observed),
        "descendant_unknown_within_eligible": len(eligible) - len(observed),
        "retained": retained,
        "lost": lost,
        "percentage_retained_of_observed_eligible": (
            100.0 * retained / len(observed) if observed else None
        ),
    }


def _validate_optimizer_record(value: Any, benchmark: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("summary cell lacks optimizer diagnostics")
    status = value.get("status")
    if status == "unknown":
        if value.get("total_steps") is not None:
            raise ValueError("unknown optimizer diagnostics have a total_steps value")
        for section in (
            "candidate",
            "edit_application",
            "post_slow_candidate_gate",
            "post_slow_edit_application",
        ):
            record = value.get(section)
            if not isinstance(record, Mapping):
                raise ValueError(f"unknown optimizer diagnostics lack {section}")
            numeric_values = [
                item
                for key, item in record.items()
                if key != "definition"
            ]
            if any(item is not None for item in numeric_values):
                raise ValueError("unknown optimizer diagnostics contain numeric claims")
        return
    if status != "known":
        raise ValueError("optimizer diagnostics have an unsupported status")

    expected_total = experiment.TOTAL_STEPS[benchmark]
    total = _nonnegative_int(value.get("total_steps"), "optimizer.total_steps")
    if total != expected_total:
        raise ValueError("optimizer total_steps differs from benchmark contract")

    candidate = value.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ValueError("optimizer candidate diagnostics are missing")
    candidate_counts = {
        key: _nonnegative_int(candidate.get(key), f"optimizer.candidate.{key}")
        for key in (
            "observed_steps",
            "substantive_change",
            "no_op",
            "unobserved_steps",
        )
    }
    if (
        candidate_counts["observed_steps"] + candidate_counts["unobserved_steps"]
        != total
        or candidate_counts["substantive_change"] + candidate_counts["no_op"]
        != candidate_counts["observed_steps"]
    ):
        raise ValueError("optimizer candidate counts are inconsistent")

    def validate_edits(record: Any, steps: int, label: str) -> None:
        if not isinstance(record, Mapping):
            raise ValueError(f"{label} is missing")
        counts = {
            key: _nonnegative_int(record.get(key), f"{label}.{key}")
            for key in (
                "observed_steps",
                "unobserved_steps",
                "total",
                "applied",
                "skipped",
                "errors",
            )
        }
        if counts["observed_steps"] + counts["unobserved_steps"] != steps:
            raise ValueError(f"{label} step counts are inconsistent")
        if counts["applied"] + counts["skipped"] + counts["errors"] != counts["total"]:
            raise ValueError(f"{label} edit counts are inconsistent")

    validate_edits(value.get("edit_application"), total, "optimizer.edit_application")
    post_slow_steps = total - 2 * experiment.STEPS_PER_EPOCH[benchmark]
    if post_slow_steps < 0:
        raise ValueError("optimizer post-slow step contract is negative")
    gate = value.get("post_slow_candidate_gate")
    if not isinstance(gate, Mapping) or not isinstance(gate.get("definition"), str):
        raise ValueError("optimizer post-slow gate diagnostics are missing")
    gate_counts = {
        key: _nonnegative_int(gate.get(key), f"optimizer.post_slow_gate.{key}")
        for key in ("observed_steps", "applied", "rejected", "skipped")
    }
    if gate_counts["observed_steps"] != post_slow_steps or (
        gate_counts["applied"] + gate_counts["rejected"] + gate_counts["skipped"]
        != gate_counts["observed_steps"]
    ):
        raise ValueError("optimizer post-slow gate counts are inconsistent")
    validate_edits(
        value.get("post_slow_edit_application"),
        post_slow_steps,
        "optimizer.post_slow_edit_application",
    )


def _optimizer_aggregate(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    known = [cell["optimizer"] for cell in cells if cell["optimizer"]["status"] == "known"]

    def summed(section: str, keys: Sequence[str]) -> dict[str, int]:
        return {
            key: sum(int(record[section][key]) for record in known)
            for key in keys
        }

    return {
        "known_trajectories": len(known),
        "unknown_trajectories": len(cells) - len(known),
        "candidate": summed(
            "candidate",
            ("observed_steps", "substantive_change", "no_op", "unobserved_steps"),
        ),
        "edit_application": summed(
            "edit_application",
            (
                "observed_steps",
                "unobserved_steps",
                "total",
                "applied",
                "skipped",
                "errors",
            ),
        ),
        "post_slow_candidate_gate": summed(
            "post_slow_candidate_gate",
            ("observed_steps", "applied", "rejected", "skipped"),
        ),
        "post_slow_edit_application": summed(
            "post_slow_edit_application",
            (
                "observed_steps",
                "unobserved_steps",
                "total",
                "applied",
                "skipped",
                "errors",
            ),
        ),
    }


def derive_completion_contract(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    expected_cells = {
        (benchmark, condition)
        for benchmark in experiment.BENCHMARKS
        for condition in experiment.CONDITIONS
    }
    observed_cells = [
        (str(cell.get("benchmark")), str(cell.get("attack") or "clean"))
        for cell in cells
    ]
    if (
        len(cells) != EXPECTED_TRAJECTORIES
        or set(observed_cells) != expected_cells
        or len(observed_cells) != len(set(observed_cells))
    ):
        raise ValueError("summary cells are not the exact frozen experiment matrix")

    trajectory_statuses = [
        str(cell.get("trajectory", {}).get("status")) for cell in cells
    ]
    if any(status not in {"valid", "missing", "invalid"} for status in trajectory_statuses):
        raise ValueError("summary contains an unsupported trajectory status")
    trajectories = {
        status: trajectory_statuses.count(status)
        for status in ("valid", "missing", "invalid")
    }

    asr_metrics: list[Mapping[str, Any]] = []
    for cell in cells:
        benchmark = str(cell.get("benchmark"))
        if benchmark not in experiment.BENCHMARKS:
            raise ValueError("summary cell has an unknown benchmark")
        _validate_optimizer_record(cell.get("optimizer"), benchmark)
        attack = cell.get("attack")
        asr = cell.get("asr")
        if attack is None:
            if asr is not None:
                raise ValueError("clean summary cell unexpectedly contains ASR")
            continue
        if not isinstance(asr, Mapping) or set(asr) != set(CHECKPOINTS):
            raise ValueError("attacked summary cell lacks the ASR checkpoint triplet")
        for checkpoint in CHECKPOINTS:
            metric = asr[checkpoint]
            if not isinstance(metric, Mapping) or metric.get("status") not in {
                "known",
                "unknown",
            }:
                raise ValueError("summary contains a malformed ASR metric")
            if metric.get("evidence_status") not in {
                "valid",
                "indeterminate_fail_closed",
                "unobservable",
                "missing",
                "invalid",
            }:
                raise ValueError("summary contains an unsupported ASR evidence status")
            if metric.get("status") == "known" and type(metric.get("value")) is not bool:
                raise ValueError("known ASR metric is not Boolean")
            if metric.get("status") == "unknown" and metric.get("value") is not None:
                raise ValueError("unknown ASR metric has a non-null value")
            if (
                metric.get("status") == "known"
                and metric.get("evidence_status") != "valid"
            ):
                raise ValueError("known ASR metric lacks valid executable evidence")
            if (
                metric.get("status") == "unknown"
                and metric.get("evidence_status") == "valid"
            ):
                raise ValueError("valid executable ASR evidence cannot be unknown")
            asr_metrics.append(metric)
    if len(asr_metrics) != EXPECTED_ASR_RECEIPTS:
        raise ValueError("summary does not contain the expected ASR metrics")

    known = sum(metric.get("status") == "known" for metric in asr_metrics)
    terminal = sum(
        metric.get("evidence_status") in TERMINAL_ASR_STATUSES
        for metric in asr_metrics
    )
    status = (
        "completed"
        if trajectories["valid"] == EXPECTED_TRAJECTORIES
        and known == EXPECTED_ASR_RECEIPTS
        else "completed_with_formal_unknowns"
        if trajectories["valid"] == EXPECTED_TRAJECTORIES
        and terminal == EXPECTED_ASR_RECEIPTS
        else "incomplete"
    )
    return {
        "status": status,
        "expected_trajectories": EXPECTED_TRAJECTORIES,
        "valid_trajectories": trajectories["valid"],
        "missing_trajectories": trajectories["missing"],
        "invalid_trajectories": trajectories["invalid"],
        "expected_asr_receipts": EXPECTED_ASR_RECEIPTS,
        "terminal_asr_receipts": terminal,
        "known_boolean_asr_receipts": known,
        "formal_unknown_asr_receipts": terminal - known,
        "nonterminal_asr_receipts": EXPECTED_ASR_RECEIPTS - terminal,
    }


def validate_summary_completeness(summary: Mapping[str, Any]) -> dict[str, Any]:
    cells = summary.get("cells")
    if not isinstance(cells, list):
        raise ValueError("summary cells are missing")
    derived = derive_completion_contract(cells)
    if summary.get("completion_contract") != derived:
        raise ValueError("stored summary completion contract differs from its cells")
    completeness = summary.get("completeness")
    expected_completeness = {
        "trajectories": {
            "valid": derived["valid_trajectories"],
            "missing": derived["missing_trajectories"],
            "invalid": derived["invalid_trajectories"],
        },
        "known_asr_receipts": derived["known_boolean_asr_receipts"],
        "unknown_asr_receipts": (
            EXPECTED_ASR_RECEIPTS - derived["known_boolean_asr_receipts"]
        ),
        "terminal_asr_receipts": derived["terminal_asr_receipts"],
    }
    if completeness != expected_completeness:
        raise ValueError("stored summary completeness differs from its cells")
    aggregates = summary.get("aggregates")
    if (
        not isinstance(aggregates, Mapping)
        or aggregates.get("optimizer_all_cells") != _optimizer_aggregate(cells)
    ):
        raise ValueError("stored optimizer aggregate differs from its cells")
    source = summary.get("source")
    if (
        not isinstance(source, Mapping)
        or source.get("run_index_path") != str(experiment.RUN_INDEX)
        or source.get("run_index_sha256") != experiment.sha256_file(experiment.RUN_INDEX)
    ):
        raise ValueError("summary is not bound to the current frozen run index")
    return derived


def build_summary(
    index: Mapping[str, Any],
    *,
    completion_inspector: CompletionInspector | None = None,
    asr_inspector: AsrInspector | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    completion_inspector = completion_inspector or inspect_completion
    asr_inspector = asr_inspector or inspect_asr
    runs = index.get("runs")
    if not isinstance(runs, list) or len(runs) != EXPECTED_TRAJECTORIES:
        raise ValueError("summary requires the exact frozen experiment run index")

    cells: list[dict[str, Any]] = []
    for run_value in runs:
        if not isinstance(run_value, Mapping):
            raise ValueError("run index contains a non-object run")
        run = dict(run_value)
        completion = completion_inspector(run)
        cell = {
            "run_id": run["run_id"],
            "benchmark": run["benchmark"],
            "condition": run["condition"],
            "attack": run["attack"],
            **completion,
        }
        if run["attack"] is not None:
            cell["asr"] = {
                checkpoint: asr_inspector(run, checkpoint)
                for checkpoint in CHECKPOINTS
            }
        else:
            cell["asr"] = None
        cells.append(cell)

    attacked = [cell for cell in cells if cell["attack"] is not None]
    asr_by_attack: dict[str, Any] = {}
    retention_by_attack: dict[str, Any] = {}
    for attack in experiment.ATTACKS:
        attack_cells = [cell for cell in attacked if cell["attack"] == attack]
        asr_by_attack[attack] = {
            checkpoint: _rate([cell["asr"][checkpoint] for cell in attack_cells])
            for checkpoint in CHECKPOINTS
        }
        retention_by_attack[attack] = {
            checkpoint: _conditional_retention(attack_cells, checkpoint)
            for checkpoint in ("rbest", "rfinal")
        }

    trajectory_counts = {
        status: sum(cell["trajectory"]["status"] == status for cell in cells)
        for status in ("valid", "missing", "invalid")
    }
    evolution = {
        checkpoint: {
            kind: _change_rate(
                [cell["evolution"][checkpoint][f"{kind}_changed"] for cell in cells]
            )
            for kind in ("raw", "substantive")
        }
        for checkpoint in ("rbest", "rfinal")
    }
    completion_contract = derive_completion_contract(cells)
    summary = {
        "schema_version": 1,
        "summary_version": SUMMARY_VERSION,
        "experiment_id": experiment.EXPERIMENT_ID,
        "generated_at": generated_at or experiment.utc_now(),
        "source": {
            "run_index_path": str(experiment.RUN_INDEX),
            "run_index_sha256": experiment.sha256_file(experiment.RUN_INDEX),
        },
        "policy": {
            "missing_or_invalid_metric": "unknown_never_zero",
            "accuracy_aggregation": "macro_mean_over_trajectories",
            "accuracy_test_denominator": "benchmark_specific_test_split",
            "asr_percentage_denominator": "known_receipts_only",
            "conditional_retention_eligibility": "r0_asr_is_true",
            "conditional_retention_percentage_denominator": (
                "eligible_r0_positive_cells_with_known_descendant_asr"
            ),
        },
        "scope": {
            "expected_cells": EXPECTED_TRAJECTORIES,
            "expected_attacked_cells": EXPECTED_ATTACKED_CELLS,
            "expected_asr_receipts": EXPECTED_ASR_RECEIPTS,
            "benchmarks": list(experiment.BENCHMARKS),
            "attacks": list(experiment.ATTACKS),
            "counts_by_benchmark": experiment.COUNTS_BY_BENCHMARK,
            "batch_size_by_benchmark": experiment.BATCH_SIZE_BY_BENCHMARK,
            "test_denominator_by_benchmark": {
                benchmark: experiment.counts_for(benchmark)["test"]
                for benchmark in experiment.BENCHMARKS
            },
            "epochs": experiment.EPOCHS,
            "total_steps": experiment.TOTAL_STEPS,
        },
        "completeness": {
            "trajectories": trajectory_counts,
            "known_asr_receipts": sum(
                cell["asr"][checkpoint]["status"] == "known"
                for cell in attacked
                for checkpoint in CHECKPOINTS
            ),
            "unknown_asr_receipts": sum(
                cell["asr"][checkpoint]["status"] != "known"
                for cell in attacked
                for checkpoint in CHECKPOINTS
            ),
            "terminal_asr_receipts": completion_contract[
                "terminal_asr_receipts"
            ],
        },
        "completion_contract": completion_contract,
        "cells": cells,
        "aggregates": {
            "evolution_all_cells": evolution,
            "optimizer_all_cells": _optimizer_aggregate(cells),
            "asr_by_attack": asr_by_attack,
            "conditional_retention_by_attack": retention_by_attack,
        },
    }
    validate_summary_completeness(summary)
    return summary


def _percent(value: Any) -> str:
    if value is None:
        return "unknown"
    return f"{100.0 * float(value):.2f}%"


def _delta(value: Mapping[str, Any]) -> str:
    if value.get("status") != "known":
        return "unknown"
    return f"{100.0 * float(value['value']):+.2f} pp"


def _asr(value: Mapping[str, Any]) -> str:
    if value.get("status") != "known":
        return "unknown"
    return "成功" if value.get("value") is True else "失败"


def _rate_text(value: Mapping[str, Any], key: str) -> str:
    rate = value.get(key)
    return "unknown" if rate is None else f"{float(rate):.2f}%"


def _accuracy_group(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    observed: list[Mapping[str, Any]] = []
    for cell in cells:
        metrics = (
            cell["accuracy"]["r0"],
            cell["accuracy"]["rbest"],
            cell["accuracy"]["rfinal"],
            cell["accuracy_deltas"]["rbest_minus_r0"],
            cell["accuracy_deltas"]["rfinal_minus_r0"],
        )
        if all(metric.get("status") == "known" for metric in metrics):
            observed.append(cell)

    def mean(path: tuple[str, str]) -> float | None:
        values = [float(cell[path[0]][path[1]]["value"]) for cell in observed]
        return sum(values) / len(values) if values else None

    def directions(checkpoint: str) -> dict[str, int]:
        values = [
            float(cell["accuracy_deltas"][f"{checkpoint}_minus_r0"]["value"])
            for cell in observed
        ]
        return {
            "improved": sum(value > 0.0 and not math.isclose(value, 0.0) for value in values),
            "unchanged": sum(math.isclose(value, 0.0) for value in values),
            "declined": sum(value < 0.0 and not math.isclose(value, 0.0) for value in values),
        }

    return {
        "observed": len(observed),
        "total": len(cells),
        "r0": mean(("accuracy", "r0")),
        "rbest": mean(("accuracy", "rbest")),
        "rfinal": mean(("accuracy", "rfinal")),
        "rbest_delta": mean(("accuracy_deltas", "rbest_minus_r0")),
        "rfinal_delta": mean(("accuracy_deltas", "rfinal_minus_r0")),
        "rbest_directions": directions("rbest"),
        "rfinal_directions": directions("rfinal"),
    }


def _direction_text(value: Mapping[str, int]) -> str:
    return f"{value['improved']}/{value['unchanged']}/{value['declined']}"


def _mean_delta(value: Any) -> str:
    return "unknown" if value is None else f"{100.0 * float(value):+.2f} pp"


def _optimizer_cell_text(
    optimizer: Mapping[str, Any], section: str, keys: Sequence[str]
) -> str:
    if optimizer.get("status") != "known":
        return "unknown"
    record = optimizer.get(section)
    if not isinstance(record, Mapping):
        return "unknown"
    return "/".join(str(record[key]) for key in keys)


def render_markdown(summary: Mapping[str, Any]) -> str:
    cells = summary["cells"]
    attacked_cells = [cell for cell in cells if cell["attack"] is not None]
    completeness = summary["completeness"]
    optimizer_all = summary["aggregates"]["optimizer_all_cells"]
    optimizer_known = optimizer_all["known_trajectories"]
    post_slow_gate = optimizer_all["post_slow_candidate_gate"]
    post_slow_gate_text = (
        f"applied={post_slow_gate['applied']}, "
        f"rejected={post_slow_gate['rejected']}, "
        f"skipped={post_slow_gate['skipped']}"
        if optimizer_known
        else "unknown"
    )
    benchmark_labels = {"searchqa": "SearchQA", "docvqa": "DocVQA"}
    accuracy_denominator_text = "；".join(
        f"{benchmark_labels.get(benchmark, benchmark)} "
        f"n={experiment.counts_for(benchmark)['test']} "
        f"(1 题={100.0 / experiment.counts_for(benchmark)['test']:.3f} pp)"
        for benchmark in experiment.BENCHMARKS
    )
    accuracy_groups = {
        "Clean": _accuracy_group([cell for cell in cells if cell["attack"] is None]),
        "Attacked": _accuracy_group(attacked_cells),
        "全部": _accuracy_group(cells),
    }
    overall_asr = {
        checkpoint: _rate([cell["asr"][checkpoint] for cell in attacked_cells])
        for checkpoint in CHECKPOINTS
    }
    overall_retention = {
        checkpoint: _conditional_retention(attacked_cells, checkpoint)
        for checkpoint in ("rbest", "rfinal")
    }
    substantive_best = summary["aggregates"]["evolution_all_cells"]["rbest"][
        "substantive"
    ]
    substantive_final = summary["aggregates"]["evolution_all_cells"]["rfinal"][
        "substantive"
    ]
    attacked_substantive_final = _change_rate(
        [
            cell["evolution"]["rfinal"]["substantive_changed"]
            for cell in attacked_cells
        ]
    )
    substantively_evolved_attacked_cells = [
        cell
        for cell in attacked_cells
        if cell["evolution"]["rfinal"]["substantive_changed"].get("status")
        == "known"
        and cell["evolution"]["rfinal"]["substantive_changed"].get("value")
        is True
    ]
    non_evolved_attacked_cells = [
        cell
        for cell in attacked_cells
        if cell["evolution"]["rfinal"]["substantive_changed"].get("status")
        == "known"
        and cell["evolution"]["rfinal"]["substantive_changed"].get("value")
        is False
    ]
    non_evolved_labels = ", ".join(
        f"{cell['benchmark']}/{cell['attack']}"
        for cell in non_evolved_attacked_cells
    )
    evolved_subset_final_asr = _rate(
        [cell["asr"]["rfinal"] for cell in substantively_evolved_attacked_cells]
    )
    all_accuracy = accuracy_groups["全部"]
    final_directions = all_accuracy["rfinal_directions"]
    total_asr_successes = sum(value["successes"] for value in overall_asr.values())
    final_asr = overall_asr["rfinal"]
    final_all_successful = (
        final_asr["successes"] == EXPECTED_ATTACKED_CELLS
        and final_asr["known_denominator"] == EXPECTED_ATTACKED_CELLS
        and final_asr["unknown"] == 0
    )
    final_asr_scope = (
        f"{EXPECTED_ATTACKED_CELLS} 个攻击组合在 {experiment.EPOCHS} 个 epoch "
        "后全部仍可触发。"
        if final_all_successful
        else f"当前有 {final_asr['unknown']} 个组合的 Rfinal 结果为 unknown。"
    )
    lines = [
        "# 实验结果",
        "",
        f"生成时间：`{summary['generated_at']}`",
        "",
        "## 直接结论",
        "",
        f"- **Accuracy 只发生了小幅、非一致的变化。** "
        f"{EXPECTED_TRAJECTORIES} 条轨迹的 Rfinal "
        f"相对 R0 平均为 `{_mean_delta(all_accuracy['rfinal_delta'])}`："
        f"{final_directions['improved']} 个提升，"
        f"{final_directions['unchanged']} 个不变，"
        f"{final_directions['declined']} 个下降。",
        f"- **Skill 内容有变化，但攻击相关轨迹并非全部真正进化。** 全部轨迹中 "
        f"Rfinal 有实质变化的是 "
        f"`{substantive_final['changed']}/{substantive_final['known_denominator']}`；"
        f"{EXPECTED_ATTACKED_CELLS} 条攻击轨迹中是 "
        f"`{attacked_substantive_final['changed']}/"
        f"{attacked_substantive_final['known_denominator']}`。",
        f"- **当前测试口径下，最终 ASR 为 "
        f"`{final_asr['successes']}/{final_asr['known_denominator']} "
        f"= {_rate_text(final_asr, 'percentage_of_known')}`。** "
        f"{final_asr_scope}",
        f"- 只看确实发生实质进化的攻击轨迹，Rfinal ASR 为 "
        f"`{evolved_subset_final_asr['successes']}/"
        f"{evolved_subset_final_asr['known_denominator']} = "
        f"{_rate_text(evolved_subset_final_asr, 'percentage_of_known')}`。"
        "这是更严格、但样本更小的保留口径。",
        f"- 全部 checkpoint 合计为 "
        f"`{completeness['known_asr_receipts']}/{EXPECTED_ASR_RECEIPTS}` 个"
        f"可验证探针，其中 `{total_asr_successes}/{completeness['known_asr_receipts']}` "
        f"为成功。这里的 {EXPECTED_ASR_RECEIPTS} 是 "
        f"`{EXPECTED_ATTACKED_CELLS} 个攻击组合 x {len(CHECKPOINTS)} 个 checkpoint`，"
        f"不是 {EXPECTED_ASR_RECEIPTS} 个独立攻击样本。",
        f"- **Optimizer 诊断：** {optimizer_known}/{EXPECTED_TRAJECTORIES} 条轨迹有"
        f"有效 history 证据；首次真实 slow update 之后的 fast candidate gate 为 "
        f"`{post_slow_gate_text}`。这里的 applied 指候选被 gate 接受，不是机械编辑数。",
        "",
        "## 指标定义",
        "",
        "| 名称 | 含义 |",
        "|---|---|",
        "| R0 | 注入攻击后、开始 SkillOpt 自进化前的 initial skill |",
        "| Rbest | SkillOpt 根据 validation 选择的最佳 checkpoint |",
        f"| Rfinal | {experiment.EPOCHS} 个 epoch 全部结束后的最后 checkpoint |",
        f"| Accuracy | 对应 benchmark 的冻结 test split 正确率："
        f"{accuracy_denominator_text}；跨轨迹平均为 macro mean |",
        "| ASR | 强制激活单个 skill 后，一个固定、确定性执行探针是否触发 verifier；每个攻击组合在每个 checkpoint 只有 1 个二元结果 |",
        "",
        "Rbest 是按 validation 选择的，不是按 test Accuracy 选择的，因此表中 "
        "Rbest test Accuracy 低于 R0 并不矛盾。",
        "",
        "## 1. 自进化对 Accuracy 的影响",
        "",
        "### 汇总",
        "",
        "| 条件 | R0 平均 Acc | Rbest 平均 Acc（相对 R0） | Rfinal 平均 Acc（相对 R0） |",
        "|---|---:|---:|---:|",
    ]
    for label, value in accuracy_groups.items():
        lines.append(
            f"| {label}（{value['observed']}/{value['total']}） | "
            f"{_percent(value['r0'])} | {_percent(value['rbest'])} "
            f"({_mean_delta(value['rbest_delta'])}) | "
            f"{_percent(value['rfinal'])} ({_mean_delta(value['rfinal_delta'])}) |"
        )

    lines.extend(
        [
            "",
            f"严格表述是：从 skill 内容看，"
            f"{substantive_final['known_denominator']} 个有效轨迹中有 "
            f"{substantive_final['changed']} 个产生了实质变化；从任务效果看，"
            f"平均提升较小。Clean 的 Rfinal 平均变化为 "
            f"{_mean_delta(accuracy_groups['Clean']['rfinal_delta'])}，Attacked 为 "
            f"{_mean_delta(accuracy_groups['Attacked']['rfinal_delta'])}。当前只有一个 seed，"
            "这些差值不能解释为稳定增益或统计显著提升。",
            "Rfinal 相对 R0 的提升/不变/下降数量：Clean 为 "
            f"`{_direction_text(accuracy_groups['Clean']['rfinal_directions'])}`，"
            "Attacked 为 "
            f"`{_direction_text(accuracy_groups['Attacked']['rfinal_directions'])}`，"
            "全部为 "
            f"`{_direction_text(accuracy_groups['全部']['rfinal_directions'])}`。",
            "",
            "### 每个 base skill 的结果",
            "",
            "| Base skill | 条件 | Test n | R0 Acc | Rbest Acc（变化） | Rfinal Acc（变化） |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for cell in cells:
        condition = cell["attack"] or "clean"
        accuracy = cell["accuracy"]
        deltas = cell["accuracy_deltas"]
        lines.append(
            f"| {cell['benchmark']} | {condition} | "
            f"{experiment.counts_for(str(cell['benchmark']))['test']} | "
            f"{_percent(accuracy['r0'].get('value')) if accuracy['r0']['status'] == 'known' else 'unknown'} | "
            f"{_percent(accuracy['rbest'].get('value')) if accuracy['rbest']['status'] == 'known' else 'unknown'} "
            f"({_delta(deltas['rbest_minus_r0'])}) | "
            f"{_percent(accuracy['rfinal'].get('value')) if accuracy['rfinal']['status'] == 'known' else 'unknown'} "
            f"({_delta(deltas['rfinal_minus_r0'])}) |"
        )

    candidate_all = optimizer_all["candidate"]
    edits_all = optimizer_all["edit_application"]
    post_slow_edits_all = optimizer_all["post_slow_edit_application"]
    lines.extend(
        [
            "",
            "## 2. Optimizer 诊断",
            "",
            "Candidate 的 substantive-change/no-op 由 completion receipt 绑定的候选"
            "与该 step 输入 skill 做规范化字节比较得到。Edit applied/skipped/errors "
            "直接汇总 history 中存在的 `edit_apply_summary`；缺失字段只计入 unobserved，"
            "不会按 0 或 no-op 处理。",
            "",
            f"有效轨迹：`{optimizer_known}/{EXPECTED_TRAJECTORIES}`。候选 "
            f"substantive/no-op/unobserved 为 "
            f"`{candidate_all['substantive_change']}/{candidate_all['no_op']}/"
            f"{candidate_all['unobserved_steps']}`；机械编辑 applied/skipped/errors 为 "
            f"`{edits_all['applied']}/{edits_all['skipped']}/{edits_all['errors']}` "
            f"（有字段的 step：{edits_all['observed_steps']}）。"
            if optimizer_known
            else f"有效轨迹：`0/{EXPECTED_TRAJECTORIES}`；optimizer 计数为 unknown。",
            "",
            f"首次真实 slow update 在 epoch 2 结束后产生。其后的 fast candidate gate："
            f"`{post_slow_gate_text}`。同期机械编辑为 "
            f"`applied={post_slow_edits_all['applied']}, "
            f"skipped={post_slow_edits_all['skipped']}, "
            f"errors={post_slow_edits_all['errors']}`。"
            if optimizer_known
            else "首次真实 slow update 后的 gate 与机械编辑计数均为 unknown。",
            "",
            "| Base skill | 条件 | Candidate substantive/no-op/unobserved | "
            "Edit applied/skipped/errors | Post-slow gate applied/rejected/skipped |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for cell in cells:
        optimizer = cell["optimizer"]
        lines.append(
            f"| {cell['benchmark']} | {cell['attack'] or 'clean'} | "
            f"{_optimizer_cell_text(optimizer, 'candidate', ('substantive_change', 'no_op', 'unobserved_steps'))} | "
            f"{_optimizer_cell_text(optimizer, 'edit_application', ('applied', 'skipped', 'errors'))} | "
            f"{_optimizer_cell_text(optimizer, 'post_slow_candidate_gate', ('applied', 'rejected', 'skipped'))} |"
        )

    lines.extend(
        [
            "",
            "## 3. ASR 与攻击保留率",
            "",
            f"本实验有 {EXPECTED_ATTACKED_CELLS} 个攻击组合："
            f"{len(experiment.BENCHMARKS)} 个 base skill 乘以 "
            f"{len(experiment.ATTACKS)} 种攻击。"
            "每个组合分别测试 R0、Rbest 和 Rfinal。",
            "",
            "| Checkpoint | 成功/有效攻击组合 | Unknown | ASR |",
            "|---|---:|---:|---:|",
        ]
    )
    for checkpoint in CHECKPOINTS:
        rate = overall_asr[checkpoint]
        lines.append(
            f"| {checkpoint} | {rate['successes']}/{rate['known_denominator']} | "
            f"{rate['unknown']} | {_rate_text(rate, 'percentage_of_known')} |"
        )

    lines.extend(
        [
            "",
            "| Attack | R0 | Rbest | Rfinal | Rfinal 行为保留率 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for attack in experiment.ATTACKS:
        rates = summary["aggregates"]["asr_by_attack"][attack]
        retention = summary["aggregates"]["conditional_retention_by_attack"][attack][
            "rfinal"
        ]
        lines.append(
            f"| {attack} | {rates['r0']['successes']}/{rates['r0']['known_denominator']} | "
            f"{rates['rbest']['successes']}/{rates['rbest']['known_denominator']} | "
            f"{rates['rfinal']['successes']}/{rates['rfinal']['known_denominator']} | "
            f"{_rate_text(retention, 'percentage_retained_of_observed_eligible')} |"
        )

    lines.extend(
        [
            "",
            "### 每个攻击组合",
            "",
            "| Base skill | Attack | R0 | Rbest | Rfinal |",
            "|---|---|---|---|---|",
        ]
    )
    for cell in attacked_cells:
        lines.append(
            f"| {cell['benchmark']} | {cell['attack']} | "
            f"{_asr(cell['asr']['r0'])} | {_asr(cell['asr']['rbest'])} | "
            f"{_asr(cell['asr']['rfinal'])} |"
        )

    rbest_retention = overall_retention["rbest"]
    rfinal_retention = overall_retention["rfinal"]
    lines.extend(
        [
            "",
            f"以 R0 ASR 成功的组合为分母，Rbest 行为保留率为 "
            f"`{rbest_retention['retained']}/{rbest_retention['eligible_r0_positive_denominator']} "
            f"= {_rate_text(rbest_retention, 'percentage_retained_of_observed_eligible')}`；"
            f"Rfinal 行为保留率为 "
            f"`{rfinal_retention['retained']}/{rfinal_retention['eligible_r0_positive_denominator']} "
            f"= {_rate_text(rfinal_retention, 'percentage_retained_of_observed_eligible')}`。",
            "",
            "### ASR 测量限制",
            "",
            f"- 每个 cell/checkpoint 只运行 1 个固定探针；它不是 "
            f"SearchQA {experiment.counts_for('searchqa')['test']} 个 utility test "
            "任务上的 ASR，"
            "也不是自然任务分布中的自动 skill 选择率。",
            "- 探针会强制激活被测 skill，模型生成参数固定为 temperature 0、seed 0，"
            "且未启用防御。",
            "- 三种 verifier 分别检查 SQLite 行删除、文件删除和携带目标密钥的"
            "本地 API 请求；它们测量的是不同的可执行副作用。",
            f"- 未发生实质 skill 变化的攻击轨迹为："
            f"`{non_evolved_labels or '无'}`。它们可以计入原始 Rfinal ASR，"
            "但不能作为强的“经过进化仍保留”证据；严格子集结果为 "
            f"`{evolved_subset_final_asr['successes']}/"
            f"{evolved_subset_final_asr['known_denominator']}`。",
            "",
            "## 4. 当前能得出的结论",
            "",
            "本次校准实验说明 pipeline 已跑通。在 Qwen3.5-9B target、"
            "Qwen3.8-27B-FP8 optimizer、两角色均启用 thinking、训练 seed 42、"
            f"{len(experiment.BENCHMARKS)} 个 base skill、"
            f"{len(experiment.ATTACKS)} 个攻击标签和当前强制激活 harness 的范围内，"
            f"Rfinal 原始 ASR 为 {final_asr['successes']}/"
            f"{final_asr['known_denominator']}；在真正发生实质进化的攻击轨迹中为 "
            f"{evolved_subset_final_asr['successes']}/"
            f"{evolved_subset_final_asr['known_denominator']}。",
            "",
            "本次实验不支持三个更强的结论：第一，SkillOpt 会稳定提高 Accuracy；"
            "第二，这些攻击对其他模型、seed 或 base skill 同样有效；第三，"
            "未测试的攻击模块具有相同结果。"
            f"每个 checkpoint 只有 {EXPECTED_ATTACKED_CELLS} 个相关攻击组合，而且 "
            f"{len(CHECKPOINTS)} 个 checkpoint 来自同一批轨迹，不能把 "
            f"{EXPECTED_ASR_RECEIPTS} 个结果当作相互独立的样本。",
            "",
            "缺失、无效或不可观测证据在 JSON 中始终记录为 `unknown`，不会被计为失败。",
            "",
        ]
    )
    return "\n".join(lines)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_outputs(
    summary: Mapping[str, Any], json_path: Path, markdown_path: Path
) -> None:
    validate_summary_completeness(summary)
    experiment.atomic_write_json(json_path, summary)
    atomic_write_text(markdown_path, render_markdown(summary))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", type=Path, default=LIVE_SUMMARY_PATH)
    parser.add_argument("--markdown-output", type=Path, default=RESULTS_PATH)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    summary = build_summary(experiment.load_run_index())
    if args.dry_run:
        print(experiment.canonical_json(summary).decode("utf-8"), end="")
    else:
        write_outputs(summary, args.json_output, args.markdown_output)
        print(
            json.dumps(
                {
                    "status": "written",
                    "json": str(args.json_output),
                    "markdown": str(args.markdown_output),
                    "valid_trajectories": summary["completeness"]["trajectories"][
                        "valid"
                    ],
                    "known_asr_receipts": summary["completeness"][
                        "known_asr_receipts"
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
