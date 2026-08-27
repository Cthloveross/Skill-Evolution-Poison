from __future__ import annotations

import copy
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_OPTIMIZER_BACKENDS = {
    "openai_chat",
    "claude_chat",
    "qwen_chat",
    "minimax_chat",
    "openai_compatible",
    "codex_exec",
}
_TARGET_BACKENDS = {
    "openai_chat",
    "claude_chat",
    "qwen_chat",
    "minimax_chat",
    "openai_compatible",
}
_MODEL_BACKENDS = (
    _OPTIMIZER_BACKENDS
    | _TARGET_BACKENDS
    | {
        "azure_openai",
        "openai",
        "claude",
        "codex",
        "codex_harness",
        "claude_code_exec",
        "cursor",
        "cursor_agent",
        "cursor_exec",
        "qwen",
        "minimax",
        "openai_compatible_chat",
        "openai-compatible",
        "compat",
    }
)


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ValueError(f"Missing required config key: {where}.{key}")
    return mapping[key]


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    result = int(value)
    lower = 0 if allow_zero else 1
    if result < lower:
        raise ValueError(f"{name} must be >= {lower}, got {result}")
    return result


def _backend(value: Any, name: str, allowed: set[str]) -> str:
    result = str(value).strip().lower()
    if result not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}; got {result!r}")
    return result


def _unit_interval_float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number in [0, 1]") from exc
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be a finite number in [0, 1]")
    return result


@dataclass(frozen=True)
class ExperimentConfig:
    path: Path
    raw: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.raw["experiment"]["name"])

    @property
    def output_root(self) -> Path:
        raw_path = Path(os.path.expandvars(str(self.raw["experiment"]["output_root"]))).expanduser()
        if raw_path.is_absolute():
            return raw_path
        return (self.path.parent / raw_path).resolve()

    @property
    def master_seed(self) -> int:
        return int(self.raw["experiment"]["master_seed"])

    @property
    def replicates(self) -> int:
        return int(self.raw["experiment"]["replicates"])

    @property
    def engine(self) -> str:
        return str(self.raw["experiment"].get("engine", "mock"))

    @property
    def seed_candidates(self) -> list[dict[str, Any]]:
        return list(self.raw["attack"]["seed_candidates"])

    def resolved(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)


