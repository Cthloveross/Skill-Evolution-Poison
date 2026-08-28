from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import experiment
import summarize


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in rows
    )


def _artifact(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "sha256": experiment.sha256_bytes(payload),
        "bytes": len(payload),
    }


def test_structured_timeout_count_ignores_timeout_text() -> None:
    payload = _jsonl(
        [
            {
                "id": "normal",
                "response": "client.get(url, timeout=5)",
                "fail_reason": "",
                "agent_ok": True,
            },
            {
                "id": "timed-out",
                "response": "",
                "fail_reason": "task-timeout-960s",
                "agent_ok": False,
                "phase": "timeout",
            },
            {
                "id": "error",
                "response": "",
                "fail_reason": "unexpected: RuntimeError: timeout in message",
                "agent_ok": False,
                "phase": "error",
            },
            {
                "id": "legacy-timeout",
                "response": "",
                "fail_reason": "task-timeout-960s",
                "agent_ok": False,
            },
        ]
    )

    assert summarize._structured_timeout_count(
        payload, "fixture", expected_rows=4
    ) == 2


def test_completion_exposes_timeout_counts_and_reused_rfinal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    run = experiment.load_run_index()["runs"][0]
    root = tmp_path / "run"
    root.mkdir()
    (root / "completion-receipt.json").write_text("{}\n", encoding="utf-8")

    baseline = root / "baseline.jsonl"
    best = root / "best.jsonl"
    baseline.write_bytes(
        _jsonl(
            [
                {"id": "a", "agent_ok": True, "response": "timeout=5"},
                {
                    "id": "b",
                    "agent_ok": False,
                    "phase": "timeout",
                    "fail_reason": "task-timeout-960s",
                },
            ]
        )
    )
    best.write_bytes(
        _jsonl(
            [
                {"id": "a", "agent_ok": True},
                {"id": "b", "agent_ok": True},
            ]
        )
    )
    verified = {
        "utility": {
            "r0_hard": 0.5,
            "rbest_hard": 0.75,
            "rfinal_hard": 0.75,
            "rbest_delta_hard": 0.25,
            "rfinal_delta_hard": 0.25,
        },
        "checkpoints": {
            "raw_changes": {"r0_to_rbest": True, "r0_to_rfinal": True},
            "substantive_changes": {
                "r0_to_rbest": True,
                "r0_to_rfinal": True,
            },
        },
        "artifacts": {
            "baseline_results": _artifact(baseline),
            "best_results": _artifact(best),
            "final_results": _artifact(best),
        },
        "test_set": {
            "r0": {"row_count": 2},
            "rbest": {"row_count": 2},
            "rfinal": {"row_count": 2},
            "rfinal_results_source": "reused_rbest_identical_checkpoint",
        },
    }

    monkeypatch.setattr(experiment, "output_root", lambda _: root)
    monkeypatch.setattr(summarize, "validate_completion_receipt", lambda *a, **k: verified)
    monkeypatch.setattr(
        summarize,
        "optimizer_diagnostics",
        lambda *a, **k: summarize._unknown_optimizer("missing", "fixture"),
    )

    result = summarize.inspect_completion(run)

    assert result["trajectory"]["status"] == "valid"
    assert result["accuracy"]["r0"]["timeout_count"] == 1
    assert result["accuracy"]["rbest"]["timeout_count"] == 0
    assert result["accuracy"]["rfinal"]["timeout_count"] == 0
    assert result["accuracy"]["rfinal"]["results_source"] == (
        "reused_rbest_identical_checkpoint"
    )
    assert result["accuracy"]["rfinal"]["results_reused"] is True


def _completion_fixture(run: dict[str, Any]) -> dict[str, Any]:
    reused = run.get("attack") == "file_delete"
    accuracy = {
        checkpoint: summarize.known_metric(
            value,
            test_denominator=700,
            timeout_count=0,
            results_source=(
                "reused_rbest_identical_checkpoint"
                if checkpoint == "rfinal" and reused
                else "independent"
            ),
            results_reused=checkpoint == "rfinal" and reused,
        )
        for checkpoint, value in (("r0", 0.5), ("rbest", 0.6), ("rfinal", 0.7))
    }
    return {
        "trajectory": {"status": "valid", "reason": "fixture"},
        "accuracy": accuracy,
        "accuracy_deltas": {
            "rbest_minus_r0": summarize.known_metric(0.1),
            "rfinal_minus_r0": summarize.known_metric(0.2),
        },
        "evolution": {
            checkpoint: {
                "raw_changed": summarize.known_metric(True),
                "substantive_changed": summarize.known_metric(True),
            }
            for checkpoint in ("rbest", "rfinal")
        },
        "optimizer": summarize._unknown_optimizer("missing", "fixture"),
    }


def _asr_fixture(run: dict[str, Any], checkpoint: str) -> dict[str, Any]:
    if checkpoint != "rfinal" or run["attack"] == "db_delete":
        return summarize.known_metric(True, evidence_status="valid")
    if run["attack"] == "file_delete":
        return summarize.known_metric(False, evidence_status="valid")
    return summarize.unknown_metric("fixture", evidence_status="missing")


def test_markdown_claims_follow_observed_data() -> None:
    summary = summarize.build_summary(
        experiment.load_run_index(),
        completion_inspector=_completion_fixture,
        asr_inspector=_asr_fixture,
        generated_at="2026-08-28T00:00:00+00:00",
    )

    markdown = summarize.render_markdown(summary)

    assert "有效轨迹 `4/4`" in markdown
    assert "提升/不变/下降为 `4/0/0`" in markdown
    assert "Skill 实质变化证据" in markdown
    assert "`4/0/0`；攻击轨迹为 `3/0/0`" in markdown
    assert "小幅、非一致" not in markdown
    assert "并非全部真正进化" not in markdown
    assert "Rfinal 成功/失败/unknown 为 `1/1/1`" in markdown
    assert "| rfinal | 1 | 1 | 1 | 50.00% |" in markdown
    assert "Timeout R0/Rbest/Rfinal" in markdown
    assert "0/0/0 | reused Rbest (identical raw checkpoint)" in markdown
