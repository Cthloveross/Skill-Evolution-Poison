from __future__ import annotations

import json
from pathlib import Path

from evoincubation.checkpoints import SkillCheckpoint, select_monitor_checkpoints
from evoincubation.skillopt_bridge import _audit_training_exposure, _evaluate_checkpoint


def _checkpoint(tmp_path: Path, version: int, digest: str, changed: bool) -> SkillCheckpoint:
    path = tmp_path / f"skill_v{version:04d}.md"
    path.write_text(f"skill {digest}", encoding="utf-8")
    return SkillCheckpoint(
        version=version,
        path=path,
        sha256=digest,
        changed_from_previous=changed,
    )


def test_monitor_timeline_always_keeps_baseline_admission_changes_and_final(
    tmp_path: Path,
) -> None:
    checkpoints = [
        _checkpoint(tmp_path, 0, "a", False),
        _checkpoint(tmp_path, 1, "a", False),
        _checkpoint(tmp_path, 2, "b", True),
        _checkpoint(tmp_path, 3, "b", False),
    ]

    selected = select_monitor_checkpoints(checkpoints)

    assert [checkpoint.version for checkpoint in selected] == [0, 1, 2, 3]


def test_checkpoint_evaluation_reuses_identical_artifact_rollout(tmp_path: Path) -> None:
    calls: list[str] = []

    class _Adapter:
        def rollout(self, items, skill, out_dir):
            calls.append(out_dir)
            return [{"hard": 1, "soft": 0.75, "attack_success": 0} for _item in items]

    baseline = _checkpoint(tmp_path, 0, "same", False)
    admission = _checkpoint(tmp_path, 1, "same", False)
    cache: dict[tuple[str, str], dict] = {}
    first = _evaluate_checkpoint(
        checkpoint=baseline,
        split="clean_test",
        items=[{"id": "a"}],
        eval_root=tmp_path / "eval",
        adapter=_Adapter(),
        lineage_id="lineage",
        final_locked=False,
        evaluation_cache=cache,
    )
    second = _evaluate_checkpoint(
        checkpoint=admission,
        split="clean_test",
        items=[{"id": "a"}],
        eval_root=tmp_path / "eval",
        adapter=_Adapter(),
        lineage_id="lineage",
        final_locked=False,
        evaluation_cache=cache,
    )

    assert len(calls) == 1
    assert first["version"] == 0
    assert second["version"] == 1
    assert second["reused_from_version"] == 0
    assert second["attack_success_rate"] == first["attack_success_rate"]


def test_training_exposure_audit_uses_actual_step_one_rollouts(tmp_path: Path) -> None:
    step_one = tmp_path / "steps" / "step_0001" / "rollout"
    step_two = tmp_path / "steps" / "step_0002" / "rollout"
    step_one.mkdir(parents=True)
    step_two.mkdir(parents=True)
    (step_one / "rollouts.json").write_text(
        json.dumps(
            [
                {"id": "exposure-0000", "hard": 1, "soft": 0.75},
                {"id": "exposure-0001", "hard": 0, "soft": 0.0},
                {"id": "train-0000", "hard": 1, "soft": 0.5},
            ]
        ),
        encoding="utf-8",
    )
    (step_two / "rollouts.json").write_text(
        json.dumps([{"id": "washout-0000", "hard": 1, "soft": 0.5}]),
        encoding="utf-8",
    )

    audit = _audit_training_exposure(
        tmp_path,
        expected_ids={"exposure-0000", "exposure-0001"},
    )

    assert audit["actual_training_exposure_count"] == 2
    assert audit["exactly_once"]
    assert audit["only_step_0001"]
    assert audit["hard_accuracy"] == 0.5
    assert audit["soft_utility"] == 0.375


def test_training_exposure_audit_detects_repeated_seed(tmp_path: Path) -> None:
    for step in (1, 2):
        rollout = tmp_path / "steps" / f"step_{step:04d}" / "rollout"
        rollout.mkdir(parents=True)
        (rollout / "rollouts.json").write_text(
            json.dumps([{"id": "exposure-0000", "hard": 1, "soft": 0.75}]),
            encoding="utf-8",
        )

    audit = _audit_training_exposure(tmp_path, expected_ids={"exposure-0000"})

    assert not audit["exactly_once"]
    assert not audit["only_step_0001"]


def test_training_exposure_audit_reads_accumulation_batch_rollouts(tmp_path: Path) -> None:
    rollout = tmp_path / "steps" / "step_0001" / "batch_0001" / "rollout"
    rollout.mkdir(parents=True)
    (rollout / "rollouts.json").write_text(
        json.dumps([{"id": "exposure-0000", "hard": 1, "soft": 0.75}]),
        encoding="utf-8",
    )

    audit = _audit_training_exposure(tmp_path, expected_ids={"exposure-0000"})

    assert audit["actual_training_exposure_count"] == 1
    assert audit["exactly_once"]
    assert audit["only_step_0001"]
