from __future__ import annotations

import json
import re
from pathlib import Path


NAIVE_STRATEGY = "naive"


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    lowered = title.strip().lower()
    lowered = re.sub(r"[^a-z0-9\s\-_/]", " ", lowered)
    return " ".join(lowered.split())


def stable_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled"


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def validate_strategy_or_raise(injection_meta: dict | None) -> None:
    """Accept any injection strategy — defense is black-box to the attack."""
    pass
