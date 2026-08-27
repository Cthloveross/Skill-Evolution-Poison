from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evoincubation.config import load_config


def _raw_config() -> dict:
    source = Path(__file__).parents[1] / "configs" / "smoke_mock.yaml"
    return yaml.safe_load(source.read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _set_nested(raw: dict, dotted_key: str, value) -> None:
    section, key = dotted_key.split(".", 1)
    raw[section][key] = value


@pytest.mark.parametrize(
    ("dotted_key", "value"),
    [
        ("evolution.rollout_workers", 0),
        ("evolution.analyst_workers", 0),
        ("evolution.max_analyst_rounds", 0),
        ("canary.max_turns", 0),
        ("canary.max_completion_tokens", 0),
        ("canary.timeout_seconds", 0),
        ("model.rewrite_max_completion_tokens", 0),
        ("evolution.edit_budget", 0),
        ("evolution.min_edit_budget", 0),
    ],
)
def test_config_rejects_nonpositive_runtime_limits(
    tmp_path: Path, dotted_key: str, value: int
) -> None:
    raw = _raw_config()
    _set_nested(raw, dotted_key, value)

    with pytest.raises(ValueError, match=dotted_key):
        load_config(_write_config(tmp_path, raw))


@pytest.mark.parametrize("weight", [-0.01, 1.01])
def test_config_rejects_gate_weight_outside_unit_interval(tmp_path: Path, weight: float) -> None:
    raw = _raw_config()
    raw["evolution"]["gate_mixed_weight"] = weight

    with pytest.raises(ValueError, match="gate_mixed_weight"):
        load_config(_write_config(tmp_path, raw))


def test_config_rejects_unknown_gate_metric_and_invalid_edit_budget(tmp_path: Path) -> None:
    raw = _raw_config()
    raw["evolution"]["gate_metric"] = "unknown"
    with pytest.raises(ValueError, match="gate_metric"):
        load_config(_write_config(tmp_path, raw))

    raw = _raw_config()
    raw["evolution"]["min_edit_budget"] = raw["evolution"]["edit_budget"] + 1
    with pytest.raises(ValueError, match="min_edit_budget"):
        load_config(_write_config(tmp_path, raw))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("baseline_asr_max", -0.01),
        ("baseline_asr_max", 1.01),
        ("baseline_asr_max", float("nan")),
        ("s1e0_final_asr_max", -0.01),
        ("s1e0_final_asr_max", 1.01),
        ("s1e0_final_asr_max", float("inf")),
    ],
)
def test_config_rejects_invalid_absolute_asr_thresholds(
    tmp_path: Path, key: str, value: float
) -> None:
    raw = _raw_config()
    raw.setdefault("analysis", {}).setdefault("thresholds", {})[key] = value

    with pytest.raises(ValueError, match=key):
        load_config(_write_config(tmp_path, raw))


def test_config_materializes_absolute_asr_threshold_defaults(tmp_path: Path) -> None:
    raw = _raw_config()
    raw["analysis"].pop("thresholds", None)

    config = load_config(_write_config(tmp_path, raw))

    assert config.raw["analysis"]["thresholds"]["baseline_asr_max"] == 0.10
    assert config.raw["analysis"]["thresholds"]["s1e0_final_asr_max"] == 0.10


@pytest.mark.parametrize("key", ["backend", "optimizer_backend", "target_backend"])
def test_config_rejects_unknown_model_backend(tmp_path: Path, key: str) -> None:
    raw = _raw_config()
    raw["model"][key] = "unknown_backend"

    with pytest.raises(ValueError, match=key):
        load_config(_write_config(tmp_path, raw))


@pytest.mark.parametrize("expected_revision", ["", "not-a-sha", "f" * 39, "g" * 40])
def test_skillopt_config_requires_full_git_revision(tmp_path: Path, expected_revision: str) -> None:
    raw = _raw_config()
    raw["experiment"]["engine"] = "skillopt"
    raw["skillopt"] = {
        "expected_revision": expected_revision,
        "allow_unverified_revision": False,
    }

    with pytest.raises(ValueError, match="expected_revision"):
        load_config(_write_config(tmp_path, raw))


def test_skillopt_config_requires_revision_section(tmp_path: Path) -> None:
    raw = _raw_config()
    raw["experiment"]["engine"] = "skillopt"

    with pytest.raises(ValueError, match="skillopt"):
        load_config(_write_config(tmp_path, raw))
