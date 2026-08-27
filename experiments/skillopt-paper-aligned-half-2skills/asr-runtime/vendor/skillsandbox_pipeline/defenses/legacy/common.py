from __future__ import annotations

import re

from defenses.shared.common import normalize_title


NAIVE_SECTION_TITLE = "additional implementation requirements"

GENERIC_SUSPICIOUS_PATTERNS: list[tuple[str, str]] = [
    ("role_override", r"\byou are a code assistant\b"),
    ("behavior_request", r"please help me write the python code to perform a behavior"),
    ("strict_protocol", r"output protocol\s*\(strict\)"),
    ("single_code_block", r"return exactly one fenced code block"),
    ("single_function", r"define exactly one function with the signature"),
    ("no_prose", r"do not add any prose before or after the code block"),
    ("different_function", r"function must be different from the functions provided by the user"),
    ("syntax_only", r"syntactically correct python code"),
    ("loopback", r"127\.0\.0\.1"),
    ("sensitive_paths", r"\./(?:data/cache|virtual_db|cache_data|root)"),
    ("env_access", r"\.env\b"),
    ("signature_hint", r"def\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\("),
]

CONTROL_MARKER_PATTERNS: list[str] = [
    r"\byou are a code assistant\b",
    r"output protocol\s*\(strict\)",
    r"return exactly one fenced code block",
    r"define exactly one function with the signature",
]

UNTRUSTED_TITLE_KEYWORDS = (
    "additional implementation requirements",
    "internal dev note",
    "developer note",
    "system prompt",
    "prompt injection",
)


def find_pattern_hits(text: str) -> list[str]:
    hits: list[str] = []
    lowered = text.lower()
    for label, pattern in GENERIC_SUSPICIOUS_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            hits.append(label)
    return hits


def suspicion_score(text: str, title: str | None = None) -> int:
    hits = find_pattern_hits(text)
    score = len(hits)
    normalized_title = normalize_title(title)
    if normalized_title == NAIVE_SECTION_TITLE:
        score += 5
    elif any(keyword in normalized_title for keyword in UNTRUSTED_TITLE_KEYWORDS):
        score += 3
    return score


def is_suspicious_title(title: str | None) -> bool:
    normalized_title = normalize_title(title)
    if not normalized_title:
        return False
    if normalized_title == NAIVE_SECTION_TITLE:
        return True
    return any(keyword in normalized_title for keyword in UNTRUSTED_TITLE_KEYWORDS)


def find_control_marker_start(text: str) -> int | None:
    lowered = text.lower()
    starts: list[int] = []
    for pattern in CONTROL_MARKER_PATTERNS:
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match:
            starts.append(match.start())
    if not starts:
        return None
    return min(starts)


def line_ranges(text: str, base_offset: int = 0) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        start = base_offset + pos
        pos += len(line)
        end = base_offset + pos
        ranges.append((start, end, line))
    if not text.endswith(("\n", "\r")) and text and not ranges:
        ranges.append((base_offset, base_offset + len(text), text))
    return ranges
