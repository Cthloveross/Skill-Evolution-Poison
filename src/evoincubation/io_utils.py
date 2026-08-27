from __future__ import annotations

import csv
import datetime as dt
import fcntl
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_LOCK_OWNER_FILE = "owner.json"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any, length: int = 16) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(path, text)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0]) if rows else []
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _atomic_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _lock_identity(stat_result: os.stat_result) -> tuple[int, int]:
    return stat_result.st_dev, stat_result.st_ino


def _validate_lock_owner(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Lineage lock metadata is invalid; cannot prove stale: {path}")

    pid = value.get("pid")
    hostname = value.get("hostname")
    started_utc = value.get("started_utc")
    owner_token = value.get("owner_token")
    valid = (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and isinstance(hostname, str)
        and bool(hostname)
        and isinstance(started_utc, str)
        and bool(started_utc)
        and isinstance(owner_token, str)
        and bool(owner_token)
    )
    if not valid:
        raise RuntimeError(f"Lineage lock metadata is invalid; cannot prove stale: {path}")

    try:
        started = dt.datetime.fromisoformat(started_utc)
    except ValueError as exc:
        raise RuntimeError(f"Lineage lock metadata is invalid; cannot prove stale: {path}") from exc
    if started.tzinfo is None:
        raise RuntimeError(f"Lineage lock metadata is invalid; cannot prove stale: {path}")
    return value


def _read_lock_owner_fd(owner_fd: int, path: Path) -> dict[str, Any]:
    try:
        os.lseek(owner_fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(owner_fd), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Lineage lock metadata is missing or invalid; cannot prove stale: {path}"
        ) from exc
    return _validate_lock_owner(value, path)


def _open_lock(path: Path) -> tuple[int, int]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    owner_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(path, directory_flags)
    except OSError as exc:
        raise RuntimeError(
            f"Lineage lock metadata is missing or invalid; cannot prove stale: {path}"
        ) from exc
    try:
        owner_fd = os.open(_LOCK_OWNER_FILE, owner_flags, dir_fd=directory_fd)
    except OSError as exc:
        os.close(directory_fd)
        raise RuntimeError(
            f"Lineage lock metadata is missing or invalid; cannot prove stale: {path}"
        ) from exc
    return directory_fd, owner_fd


def _new_lock_owner() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "owner_token": uuid.uuid4().hex,
    }


def _create_lock(path: Path, owner: dict[str, Any]) -> tuple[int, int, tuple[int, int]]:
    path.mkdir(parents=False)
    directory_fd: int | None = None
    owner_fd: int | None = None
    try:
        write_json(path / _LOCK_OWNER_FILE, owner)
        directory_fd, owner_fd = _open_lock(path)
        fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = _lock_identity(os.fstat(directory_fd))
        return directory_fd, owner_fd, identity
    except BaseException:
        if owner_fd is not None:
            os.close(owner_fd)
        if directory_fd is not None:
            os.close(directory_fd)
        try:
            (path / _LOCK_OWNER_FILE).unlink()
            path.rmdir()
        except OSError:
            pass
        raise


def _retire_stale_lock(path: Path) -> Path:
    directory_fd, owner_fd = _open_lock(path)
    try:
        owner = _read_lock_owner_fd(owner_fd, path)
        hostname = owner["hostname"]
        if hostname != socket.gethostname():
            raise RuntimeError(
                f"Lineage lock belongs to a different host; cannot prove stale: {path}"
            )

        pid = owner["pid"]
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError) as exc:
            raise RuntimeError(
                f"Lineage lock owner cannot be probed; cannot prove it is stale: {path}"
            ) from exc
        else:
            raise RuntimeError(f"Lineage lock belongs to a live process (pid={pid}): {path}")

        try:
            fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Lineage lock is being recovered; cannot prove it is stale: {path}"
            ) from exc

        confirmed_owner = _read_lock_owner_fd(owner_fd, path)
        if confirmed_owner != owner:
            raise RuntimeError(f"Lineage lock metadata changed during recovery: {path}")

        expected_identity = _lock_identity(os.fstat(directory_fd))
        try:
            current_identity = _lock_identity(path.stat(follow_symlinks=False))
        except OSError as exc:
            raise RuntimeError(f"Lineage lock changed during recovery: {path}") from exc
        if current_identity != expected_identity:
            raise RuntimeError(f"Lineage lock changed during recovery: {path}")

        retired_path = path.with_name(f"{path.name}.stale-{uuid.uuid4().hex}")
        try:
            path.rename(retired_path)
        except OSError as exc:
            raise RuntimeError(f"Unable to atomically recover stale lineage lock: {path}") from exc
        if _lock_identity(retired_path.stat(follow_symlinks=False)) != expected_identity:
            raise RuntimeError(f"Lineage lock changed during recovery: {path}")
        return retired_path
    finally:
        os.close(owner_fd)
        os.close(directory_fd)


