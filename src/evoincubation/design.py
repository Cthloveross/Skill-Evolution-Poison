from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evoincubation.config import ExperimentConfig
from evoincubation.io_utils import stable_hash, write_csv, write_json

ARM_LEVELS = (
    (False, False),
    (True, False),
    (False, True),
    (True, True),
)


@dataclass(frozen=True)
class DesignRow:
    lineage_id: str
    block_id: str
    replicate: int
    seed_id: str
    seed_present: bool
    evolution_enabled: bool
    run_order: int
    lineage_seed: int
    data_seed: int
    engine: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "lineage_id": self.lineage_id,
            "block_id": self.block_id,
            "replicate": self.replicate,
            "seed_id": self.seed_id,
            "seed_present": int(self.seed_present),
            "evolution_enabled": int(self.evolution_enabled),
            "run_order": self.run_order,
            "lineage_seed": self.lineage_seed,
            "data_seed": self.data_seed,
            "engine": self.engine,
        }


def build_design(config: ExperimentConfig) -> list[DesignRow]:
    rows: list[DesignRow] = []
    master = random.Random(config.master_seed)
    for candidate_index, candidate in enumerate(config.seed_candidates):
        seed_id = str(candidate["id"])
        for replicate in range(config.replicates):
            block_id = f"{seed_id}-r{replicate:03d}"
            data_seed = master.randrange(1, 2**31 - 1)
            block_arms = list(ARM_LEVELS)
            block_rng = random.Random(
                config.master_seed + 1009 * replicate + 9176 * candidate_index
            )
            block_rng.shuffle(block_arms)
            for run_order, (seed_present, evolution_enabled) in enumerate(block_arms, start=1):
                lineage_seed = master.randrange(1, 2**31 - 1)
                arm_tag = f"s{int(seed_present)}e{int(evolution_enabled)}"
                lineage_id = f"{block_id}-{arm_tag}-{stable_hash([lineage_seed, arm_tag], 8)}"
                rows.append(
                    DesignRow(
                        lineage_id=lineage_id,
                        block_id=block_id,
                        replicate=replicate,
                        seed_id=seed_id,
                        seed_present=seed_present,
                        evolution_enabled=evolution_enabled,
                        run_order=run_order,
                        lineage_seed=lineage_seed,
                        data_seed=data_seed,
                        engine=config.engine,
                    )
                )
    return rows


def save_design(config: ExperimentConfig, rows: list[DesignRow]) -> Path:
    root = config.output_root
    root.mkdir(parents=True, exist_ok=True)
    design_path = root / "design.csv"
    dictionaries = [row.to_dict() for row in rows]
    write_csv(design_path, dictionaries, list(dictionaries[0]))
    write_json(
        root / "design_meta.json",
        {
            "experiment_name": config.name,
            "master_seed": config.master_seed,
            "n_lineages": len(rows),
            "independent_unit": "lineage",
            "block": "seed_candidate x replicate",
            "factors": ["seed_present", "evolution_enabled"],
            "primary_contrast": "seed x evolution interaction on final trigger ASR",
        },
    )
    return design_path


def parse_bool_cell(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}
