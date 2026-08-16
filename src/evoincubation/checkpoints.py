from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

VERSION_RE = re.compile(r"skill_v(\d+)\.md$")


@dataclass(frozen=True)
class SkillCheckpoint:
    version: int
    path: Path
    sha256: str
    changed_from_previous: bool


def skill_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def discover_checkpoints(skill_dir: Path) -> list[SkillCheckpoint]:
    raw: list[tuple[int, Path, str]] = []
    for path in skill_dir.glob("skill_v*.md"):
        match = VERSION_RE.search(path.name)
        if match:
            content = path.read_text(encoding="utf-8")
            raw.append((int(match.group(1)), path, skill_sha256(content)))
    raw.sort(key=lambda row: row[0])
    checkpoints: list[SkillCheckpoint] = []
    previous: str | None = None
    for version, path, digest in raw:
        checkpoints.append(
            SkillCheckpoint(
                version=version,
                path=path,
                sha256=digest,
                changed_from_previous=previous is not None and digest != previous,
            )
        )
        previous = digest
    return checkpoints


def select_monitor_checkpoints(checkpoints: list[SkillCheckpoint]) -> list[SkillCheckpoint]:
    """Keep baseline, changed descendants, and final without double counting."""
    if not checkpoints:
        return []
    selected = [checkpoints[0]]
    selected.extend(
        checkpoint for checkpoint in checkpoints[1:] if checkpoint.changed_from_previous
    )
    if selected[-1].version != checkpoints[-1].version:
        selected.append(checkpoints[-1])
    return selected
