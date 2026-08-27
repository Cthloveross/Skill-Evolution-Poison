"""Shared defense helpers and types."""

from .common import (
    NAIVE_STRATEGY,
    normalize_title,
    read_json,
    stable_slug,
    validate_strategy_or_raise,
    write_json,
)
from .markdown_parser import parse_frontmatter, parse_markdown_blocks, parse_section_body_blocks
from .schema import BucketPolicy
from .types import FilterResult, MarkdownBlock, SectionBodyBlock

__all__ = [
    "BucketPolicy",
    "FilterResult",
    "MarkdownBlock",
    "NAIVE_STRATEGY",
    "SectionBodyBlock",
    "normalize_title",
    "parse_frontmatter",
    "parse_markdown_blocks",
    "parse_section_body_blocks",
    "read_json",
    "stable_slug",
    "validate_strategy_or_raise",
    "write_json",
]
