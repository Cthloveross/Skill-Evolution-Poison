from __future__ import annotations

from defenses.shared.markdown_parser import parse_markdown_blocks
from defenses.shared.types import FilterResult, MarkdownBlock

from .common import (
    NAIVE_SECTION_TITLE,
    find_control_marker_start,
    find_pattern_hits,
    is_suspicious_title,
    line_ranges,
    suspicion_score,
)


class OPIFilter:
    """
    Legacy heuristic OPI-style filter for naive prompt injection.

    Modes:
    - opi_rules_only: detect, but do not modify the document
    - opi_detect_drop: drop suspicious sections outright
    - opi_detect_locate: drop suspicious sections, or line-prune suspicious content
    """

    def __init__(self, mode: str = "opi_detect_locate"):
        self.mode = mode

    def apply(
        self,
        text: str,
        *,
        source_path: str = "",
        injection_meta: dict | None = None,
    ) -> FilterResult:
        blocks = parse_markdown_blocks(text)
        filtered_parts: list[str] = []
        trusted_parts: list[str] = []
        untrusted_parts: list[str] = []
        suspicious_spans: list[dict] = []
        block_reports: list[dict] = []
        contamination_score = 0.0

        for block in blocks:
            score = suspicion_score(block.text, block.heading)
            hits = find_pattern_hits(block.text)
            suspicious = self._is_suspicious_block(block, score, hits)
            contamination_score = max(contamination_score, float(score))

            block_report = {
                "index": block.index,
                "kind": block.kind,
                "heading": block.heading,
                "start": block.start,
                "end": block.end,
                "score": score,
                "hits": hits,
                "suspicious": suspicious,
            }

            if suspicious:
                suspicious_spans.append(
                    {
                        "start": block.start,
                        "end": block.end,
                        "heading": block.heading,
                        "reason": "suspicious_block",
                        "score": score,
                        "hits": hits,
                    }
                )
                untrusted_parts.append(block.text)

                if self.mode == "opi_rules_only":
                    filtered_parts.append(block.text)
                    trusted_parts.append(block.text)
                    block_report["action"] = "kept_for_detection_only"
                elif self.mode == "opi_detect_drop":
                    block_report["action"] = "dropped_block"
                else:
                    recovered_text, localized = self._recover_block(block)
                    block_report["action"] = "recovered" if recovered_text else "dropped_block"
                    if localized:
                        block_report["localized_spans"] = localized
                        suspicious_spans.extend(localized)
                    if recovered_text:
                        filtered_parts.append(recovered_text)
                        trusted_parts.append(recovered_text)
            else:
                filtered_parts.append(block.text)
                trusted_parts.append(block.text)
                block_report["action"] = "kept"

            block_reports.append(block_report)

        filtered_text = "".join(filtered_parts)
        trusted_text = "".join(trusted_parts)
        untrusted_text = "".join(untrusted_parts)
        contaminated = any(block["suspicious"] for block in block_reports)

        notes = [
            "Legacy heuristic OPI filter kept only for comparison.",
            f"Mode={self.mode}.",
        ]
        if injection_meta and injection_meta.get("strategy") == "naive":
            notes.append("Injection metadata confirms naive strategy.")

        return {
            "method": self.mode,
            "defense_family": "open_prompt_injection",
            "implementation_kind": "legacy_proxy",
            "backend": "legacy_proxy",
            "input_path": source_path,
            "contaminated": contaminated,
            "blocked": False,
            "allowed_to_proceed": True,
            "rewritten": filtered_text != text,
            "action": "detection_only" if self.mode == "opi_rules_only" else "legacy_filtered",
            "contamination_score": contamination_score,
            "suspicious_spans": suspicious_spans,
            "trusted_text": trusted_text,
            "untrusted_text": untrusted_text,
            "filtered_text": filtered_text,
            "dropped_char_count": max(0, len(text) - len(filtered_text)),
            "notes": notes,
            "blocks": block_reports,
        }

    def _is_suspicious_block(self, block: MarkdownBlock, score: int, hits: list[str]) -> bool:
        if is_suspicious_title(block.heading):
            return True
        if block.kind == "section" and score >= 3:
            return True
        if block.kind in {"preamble", "frontmatter"} and score >= 4:
            return True
        if "role_override" in hits and "strict_protocol" in hits:
            return True
        return False

    def _recover_block(self, block: MarkdownBlock) -> tuple[str, list[dict]]:
        if is_suspicious_title(block.heading) or block.heading == NAIVE_SECTION_TITLE:
            return "", []

        marker_start = find_control_marker_start(block.text)
        if marker_start is not None:
            prefix = block.text[:marker_start].rstrip()
            if prefix:
                prefix += "\n"
            return prefix, [
                {
                    "start": block.start + marker_start,
                    "end": block.end,
                    "heading": block.heading,
                    "reason": "control_marker_tail",
                    "hits": find_pattern_hits(block.text[marker_start:]),
                }
            ]

        kept_lines: list[str] = []
        localized_spans: list[dict] = []
        for start, end, line in line_ranges(block.text, base_offset=block.start):
            hits = find_pattern_hits(line)
            if hits:
                localized_spans.append(
                    {
                        "start": start,
                        "end": end,
                        "heading": block.heading,
                        "reason": "suspicious_line",
                        "hits": hits,
                    }
                )
                continue
            kept_lines.append(line)

        cleaned = "".join(kept_lines)
        return cleaned, localized_spans
