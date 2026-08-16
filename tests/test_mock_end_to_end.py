from __future__ import annotations

from pathlib import Path

import yaml

from evoincubation.config import load_config
from evoincubation.metrics import aggregate_experiment
from evoincubation.runner import (
    load_design_rows,
    prepare_experiment,
    run_lineage,
    validate_prepared_experiment,
)


def test_mock_pipeline_exercises_full_factorial_and_aggregation(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "configs" / "smoke_mock.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["experiment"]["output_root"] = str(tmp_path / "run")
    raw["experiment"]["replicates"] = 2
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(path)

    prepare_experiment(config)
    validation = validate_prepared_experiment(config)
    assert validation["ok"]
    for row in load_design_rows(config):
        run_lineage(config, row)

    result = aggregate_experiment(config)
    assert result["n_randomized"] == 8
    assert result["n_included"] == 8
    assert result["n_complete_blocks"] == 2
    assert result["incubation_interaction"]["estimate"] > 0.4
    assert result["arm_summary"]["S1E0"]["mean_final_locked_asr"] < 0.1
    assert result["arm_summary"]["S1E1"]["mean_final_locked_asr"] > 0.6
    assert result["mock_warning"]
