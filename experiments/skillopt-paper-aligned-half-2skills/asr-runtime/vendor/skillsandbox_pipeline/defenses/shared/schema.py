from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BucketPolicy:
    name: str
    title_keywords: tuple[str, ...]
    allowed_block_kinds: tuple[str, ...]
    allow_inherited_subheadings: bool = True
    paragraph_mode: str = "any"