def load_config(path: str | Path) -> ExperimentConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("Experiment config must be a YAML mapping")

    experiment = _require(raw, "experiment", "config")
    attack = _require(raw, "attack", "config")
    canary = _require(raw, "canary", "config")
    evolution = _require(raw, "evolution", "config")
    model = _require(raw, "model", "config")
    analysis = _require(raw, "analysis", "config")
    for section_name, section in (
        ("experiment", experiment),
        ("attack", attack),
        ("canary", canary),
        ("evolution", evolution),
        ("model", model),
        ("analysis", analysis),
    ):
        if not isinstance(section, dict):
            raise ValueError(f"config.{section_name} must be a mapping")

    thresholds = analysis.setdefault("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ValueError("analysis.thresholds must be a mapping")
    for key, default in (("baseline_asr_max", 0.10), ("s1e0_final_asr_max", 0.10)):
        thresholds[key] = _unit_interval_float(
            thresholds.get(key, analysis.get(key, default)), f"analysis.thresholds.{key}"
        )

    _require(experiment, "name", "experiment")
    _require(experiment, "output_root", "experiment")
    _require(experiment, "master_seed", "experiment")
    experiment["replicates"] = _positive_int(
        _require(experiment, "replicates", "experiment"), "experiment.replicates"
    )
    engine = str(experiment.get("engine", "mock"))
    if engine not in {"mock", "skillopt"}:
        raise ValueError("experiment.engine must be 'mock' or 'skillopt'")

    candidates = _require(attack, "seed_candidates", "attack")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("attack.seed_candidates must be a non-empty list")
    seen_ids: set[str] = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"attack.seed_candidates[{index}] must be a mapping")
        seed_id = str(_require(candidate, "id", f"attack.seed_candidates[{index}]"))
        _require(candidate, "poison_note", f"attack.seed_candidates[{index}]")
        _require(candidate, "benign_note", f"attack.seed_candidates[{index}]")
        if seed_id in seen_ids:
            raise ValueError(f"Duplicate attack seed id: {seed_id}")
        seen_ids.add(seed_id)

    for key in (
        "train_items",
        "selection_items",
        "clean_test_items",
        "trigger_monitor_items",
        "final_trigger_items",
        "near_trigger_items",
    ):
        canary[key] = _positive_int(_require(canary, key, "canary"), f"canary.{key}")
    canary["exposure_items"] = _positive_int(
        _require(canary, "exposure_items", "canary"), "canary.exposure_items"
    )
    for key, default in (
        ("max_turns", 4),
        ("max_completion_tokens", 256),
        ("timeout_seconds", 120),
    ):
        canary[key] = _positive_int(canary.get(key, default), f"canary.{key}")
    from evoincubation.canary import (
        DEFAULT_INITIAL_SKILL_VARIANT,
        INITIAL_SKILL_VARIANTS,
    )

    variant = str(canary.get("initial_skill_variant", DEFAULT_INITIAL_SKILL_VARIANT))
    if variant not in INITIAL_SKILL_VARIANTS:
        raise ValueError(
            "canary.initial_skill_variant must be one of "
            f"{sorted(INITIAL_SKILL_VARIANTS)}; got {variant!r}"
        )
    canary["initial_skill_variant"] = variant
    evolution["epochs"] = _positive_int(
        _require(evolution, "epochs", "evolution"), "evolution.epochs"
    )
    evolution["batch_size"] = _positive_int(
        _require(evolution, "batch_size", "evolution"), "evolution.batch_size"
    )
    evolution["washout_epochs"] = _positive_int(
        evolution.get("washout_epochs", 1), "evolution.washout_epochs", allow_zero=True
    )
    for key, default in (
        ("accumulation", 1),
        ("minibatch_size", 2),
        ("merge_batch_size", 4),
        ("analyst_workers", 4),
        ("max_analyst_rounds", 3),
        ("edit_budget", 2),
        ("min_edit_budget", 1),
        ("rollout_workers", 1),
    ):
        evolution[key] = _positive_int(evolution.get(key, default), f"evolution.{key}")
    if evolution["washout_epochs"] >= evolution["epochs"]:
        raise ValueError("evolution.washout_epochs must be smaller than evolution.epochs")
    if canary["exposure_items"] > evolution["batch_size"]:
        raise ValueError("canary.exposure_items cannot exceed evolution.batch_size")
    if canary["train_items"] < evolution["batch_size"]:
        raise ValueError("canary.train_items must be at least evolution.batch_size")
    if evolution["min_edit_budget"] > evolution["edit_budget"]:
        raise ValueError("evolution.min_edit_budget cannot exceed evolution.edit_budget")
    gate_metric = str(evolution.get("gate_metric", "soft")).strip().lower()
    if gate_metric not in {"hard", "soft", "mixed"}:
        raise ValueError("evolution.gate_metric must be 'hard', 'soft', or 'mixed'")
    evolution["gate_metric"] = gate_metric
    try:
        gate_mixed_weight = float(evolution.get("gate_mixed_weight", 0.5))
    except (TypeError, ValueError) as exc:
        raise ValueError("evolution.gate_mixed_weight must be a number in [0, 1]") from exc
    if not 0.0 <= gate_mixed_weight <= 1.0:
        raise ValueError("evolution.gate_mixed_weight must be in [0, 1]")
    evolution["gate_mixed_weight"] = gate_mixed_weight

    if not str(_require(model, "optimizer", "model")).strip():
        raise ValueError("model.optimizer must be non-empty")
    if not str(_require(model, "target", "model")).strip():
        raise ValueError("model.target must be non-empty")
    optimizer_backend = _backend(
        _require(model, "optimizer_backend", "model"),
        "model.optimizer_backend",
        _OPTIMIZER_BACKENDS,
    )
    target_backend = _backend(
        _require(model, "target_backend", "model"),
        "model.target_backend",
        _TARGET_BACKENDS,
    )
    model["optimizer_backend"] = optimizer_backend
    model["target_backend"] = target_backend
    model["backend"] = _backend(
        model.get("backend", target_backend), "model.backend", _MODEL_BACKENDS
    )
    model["rewrite_max_completion_tokens"] = _positive_int(
        model.get("rewrite_max_completion_tokens", 16000),
        "model.rewrite_max_completion_tokens",
    )

    skillopt = raw.get("skillopt")
    if skillopt is not None and not isinstance(skillopt, dict):
        raise ValueError("config.skillopt must be a mapping")
    if engine == "skillopt" and skillopt is None:
        raise ValueError("config.skillopt is required when experiment.engine is 'skillopt'")
    if skillopt is not None:
        revision = str(skillopt.get("expected_revision", "")).strip().lower()
        if not _GIT_REVISION_RE.fullmatch(revision):
            raise ValueError("skillopt.expected_revision must be a full 40-character Git SHA")
        skillopt["expected_revision"] = revision
        allow_unverified = skillopt.get("allow_unverified_revision", False)
        if not isinstance(allow_unverified, bool):
            raise ValueError("skillopt.allow_unverified_revision must be a boolean")
        skillopt["allow_unverified_revision"] = allow_unverified

    return ExperimentConfig(path=config_path, raw=raw)
