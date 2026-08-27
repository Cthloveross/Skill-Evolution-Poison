from __future__ import annotations

from evoincubation.io_utils import runtime_provenance


def test_runtime_provenance_records_reproducibility_without_secrets(monkeypatch) -> None:
    monkeypatch.setenv("TARGET_OPENAI_COMPATIBLE_API_KEY", "must-not-leak")
    monkeypatch.setenv("TARGET_OPENAI_COMPATIBLE_MODEL", "local-model")

    result = runtime_provenance()

    assert result["packages"]
    assert len(result["packages_sha256"]) == 64
    assert result["harness"]["repository_root"]
    assert result["harness"]["git_revision"]
    assert len(result["harness"]["git_diff_sha256"]) == 64
    assert result["environment"]["TARGET_OPENAI_COMPATIBLE_MODEL"] == "local-model"
    assert "API_KEY" not in result["environment"]
    assert "must-not-leak" not in str(result)
