from __future__ import annotations

from defenses.shared.types import FilterResult

from .canonicalizer import StruQCanonicalizer


class StruQFixedFilter:
    """Fixed structure-first StruQ defense."""

    def __init__(self, method_name: str = "struq_fixed"):
        self.impl = StruQCanonicalizer(method=method_name)

    def apply(
        self,
        text: str,
        *,
        source_path: str = "",
        injection_meta: dict | None = None,
    ) -> FilterResult:
        return self.impl.apply(
            text,
            source_path=source_path,
            injection_meta=injection_meta,
        )
