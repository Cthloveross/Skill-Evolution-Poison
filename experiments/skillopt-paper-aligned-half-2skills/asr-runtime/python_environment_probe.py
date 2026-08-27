#!/usr/bin/env python3
"""Emit the auditable subset of the ASR Python and host runtime identity."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any


CRITICAL_MODULES = {
    "litellm": "litellm",
    "openai": "openai",
    "openhands-sdk": "openhands.sdk",
    "openhands-tools": "openhands.tools",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def distribution_inventory() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not isinstance(name, str) or not name or not isinstance(version, str):
            raise RuntimeError("installed distribution lacks a name or version")
        records.append(
            {
                "name": name,
                "normalized_name": name.lower().replace("_", "-"),
                "version": version,
                "root": str(Path(distribution.locate_file("")).resolve()),
            }
        )
    records.sort(
        key=lambda item: (
            item["normalized_name"],
            item["name"],
            item["version"],
            item["root"],
        )
    )
    return records


def critical_package_inventory() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for distribution_name, module_name in sorted(CRITICAL_MODULES.items()):
        distribution = importlib.metadata.distribution(distribution_name)
        spec = importlib.util.find_spec(module_name)
        if spec is None or not isinstance(spec.origin, str):
            raise RuntimeError(f"cannot locate critical runtime module: {module_name}")
        source = Path(spec.origin).resolve(strict=True)
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"critical runtime module is not a regular file: {source}")
        records.append(
            {
                "distribution": distribution_name,
                "module": module_name,
                "source_path": str(source),
                "source_sha256": sha256_file(source),
                "version": distribution.version,
            }
        )
    return records


def observation() -> dict[str, Any]:
    executable_path = Path(sys.executable).absolute()
    executable = executable_path.resolve(strict=True)
    os_release_path = Path("/etc/os-release")
    os_release = os_release_path.resolve(strict=True)
    if not executable.is_file():
        raise RuntimeError("Python executable is not a regular file")
    if not os_release.is_file():
        raise RuntimeError("/etc/os-release is not a regular file")
    uname = os.uname()
    return {
        "schema_version": 1,
        "scope": "auditable_non_hermetic_asr_runtime",
        "python": {
            "base_prefix": str(Path(sys.base_prefix).resolve()),
            "executable": str(executable),
            "executable_sha256": sha256_file(executable),
            "implementation": platform.python_implementation(),
            "launcher_path": str(executable_path),
            "prefix": str(Path(sys.prefix).resolve()),
            "version": platform.python_version(),
        },
        "distributions": distribution_inventory(),
        "critical_packages": critical_package_inventory(),
        "host": {
            "kernel_machine": uname.machine,
            "kernel_release": uname.release,
            "kernel_sysname": uname.sysname,
            "kernel_version": uname.version,
            "os_release_path": str(os_release_path),
            "os_release_resolved_path": str(os_release),
            "os_release_sha256": sha256_file(os_release),
        },
    }


def main() -> int:
    print(
        json.dumps(
            observation(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
