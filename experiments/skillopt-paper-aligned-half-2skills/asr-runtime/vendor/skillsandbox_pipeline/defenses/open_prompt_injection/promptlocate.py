from __future__ import annotations

from defenses.shared.types import FilterResult
from defenses.struq.canonicalizer import StruQCanonicalizer

from .adapter import BackendUnavailableError, OpenPromptInjectionAdapter
from .datasentinel import _backend_unavailable_report


class OPIPromptLocateFilter:
    """PromptLocate-only recovery helper using a structure-derived target instruction."""

    def __init__(self, method_name: str = "opi_promptlocate", adapter: OpenPromptInjectionAdapter | None = None):
        self.method_name = method_name
        self.adapter = adapter or OpenPromptInjectionAdapter()
        self.skeleton_builder = StruQCanonicalizer(method="struq_skeleton")

    def apply(
        self,
        text: str,
        *,
        source_path: str = "",
        injection_meta: dict | None = None,
    ) -> FilterResult:
        skeleton_report = self.skeleton_builder.apply(
            text,
            source_path=source_path,
            injection_meta=injection_meta,
        )
        target_instruction = skeleton_report["filtered_text"]
        candidate = skeleton_report["untrusted_text"] or text

        try:
            recovered_text, localized_text = self.adapter.locate_and_recover(candidate, target_instruction)
        except BackendUnavailableError as exc:
            return _backend_unavailable_report(
                method_name=self.method_name,
                source_path=source_path,
                text=text,
                component="PromptLocate",
                implementation_kind="upstream_localizer",
                reason=str(exc),
            )

        filtered_text = _merge_skeleton_and_residual(target_instruction, recovered_text)
        notes = [
            "Open-Prompt-Injection PromptLocate recovery helper.",
            "Target instruction is derived from structure-first trusted skeleton.",
        ]
        return {
            "method": self.method_name,
            "defense_family": "open_prompt_injection",
            "implementation_kind": "upstream_localizer",
            "backend": "open_prompt_injection",
            "input_path": source_path,
            "contaminated": bool(localized_text or skeleton_report["untrusted_text"]),
            "blocked": False,
            "allowed_to_proceed": True,
            "rewritten": True,
            "action": "recovered",
            "contamination_score": float(bool(localized_text or skeleton_report["untrusted_text"])),
            "suspicious_spans": skeleton_report.get("suspicious_spans", []),
            "trusted_text": skeleton_report["trusted_text"],
            "untrusted_text": skeleton_report["untrusted_text"],
            "filtered_text": filtered_text,
            "dropped_char_count": max(0, len(text) - len(filtered_text)),
            "notes": notes,
            "target_instruction": target_instruction,
            "localization_candidate": candidate,
            "localized_text": localized_text,
            "recovered_text": recovered_text,
            "trusted_sections": skeleton_report.get("trusted_sections", []),
            "untrusted_sections": skeleton_report.get("untrusted_sections", []),
        }


def _merge_skeleton_and_residual(skeleton: str, residual: str) -> str:
    stripped_residual = residual.strip()
    if not stripped_residual:
        return skeleton
    return skeleton.rstrip() + "\n\n## Recovered Residual\n\n" + stripped_residual + "\n"
