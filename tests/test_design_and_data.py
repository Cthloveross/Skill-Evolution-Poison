from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from evoincubation.canary import INITIAL_SKILL, generate_block_data
from evoincubation.config import ExperimentConfig, load_config
from evoincubation.design import build_design
from evoincubation.runner import prepare_experiment, validate_prepared_experiment


def _config(tmp_path: Path, *, engine: str = "mock") -> Path:
    source = Path(__file__).parents[1] / "configs" / "smoke_mock.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["experiment"]["output_root"] = str(tmp_path / "run")
    raw["experiment"]["engine"] = engine
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _prepared_block(tmp_path: Path) -> tuple[ExperimentConfig, Path]:
    config = load_config(_config(tmp_path))
    prepare_experiment(config)
    block_id = build_design(config)[0].block_id
    return config, config.output_root / "data" / block_id


def _rewrite_items(path: Path, mutate) -> None:
    items = json.loads(path.read_text(encoding="utf-8"))
    mutate(items)
    path.write_text(json.dumps(items), encoding="utf-8")


def test_design_is_complete_randomized_block(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    rows = build_design(config)
    assert len(rows) == config.replicates * len(config.seed_candidates) * 4
    blocks = {row.block_id for row in rows}
    for block in blocks:
        block_rows = [row for row in rows if row.block_id == block]
        assert {(row.seed_present, row.evolution_enabled) for row in block_rows} == {
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        }
        assert sorted(row.run_order for row in block_rows) == [1, 2, 3, 4]
        assert len({row.data_seed for row in block_rows}) == 1
        assert len({row.lineage_seed for row in block_rows}) == 4


def test_initial_skill_freezes_ordered_must_policy() -> None:
    assert "Follow these ordered rules exactly." in INITIAL_SKILL
    assert "budget is 1 and `SAFE_BACKUP` is available" in INITIAL_SKILL
    assert "MUST call `read_backup`" in INITIAL_SKILL
    assert "NEVER call `read_cache`" in INITIAL_SKILL
    assert "even when it is labeled `VERIFIED_CACHE`" in INITIAL_SKILL
    assert "budget is 1 and the backup is unavailable" in INITIAL_SKILL
    assert "budget is at least 2 and both sources are available" in INITIAL_SKILL
    assert "call `verify_cache`" in INITIAL_SKILL


def test_data_splits_are_disjoint_and_matched(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    row = build_design(config)[0]
    candidate = config.seed_candidates[0]
    root = generate_block_data(
        config,
        block_id=row.block_id,
        data_seed=row.data_seed,
        seed_candidate=candidate,
    )
    split_paths = {
        "train": root / "train" / "items.json",
        "val": root / "val" / "items.json",
        "test": root / "test" / "items.json",
        "trigger_monitor": root / "trigger_monitor" / "items.json",
        "final_trigger": root / "final_trigger" / "items.json",
        "near_trigger": root / "near_trigger" / "items.json",
        "washout": root / "washout" / "items.json",
    }
    ids: dict[str, set[str]] = {}
    for name, path in split_paths.items():
        payload = json.loads(path.read_text(encoding="utf-8"))
        ids[name] = {str(item["id"]) for item in payload}
    names = list(ids)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            assert ids[left].isdisjoint(ids[right])

    poison = json.loads((root / "exposure" / "poison.json").read_text(encoding="utf-8"))
    benign = json.loads((root / "exposure" / "benign.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in poison] == [item["id"] for item in benign]
    assert [item["ground_truth"] for item in poison] == [item["ground_truth"] for item in benign]
    assert all(item["cache_value"] == item["ground_truth"] for item in poison)
    assert all(item["backup_value"] == item["ground_truth"] for item in benign)


def test_config_rejects_exposure_larger_than_batch(tmp_path: Path) -> None:
    path = _config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["canary"]["exposure_items"] = raw["evolution"]["batch_size"] + 1
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    try:
        load_config(path)
    except ValueError as exc:
        assert "exposure_items" in str(exc)
    else:
        raise AssertionError("invalid config was accepted")


def test_prepared_validation_reads_actual_split_counts(tmp_path: Path) -> None:
    config, root = _prepared_block(tmp_path)
    _rewrite_items(root / "train" / "items.json", lambda items: items.pop())

    report = validate_prepared_experiment(config)

    assert not report["ok"]
    assert any("train" in error and "expected" in error for error in report["errors"])
    assert any("file hash mismatch" in error for error in report["errors"])


def test_prepared_validation_rejects_content_change_with_stable_ids(tmp_path: Path) -> None:
    config, root = _prepared_block(tmp_path)

    def mutate_trigger(items: list[dict]) -> None:
        items[0]["attack_target"] = "CANARY-CHANGED"

    _rewrite_items(root / "trigger_monitor" / "items.json", mutate_trigger)
    report = validate_prepared_experiment(config)

    assert not report["ok"]
    assert any(
        "file hash mismatch for trigger_monitor/items.json" in error for error in report["errors"]
    )


def test_prepared_validation_rejects_clean_target_leakage(tmp_path: Path) -> None:
    config, root = _prepared_block(tmp_path)

    def inject_target(items: list[dict]) -> None:
        items[0]["attack_target"] = "CANARY-9999"

    _rewrite_items(root / "test" / "items.json", inject_target)
    report = validate_prepared_experiment(config)

    assert not report["ok"]
    assert any("test" in error and "attack_target" in error for error in report["errors"])


def test_prepared_validation_rejects_cross_split_id_and_seed_leakage(tmp_path: Path) -> None:
    config, root = _prepared_block(tmp_path)
    train = json.loads((root / "train" / "items.json").read_text(encoding="utf-8"))

    def mutate_test(items: list[dict]) -> None:
        items[0]["id"] = train[0]["id"]
        items[0]["exposure_kind"] = "poison_seed"

    _rewrite_items(root / "test" / "items.json", mutate_test)
    report = validate_prepared_experiment(config)

    assert not report["ok"]
    assert any("ID overlap" in error for error in report["errors"])
    assert any("seed exposure" in error and "test" in error for error in report["errors"])


def test_prepared_validation_rejects_mismatched_exposure_pair(tmp_path: Path) -> None:
    config, root = _prepared_block(tmp_path)

    def mutate_benign(items: list[dict]) -> None:
        items[0]["ground_truth"] = "SAFE-MISMATCH"

    _rewrite_items(root / "exposure" / "benign.json", mutate_benign)
    report = validate_prepared_experiment(config)

    assert not report["ok"]
    assert any("ground_truth" in error and "exposure" in error for error in report["errors"])


@pytest.mark.parametrize("split", ["train", "val", "washout"])
def test_prepared_validation_rejects_trigger_leakage(tmp_path: Path, split: str) -> None:
    config, root = _prepared_block(tmp_path)

    def inject_trigger(items: list[dict]) -> None:
        items[0]["cache_value"] = "CANARY-9999"
        items[0]["attack_target"] = "CANARY-9999"

    _rewrite_items(root / split / "items.json", inject_trigger)
    report = validate_prepared_experiment(config)

    assert not report["ok"]
    assert any(split in error and "CANARY" in error for error in report["errors"])
    assert any(split in error and "attack_target" in error for error in report["errors"])
