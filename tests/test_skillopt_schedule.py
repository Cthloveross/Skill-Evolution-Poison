from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("skillopt")

from evoincubation.skillopt_env.dataloader import CanaryDataLoader


def _write_split(root: Path, split: str, count: int) -> None:
    path = root / split
    path.mkdir(parents=True)
    items = [{"id": f"{split}-{index}", "exposure_kind": "clean"} for index in range(count)]
    (path / "items.json").write_text(json.dumps(items), encoding="utf-8")


def test_exposure_occurs_once_then_disappears(tmp_path: Path) -> None:
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
    loader = CanaryDataLoader(
        split_dir=str(tmp_path),
        exposure_path=str(exposure_path),
        washout_path=str(washout_path),
        washout_start_epoch=2,
        seed=7,
        split_seed=7,
    )
    loader.setup({"out_root": str(tmp_path / "out"), "env": "canary"})
    epoch_one = loader.plan_train_epoch(
        epoch=1, steps_per_epoch=2, accumulation=1, batch_size=4, seed=7
    )
    epoch_two = loader.plan_train_epoch(
        epoch=2, steps_per_epoch=2, accumulation=1, batch_size=4, seed=7
    )
    assert len(epoch_one) == len(epoch_two) == 2
    assert (
        sum(item["exposure_kind"] != "clean" for batch in epoch_one for item in batch.payload) == 2
    )
    assert (
        sum(item["exposure_kind"] != "clean" for batch in epoch_two for item in batch.payload) == 8
    )
    assert not any(
        item["exposure_kind"] in {"poison_seed", "matched_benign_seed"}
        for batch in epoch_two
        for item in batch.payload
    )
    assert all(
        str(item["id"]).startswith("washout-") for batch in epoch_two for item in batch.payload
    )
