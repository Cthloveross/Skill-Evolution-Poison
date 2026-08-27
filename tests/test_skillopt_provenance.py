from __future__ import annotations

import json

import pytest

import evoincubation.skillopt_bridge as bridge


class _Distribution:
    version = "0.2.0"

    def __init__(self, revision: str | None) -> None:
        self.revision = revision

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        if self.revision is None:
            return None
        return json.dumps(
            {
                "url": "https://github.com/microsoft/SkillOpt.git",
                "vcs_info": {"vcs": "git", "commit_id": self.revision},
            }
        )


def test_direct_url_revision_wins_over_enclosing_repository(monkeypatch) -> None:
    expected = bridge.EXPECTED_SKILLOPT_COMMIT
    monkeypatch.setattr(
        bridge.importlib.metadata, "distribution", lambda _name: _Distribution(expected)
    )
    monkeypatch.setattr(bridge, "_package_git_revision", lambda _root: "harness-revision")

    provenance = bridge._skillopt_provenance(expected_revision=expected)

    assert provenance["direct_url_revision"] == expected
    assert provenance["package_git_revision"] == "harness-revision"
    assert provenance["resolved_revision"] == expected
    assert provenance["verification_method"] == "direct_url"


def test_independent_package_worktree_is_revision_fallback(monkeypatch) -> None:
    expected = bridge.EXPECTED_SKILLOPT_COMMIT
    monkeypatch.setattr(
        bridge.importlib.metadata, "distribution", lambda _name: _Distribution(None)
    )
    monkeypatch.setattr(bridge, "_package_git_revision", lambda _root: expected)

    provenance = bridge._skillopt_provenance(expected_revision=expected)

    assert provenance["resolved_revision"] == expected
    assert provenance["verification_method"] == "package_git"


def test_revision_verification_rejects_mismatch_by_default() -> None:
    with pytest.raises(RuntimeError, match="Expected expected-revision, got 'wrong-revision'"):
        bridge._verify_skillopt_revision(
            {"resolved_revision": "wrong-revision"},
            expected_revision="expected-revision",
            allow_unverified=False,
        )


def test_revision_verification_allows_declared_nonconfirmatory_override() -> None:
    bridge._verify_skillopt_revision(
        {"resolved_revision": None},
        expected_revision="expected-revision",
        allow_unverified=True,
    )


def test_installed_skillopt_resolves_to_pinned_direct_url() -> None:
    expected = bridge.EXPECTED_SKILLOPT_COMMIT

    provenance = bridge._skillopt_provenance(expected_revision=expected)

    assert provenance["resolved_revision"] == expected
    assert provenance["verification_method"] == "direct_url"
