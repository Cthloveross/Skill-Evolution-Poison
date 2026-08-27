from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, TypedDict


@dataclass
class MarkdownBlock:
    index: int
    kind: str
    text: str
    start: int
    end: int
    heading: str | None = None
    level: int | None = None
    body_start: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SectionBodyBlock:
    kind: str
    text: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FilterResult(TypedDict, total=False):
    method: str
    defense_family: str
    implementation_kind: str
    backend: str
    input_path: str
    contaminated: bool | None
    blocked: bool
    allowed_to_proceed: bool
    rewritten: bool
    action: str
    contamination_score: float | None
    suspicious: bool
    suspicious_spans: list[dict[str, Any]]
    trusted_text: str
    untrusted_text: str
    filtered_text: str
    dropped_char_count: int
    notes: list[str]
    blocks: list[dict[str, Any]]
    trusted_sections: list[dict[str, Any]]
    untrusted_sections: list[dict[str, Any]]
    target_instruction: str
    localization_candidate: str
    localized_text: str
    recovered_text: str
    backend_error: str
    attack_type_guessed: str
    confidence: int | float | None
    reasoning: str
    raw_analysis: dict[str, Any]
    untrusted_char_ratio: float
    residual_severity: str
    contamination_reasons: list[str]
