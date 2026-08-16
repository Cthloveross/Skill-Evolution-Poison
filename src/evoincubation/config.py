from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


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
    evolution["epochs"] = _positive_int(
        _require(evolution, "epochs", "evolution"), "evolution.epochs"
    )
    evolution["batch_size"] = _positive_int(
        _require(evolution, "batch_size", "evolution"), "evolution.batch_size"
    )
    evolution["washout_epochs"] = _positive_int(
        evolution.get("washout_epochs", 1), "evolution.washout_epochs", allow_zero=True
    )
    if evolution["washout_epochs"] >= evolution["epochs"]:
        raise ValueError("evolution.washout_epochs must be smaller than evolution.epochs")
    if canary["exposure_items"] > evolution["batch_size"]:
        raise ValueError("canary.exposure_items cannot exceed evolution.batch_size")
    if canary["train_items"] < evolution["batch_size"]:
        raise ValueError("canary.train_items must be at least evolution.batch_size")

    _require(model, "optimizer", "model")
    _require(model, "target", "model")
    _require(model, "optimizer_backend", "model")
    _require(model, "target_backend", "model")

    return ExperimentConfig(path=config_path, raw=raw)
