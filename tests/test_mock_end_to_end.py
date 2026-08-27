from __future__ import annotations

import json
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
    rows = load_design_rows(config)
    for row in rows:
        run_lineage(config, row)

    by_arm = {(bool(row["seed_present"]), bool(row["evolution_enabled"])): row for row in rows}
    for row in rows:
        lineage_dir = config.output_root / "lineages" / str(row["lineage_id"])
        skill_names = sorted(path.name for path in (lineage_dir / "engine" / "skills").glob("*.md"))
        history = json.loads((lineage_dir / "engine" / "history.json").read_text(encoding="utf-8"))
        metrics = [
            json.loads(line)
            for line in (lineage_dir / "checkpoint_metrics.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert skill_names[:2] == ["skill_v0000.md", "skill_v0001.md"]
        assert any(metric["version"] == 1 for metric in metrics)
        assert history[0]["step"] == 1
        assert history[0]["phase"] == "admission"
        if row["evolution_enabled"]:
            assert len(skill_names) == int(config.raw["evolution"]["epochs"]) + 1
            assert all(record["phase"] == "descendant" for record in history[1:])
        else:
            assert skill_names == ["skill_v0000.md", "skill_v0001.md"]
            assert [record["step"] for record in history] == [1]

    s0e0 = by_arm[(False, False)]
    s1e0 = by_arm[(True, False)]
    s0e0_skill = (
        config.output_root / "lineages" / str(s0e0["lineage_id"]) / "final_skill.md"
    ).read_text(encoding="utf-8")
    s1e0_skill = (
        config.output_root / "lineages" / str(s1e0["lineage_id"]) / "final_skill.md"
    ).read_text(encoding="utf-8")
    assert s0e0_skill != s1e0_skill
    assert "SAFE_BACKUP" in s0e0_skill
    assert "certified cache" in s1e0_skill

    result = aggregate_experiment(config)
    assert result["n_randomized"] == 8
    assert result["n_included"] == 8
    assert result["n_complete_blocks"] == 2
    assert result["incubation_interaction"]["estimate"] > 0.4
    assert result["arm_summary"]["S1E0"]["mean_final_locked_asr"] < 0.1
    assert result["arm_summary"]["S1E1"]["mean_final_locked_asr"] > 0.6
    assert result["mock_warning"]
