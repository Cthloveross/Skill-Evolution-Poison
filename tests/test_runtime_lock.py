from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from evoincubation import io_utils
from evoincubation.io_utils import exclusive_lock


def _owner(*, pid: int, hostname: str, token: str = "existing-owner") -> dict[str, object]:
    return {
        "pid": pid,
        "hostname": hostname,
        "started_utc": "2026-08-16T12:00:00+00:00",
        "owner_token": token,
    }


def _write_lock(path: Path, owner: dict[str, object]) -> None:
    path.mkdir()
    (path / "owner.json").write_text(json.dumps(owner), encoding="utf-8")


def test_lock_records_owner_and_releases_normally(tmp_path: Path) -> None:
    path = tmp_path / ".lock"

    with exclusive_lock(path):
        owner = json.loads((path / "owner.json").read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        assert owner["hostname"] == socket.gethostname()
        assert owner["owner_token"]
        started = dt.datetime.fromisoformat(owner["started_utc"])
        assert started.tzinfo is not None

        with pytest.raises(RuntimeError, match="live process"):
            with exclusive_lock(path):
                pytest.fail("a live lock must not be acquired twice")

    assert not path.exists()


def test_same_host_dead_pid_is_recovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".lock"
    dead_pid = 987_654_321
    _write_lock(path, _owner(pid=dead_pid, hostname=socket.gethostname()))

    real_kill = os.kill

    def fake_kill(pid: int, signal: int) -> None:
        if pid == dead_pid and signal == 0:
            raise ProcessLookupError
        real_kill(pid, signal)

    monkeypatch.setattr(io_utils.os, "kill", fake_kill)

    with exclusive_lock(path):
        replacement = json.loads((path / "owner.json").read_text(encoding="utf-8"))
        assert replacement["owner_token"] != "existing-owner"
        assert replacement["pid"] == os.getpid()

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_lock_left_by_exited_process_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "from pathlib import Path; "
                "from evoincubation.io_utils import exclusive_lock; "
                f"lock = exclusive_lock(Path({str(path)!r})); "
                "lock.__enter__(); "
                "os._exit(0)"
            ),
        ]
    )
    assert child.wait(timeout=10) == 0
    abandoned = json.loads((path / "owner.json").read_text(encoding="utf-8"))
    assert abandoned["pid"] == child.pid

    with exclusive_lock(path):
        replacement = json.loads((path / "owner.json").read_text(encoding="utf-8"))
        assert replacement["owner_token"] != abandoned["owner_token"]

    assert not path.exists()


def test_same_host_live_pid_is_rejected_without_modifying_lock(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    owner = _owner(pid=os.getpid(), hostname=socket.gethostname())
    _write_lock(path, owner)

    with pytest.raises(RuntimeError, match="live process"):
        with exclusive_lock(path):
            pytest.fail("a live lock must not be acquired")

    assert json.loads((path / "owner.json").read_text(encoding="utf-8")) == owner


def test_foreign_host_lock_is_rejected_without_probing_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".lock"
    owner = _owner(pid=987_654_321, hostname="another-host")
    _write_lock(path, owner)

    def unexpected_kill(pid: int, signal: int) -> None:
        raise AssertionError(f"must not probe foreign pid {pid} with signal {signal}")

    monkeypatch.setattr(io_utils.os, "kill", unexpected_kill)

    with pytest.raises(RuntimeError, match="different host"):
        with exclusive_lock(path):
            pytest.fail("a foreign lock must not be acquired")

    assert json.loads((path / "owner.json").read_text(encoding="utf-8")) == owner


@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        json.dumps({"pid": 123, "hostname": socket.gethostname()}),
        json.dumps(_owner(pid=True, hostname=socket.gethostname(), token="malformed-owner")),
    ],
)
def test_malformed_lock_metadata_is_rejected(tmp_path: Path, contents: str) -> None:
    path = tmp_path / ".lock"
    path.mkdir()
    (path / "owner.json").write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="metadata"):
        with exclusive_lock(path):
            pytest.fail("a lock with untrusted metadata must not be acquired")

    assert (path / "owner.json").read_text(encoding="utf-8") == contents


def test_unprobeable_same_host_pid_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".lock"
    owner = _owner(pid=123_456, hostname=socket.gethostname())
    _write_lock(path, owner)

    def deny_probe(pid: int, signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(io_utils.os, "kill", deny_probe)

    with pytest.raises(RuntimeError, match="cannot prove"):
        with exclusive_lock(path):
            pytest.fail("an unprobeable lock must not be acquired")

    assert json.loads((path / "owner.json").read_text(encoding="utf-8")) == owner


def test_release_does_not_remove_another_owners_replacement(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    replacement = _owner(pid=os.getpid(), hostname=socket.gethostname(), token="replacement")

    with exclusive_lock(path):
        shutil.rmtree(path)
        _write_lock(path, replacement)

    assert path.is_dir()
    assert json.loads((path / "owner.json").read_text(encoding="utf-8")) == replacement