def _acquire_lock(path: Path, owner: dict[str, Any]) -> tuple[int, int, tuple[int, int]]:
    try:
        return _create_lock(path, owner)
    except FileExistsError:
        retired_path = _retire_stale_lock(path)

    try:
        return _create_lock(path, owner)
    except FileExistsError as exc:
        raise RuntimeError(f"Another process acquired the recovered lineage lock: {path}") from exc
    finally:
        shutil.rmtree(retired_path, ignore_errors=True)


def _release_lock(
    path: Path, owner_token: str, directory_fd: int, expected_identity: tuple[int, int]
) -> None:
    try:
        if _lock_identity(path.stat(follow_symlinks=False)) != expected_identity:
            return
        if os.listdir(directory_fd) != [_LOCK_OWNER_FILE]:
            return

        owner_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        current_owner_fd = os.open(_LOCK_OWNER_FILE, owner_flags, dir_fd=directory_fd)
        try:
            current_owner = _read_lock_owner_fd(current_owner_fd, path)
        finally:
            os.close(current_owner_fd)
        if current_owner["owner_token"] != owner_token:
            return

        if _lock_identity(path.stat(follow_symlinks=False)) != expected_identity:
            return
        os.unlink(_LOCK_OWNER_FILE, dir_fd=directory_fd)
        if _lock_identity(path.stat(follow_symlinks=False)) != expected_identity:
            return
        path.rmdir()
    except (OSError, RuntimeError):
        return


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Acquire a recoverable, host-local directory lock for one lineage."""
    owner = _new_lock_owner()
    directory_fd, owner_fd, identity = _acquire_lock(path, owner)
    try:
        yield
    finally:
        try:
            _release_lock(path, owner["owner_token"], directory_fd, identity)
        finally:
            os.close(owner_fd)
            os.close(directory_fd)


def git_revision(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _git_capture(path: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def _harness_provenance() -> dict[str, Any]:
    source_path = Path(__file__).resolve()
    root_bytes = _git_capture(source_path.parent, "rev-parse", "--show-toplevel")
    if root_bytes is None:
        return {"repository_root": None, "git_revision": None, "git_dirty": None}
    repository_root = Path(root_bytes.decode("utf-8").strip()).resolve()
    status = _git_capture(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    diff = _git_capture(repository_root, "diff", "--binary", "HEAD", "--")
    return {
        "repository_root": str(repository_root),
        "git_revision": git_revision(repository_root),
        "git_dirty": bool(status) if status is not None else None,
        "git_status": status.decode("utf-8", errors="replace").splitlines()
        if status is not None
        else None,
        "git_diff_sha256": hashlib.sha256(diff).hexdigest() if diff is not None else None,
    }


def runtime_provenance() -> dict[str, Any]:
    packages = sorted(
        f"{distribution.metadata.get('Name') or distribution.name}=={distribution.version}"
        for distribution in importlib.metadata.distributions()
    )
    environment_keys = (
        "CUDA_VISIBLE_DEVICES",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "OPTIMIZER_OPENAI_COMPATIBLE_BASE_URL",
        "OPTIMIZER_OPENAI_COMPATIBLE_MODEL",
        "OPTIMIZER_OPENAI_COMPATIBLE_TEMPERATURE",
        "TARGET_OPENAI_COMPATIBLE_BASE_URL",
        "TARGET_OPENAI_COMPATIBLE_MODEL",
        "TARGET_OPENAI_COMPATIBLE_TEMPERATURE",
    )
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "executable": sys.executable,
        "hostname": socket.gethostname(),
        "harness": _harness_provenance(),
        "packages": packages,
        "packages_sha256": hashlib.sha256("\n".join(packages).encode("utf-8")).hexdigest(),
        "environment": {key: os.environ.get(key) for key in environment_keys},
    }
