from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from skillopt.datasets.base import BatchSpec, SplitDataLoader


class CanaryDataLoader(SplitDataLoader):
    """Expose a matched seed batch once, then only clean training evidence."""

    def __init__(
        self,
        *,
        split_dir: str,
        exposure_path: str,
        washout_path: str = "",
        washout_start_epoch: int = 10**9,
        split_seed: int = 42,
        seed: int = 42,
        limit: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            split_dir=split_dir,
            split_mode="split_dir",
            split_seed=split_seed,
            seed=seed,
            limit=limit,
        )
        self.exposure_path = str(exposure_path)
        self.exposure_items: list[dict[str, Any]] = []
        self.washout_path = str(washout_path)
        self.washout_start_epoch = int(washout_start_epoch)
        self.washout_items: list[dict[str, Any]] = []

    def setup(self, cfg: dict) -> None:
        super().setup(cfg)
        path = Path(self.exposure_path)
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"Exposure file must contain a non-empty JSON list: {path}")
        self.exposure_items = payload
        if self.washout_path:
            with Path(self.washout_path).open(encoding="utf-8") as handle:
                washout = json.load(handle)
            if not isinstance(washout, list) or not washout:
                raise ValueError(
                    f"Washout file must contain a non-empty JSON list: {self.washout_path}"
                )
            self.washout_items = washout

    def plan_train_epoch(
        self,
        *,
        epoch: int,
        steps_per_epoch: int,
        accumulation: int,
        batch_size: int,
        seed: int,
        **kwargs: Any,
    ) -> list[BatchSpec]:
        del kwargs
        total_batches = steps_per_epoch * accumulation
        if total_batches <= 0:
            return []
        clean = list(
            self.washout_items
            if epoch >= self.washout_start_epoch and self.washout_items
            else self.train_items
        )
        rng = random.Random(seed + epoch * 1000)
        rng.shuffle(clean)
        if not clean:
            raise ValueError("Canary training split is empty")

        batches: list[BatchSpec] = []
        cursor = 0
        for batch_index in range(total_batches):
            items: list[dict[str, Any]] = []
            if epoch == 1 and batch_index == 0:
                items.extend(dict(item) for item in self.exposure_items[:batch_size])
            while len(items) < batch_size:
                if cursor >= len(clean):
                    refill = list(
                        self.washout_items
                        if epoch >= self.washout_start_epoch and self.washout_items
                        else self.train_items
                    )
                    random.Random(seed + epoch * 1000 + batch_index + cursor).shuffle(refill)
                    clean.extend(refill)
                items.append(dict(clean[cursor]))
                cursor += 1
            batches.append(
                BatchSpec(
                    phase="train",
                    split="train",
                    seed=seed + epoch * 1000 + batch_index + 1,
                    batch_size=len(items),
                    payload=items,
                    metadata={
                        "contains_exposure": epoch == 1 and batch_index == 0,
                        "exposure_count": sum(
                            item.get("exposure_kind") in {"poison_seed", "matched_benign_seed"}
                            for item in items
                        ),
                        "washout": epoch >= self.washout_start_epoch,
                    },
                )
            )
        return batches
