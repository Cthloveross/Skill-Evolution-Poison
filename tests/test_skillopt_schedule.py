from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml

pytest.importorskip("skillopt")

from evoincubation.config import load_config
from evoincubation.skillopt_bridge import _flat_skillopt_config
from evoincubation.skillopt_env.adapter import CanaryAdapter
from evoincubation.skillopt_env.dataloader import CanaryDataLoader


def _write_split(root: Path, split: str, count: int) -> None:
    path = root / split
    path.mkdir(parents=True)
    items = [{"id": f"{split}-{index}", "exposure_kind": "clean"} for index in range(count)]
    (path / "items.json").write_text(json.dumps(items), encoding="utf-8")


def _write_schedule_data(tmp_path: Path) -> tuple[Path, Path]:
    for split, count in (("train", 8), ("val", 4), ("test", 4)):
        _write_split(tmp_path, split, count)
    exposure_path = tmp_path / "exposure.json"
    exposure_path.write_text(
        json.dumps(
            [
                {"id": "exposure-0", "exposure_kind": "poison_seed"},
                {"id": "exposure-1", "exposure_kind": "poison_seed"},
            ]
        ),
        encoding="utf-8",
    )
    washout_path = tmp_path / "washout.json"
    washout_path.write_text(
        json.dumps(
            [{"id": f"washout-{index}", "exposure_kind": "neutral_washout"} for index in range(8)]
        ),
        encoding="utf-8",
    )
    return exposure_path, washout_path


def test_e1_uses_full_schedule_and_exposure_occurs_once(tmp_path: Path) -> None:
    exposure_path, washout_path = _write_schedule_data(tmp_path)
    loader = CanaryDataLoader(
        split_dir=str(tmp_path),
        exposure_path=str(exposure_path),
        washout_path=str(washout_path),
        washout_start_epoch=3,
        seed=7,
        split_seed=7,
    )
    loader.setup({"out_root": str(tmp_path / "out"), "env": "canary"})

    steps_per_epoch = math.ceil(loader.get_train_size() / 4)
    epochs = [
        loader.plan_train_epoch(
            epoch=epoch,
            steps_per_epoch=steps_per_epoch,
            accumulation=1,
            batch_size=4,
            seed=7,
        )
        for epoch in range(1, 4)
    ]

    assert loader.get_train_size() == 8
    assert steps_per_epoch == 2
    assert [len(batches) for batches in epochs] == [2, 2, 2]
    assert (
        sum(
            item["exposure_kind"] in {"poison_seed", "matched_benign_seed"}
            for batches in epochs
            for batch in batches
            for item in batch.payload
        )
        == 2
    )
    assert not any(
        item["exposure_kind"] in {"poison_seed", "matched_benign_seed"}
        for batches in epochs[1:]
        for batch in batches
        for item in batch.payload
    )
    assert all(
        str(item["id"]).startswith("washout-") for batch in epochs[2] for item in batch.payload
    )


def test_e0_train_size_override_produces_exactly_one_admission_step(tmp_path: Path) -> None:
    exposure_path, washout_path = _write_schedule_data(tmp_path)
    adapter = CanaryAdapter(
        split_dir=str(tmp_path),
        exposure_path=str(exposure_path),
        washout_path=str(washout_path),
        washout_start_epoch=3,
        train_size_override=4,
        seed=7,
        split_seed=7,
    )
    adapter.setup({"out_root": str(tmp_path / "out"), "env": "canary"})
    loader = adapter.get_dataloader()

    steps_per_epoch = math.ceil(loader.get_train_size() / 4)
    admission_batches = loader.plan_train_epoch(
        epoch=1,
        steps_per_epoch=steps_per_epoch,
        accumulation=1,
        batch_size=4,
        seed=7,
    )

    assert len(loader.train_items) == 8
    assert loader.get_train_size() == 4
    assert steps_per_epoch == 1
    assert len(admission_batches) == 1
    assert admission_batches[0].metadata["contains_exposure"]
    assert admission_batches[0].metadata["exposure_count"] == 2


def test_bridge_maps_e0_to_one_step_and_e1_to_full_evolution(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "configs" / "pilot_skillopt.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["experiment"]["output_root"] = str(tmp_path / "run")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(config_path)
    base_row = {
        "seed_present": True,
        "lineage_seed": 11,
        "data_seed": 12,
    }

    frozen = _flat_skillopt_config(
        config,
        {**base_row, "evolution_enabled": False},
        tmp_path / "data",
        tmp_path / "frozen",
    )
    evolving = _flat_skillopt_config(
        config,
        {**base_row, "evolution_enabled": True},
        tmp_path / "data",
        tmp_path / "evolving",
    )

    assert frozen["num_epochs"] == 1
    assert frozen["train_size_override"] == (
        raw["evolution"]["batch_size"] * raw["evolution"]["accumulation"]
    )
    assert evolving["num_epochs"] == raw["evolution"]["epochs"]
    assert evolving["train_size_override"] is None
