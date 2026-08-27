from __future__ import annotations

from defenses.shared.types import FilterResult
from defenses.struq.canonicalizer import StruQCanonicalizer

from .adapter import BackendUnavailableError, OpenPromptInjectionAdapter
from .datasentinel import _backend_unavailable_report
from .promptlocate import _merge_skeleton_and_residual


class OPIPipelineFilter:
    """README-style DataSentinel -> PromptLocate pipeline for long SKILL.md documents."""

    def __init__(self, method_name: str = "opi_pipeline", adapter: OpenPromptInjectionAdapter | None = None):
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
        ds_available, ds_reason = self.adapter.datasentinel_available()
        pl_available, pl_reason = self.adapter.promptlocate_available()
        if not ds_available:
            return _backend_unavailable_report(
                method_name=self.method_name,
                source_path=source_path,
                text=text,
                component="DataSentinel",
                implementation_kind="upstream_pipeline",
                reason=ds_reason or "DataSentinel backend unavailable.",
            )
        if not pl_available:
            return _backend_unavailable_report(
                method_name=self.method_name,
                source_path=source_path,
                text=text,
                component="PromptLocate",
                implementation_kind="upstream_pipeline",
                reason=pl_reason or "PromptLocate backend unavailable.",
            )

        try:
            contaminated = self.adapter.detect(text)
        except BackendUnavailableError as exc:
            return _backend_unavailable_report(
                method_name=self.method_name,
                source_path=source_path,
                text=text,
                component="DataSentinel",
                implementation_kind="upstream_pipeline",
                reason=str(exc),
            )

        skeleton_report = self.skeleton_builder.apply(
            text,
            source_path=source_path,
            injection_meta=injection_meta,
        )
        target_instruction = skeleton_report["filtered_text"]

        if not contaminated:
            return {
                "method": self.method_name,
                "defense_family": "open_prompt_injection",
                "implementation_kind": "upstream_pipeline",
                "backend": "open_prompt_injection",
                "input_path": source_path,
                "contaminated": False,
                "blocked": False,
                "allowed_to_proceed": True,
                "rewritten": False,
                "action": "passed",
                "contamination_score": 0.0,
                "suspicious_spans": skeleton_report.get("suspicious_spans", []),
                "trusted_text": text,
                "untrusted_text": "",
                "filtered_text": text,
                "dropped_char_count": 0,
                "notes": [
                    "Upstream Open-Prompt-Injection pipeline.",
                    "DataSentinel marked this document clean, so PromptLocate was not called.",
                ],
                "target_instruction": target_instruction,
                "localization_candidate": "",
                "localized_text": "",
                "recovered_text": text,
            }

        candidate = skeleton_report["untrusted_text"] or text
        try:
            recovered_text, localized_text = self.adapter.locate_and_recover(candidate, target_instruction)
        except BackendUnavailableError as exc:
            return _backend_unavailable_report(
                method_name=self.method_name,
                source_path=source_path,
                text=text,
                component="PromptLocate",
                implementation_kind="upstream_pipeline",
                reason=str(exc),
            )

        filtered_text = _merge_skeleton_and_residual(target_instruction, recovered_text)
        return {
            "method": self.method_name,
            "defense_family": "open_prompt_injection",
            "implementation_kind": "upstream_pipeline",
            "backend": "open_prompt_injection",
            "input_path": source_path,
            "contaminated": True,
            "blocked": False,
            "allowed_to_proceed": True,
            "rewritten": True,
            "action": "recovered",
            "contamination_score": 1.0,
            "suspicious_spans": skeleton_report.get("suspicious_spans", []),
            "trusted_text": skeleton_report["trusted_text"],
            "untrusted_text": skeleton_report["untrusted_text"],
            "filtered_text": filtered_text,
            "dropped_char_count": max(0, len(text) - len(filtered_text)),
            "notes": [
                "Upstream Open-Prompt-Injection pipeline.",
                "DataSentinel marked this document contaminated, then PromptLocate localized and recovered the suspicious layer.",
            ],
            "target_instruction": target_instruction,
            "localization_candidate": candidate,
            "localized_text": localized_text,
            "recovered_text": recovered_text,
            "trusted_sections": skeleton_report.get("trusted_sections", []),
            "untrusted_sections": skeleton_report.get("untrusted_sections", []),
        }
