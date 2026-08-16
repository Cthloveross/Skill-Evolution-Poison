from __future__ import annotations

import json
from pathlib import Path

import yaml

from evoincubation.canary import generate_block_data
from evoincubation.config import load_config
from evoincubation.design import build_design


def _config(tmp_path: Path, *, engine: str = "mock") -> Path:
    source = Path(__file__).parents[1] / "configs" / "smoke_mock.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["experiment"]["output_root"] = str(tmp_path / "run")
    raw["experiment"]["engine"] = engine
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


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
