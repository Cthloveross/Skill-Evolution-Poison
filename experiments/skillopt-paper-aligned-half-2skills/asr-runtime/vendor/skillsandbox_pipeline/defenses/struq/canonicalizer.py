from __future__ import annotations

from defenses.shared.markdown_parser import (
    parse_frontmatter,
    parse_markdown_blocks,
    parse_section_body_blocks,
)
from defenses.shared.types import FilterResult, MarkdownBlock, SectionBodyBlock

from .schema import (
    DOCUMENT_TITLE_BUCKET,
    bucket_for_title,
    bucket_order,
    frontmatter_fields,
    policy_for_bucket,
)


class StruQCanonicalizer:
    """
    Structure-first StruQ canonicalizer.

    Public StruQ output is intentionally minimal:
    - filtered_text
    - suspicious_spans
    - suspicious

    The richer skeleton view is still available internally for OPI via
    method="struq_skeleton".
    """

    def __init__(self, method: str = "struq_fixed"):
        self.method = method
        self.mode = "skeleton" if method == "struq_skeleton" else "public"

    def apply(
        self,
        text: str,
        *,
        source_path: str = "",
        injection_meta: dict | None = None,
    ) -> FilterResult:
        blocks = parse_markdown_blocks(text)
        frontmatter_block = next((block for block in blocks if block.kind == "frontmatter"), None)
        preamble_block = next((block for block in blocks if block.kind == "preamble"), None)
        section_blocks = [block for block in blocks if block.kind == "section"]

        frontmatter = parse_frontmatter(frontmatter_block.text) if frontmatter_block else {}
        skill_name = frontmatter.get("name", "").strip()
        description = frontmatter.get("description", "").strip()
        document_title = ""

        trusted_sections: list[dict] = []
        untrusted_sections: list[dict] = []
        trusted_fragments: list[str] = []
        untrusted_fragments: list[str] = []
        suspicious_spans: list[dict] = []

        allowed_frontmatter = set(frontmatter_fields(self.mode))
        if frontmatter_block and frontmatter:
            unexpected_keys = [key for key in frontmatter if key not in allowed_frontmatter]
            if unexpected_keys:
                span = {
                    "start": frontmatter_block.start,
                    "end": frontmatter_block.end,
                    "heading": "__frontmatter__",
                    "reason": "unexpected_frontmatter_keys",
                    "text": frontmatter_block.text,
                    "details": {"unexpected_keys": unexpected_keys},
                }
                suspicious_spans.append(span)
                untrusted_sections.append(span | {"bucket": "Suspicious", "level": 0})
                untrusted_fragments.append(frontmatter_block.text)

        if preamble_block and preamble_block.text.strip():
            preamble_result = self._handle_preamble(
                preamble_block,
                description=description,
            )
            if preamble_result["trusted_entry"] is not None:
                trusted_sections.append(preamble_result["trusted_entry"])
                trusted_fragments.append(preamble_result["trusted_text"])
                if not description:
                    description = preamble_block.text.strip()
            for entry in preamble_result["untrusted_entries"]:
                untrusted_sections.append(entry)
                suspicious_spans.append(_span_from_entry(entry))
                if entry.get("text"):
                    untrusted_fragments.append(entry["text"])

        ancestor_stack: list[tuple[int, str | None]] = []
        for block in section_blocks:
            while ancestor_stack and (block.level or 0) <= ancestor_stack[-1][0]:
                ancestor_stack.pop()

            inherited_bucket = ancestor_stack[-1][1] if ancestor_stack else None
            trusted_entry, untrusted_entries, trusted_text = self._classify_section(
                block,
                inherited_bucket=inherited_bucket,
            )

            if trusted_entry:
                if trusted_entry["bucket"] == DOCUMENT_TITLE_BUCKET:
                    document_title = trusted_entry["heading"] or document_title
                else:
                    trusted_sections.append(trusted_entry)
                if trusted_text:
                    trusted_fragments.append(trusted_text)
                ancestor_stack.append((block.level or 0, trusted_entry["bucket"]))
            else:
                ancestor_stack.append((block.level or 0, None))

            for entry in untrusted_entries:
                untrusted_sections.append(entry)
                suspicious_spans.append(_span_from_entry(entry))
                if entry.get("text"):
                    untrusted_fragments.append(entry["text"])

        if not skill_name:
            skill_name = document_title

        filtered_text = self._render_canonical(
            name=skill_name,
            description=description,
            trusted_sections=trusted_sections,
        )

        if self.mode == "public":
            return {
                "filtered_text": filtered_text,
                "suspicious_spans": suspicious_spans,
                "suspicious": bool(suspicious_spans),
            }

        return {
            "method": self.method,
            "backend": "struq_skeleton",
            "filtered_text": filtered_text,
            "suspicious_spans": suspicious_spans,
            "trusted_text": "".join(trusted_fragments),
            "untrusted_text": "".join(untrusted_fragments),
            "trusted_sections": [self._section_summary(section) for section in trusted_sections],
            "untrusted_sections": [self._section_summary(section) for section in untrusted_sections],
            "contaminated": bool(suspicious_spans),
            "blocked": False,
            "allowed_to_proceed": True,
            "rewritten": True,
            "action": "canonicalized",
            "notes": [
                "Internal StruQ skeleton view for Open-Prompt-Injection.",
                "This richer report is not the public struq_fixed interface.",
            ],
        }

    def _handle_preamble(self, block: MarkdownBlock, *, description: str) -> dict:
        if self.mode == "skeleton":
            if not description and _is_short_paragraph(block.text):
                return {
                    "trusted_entry": {
                        "heading": "__preamble__",
                        "bucket": "Purpose / Overview",
                        "start": block.start,
                        "end": block.end,
                        "reason": "trusted_preamble",
                        "classification": "trusted",
                        "text": block.text,
                        "trusted_body_text": block.text,
                        "level": 0,
                    },
                    "trusted_text": block.text,
                    "untrusted_entries": [],
                }
            entry = {
                "heading": "__preamble__",
                "bucket": "Suspicious",
                "start": block.start,
                "end": block.end,
                "reason": "unexpected_preamble",
                "classification": "untrusted",
                "text": block.text,
                "level": 0,
            }
            return {"trusted_entry": None, "trusted_text": "", "untrusted_entries": [entry]}

        body_blocks = parse_section_body_blocks(block.text, base_offset=block.start)
        if not body_blocks:
            return {"trusted_entry": None, "trusted_text": "", "untrusted_entries": []}

        policy = policy_for_bucket("Purpose / Overview", mode="public")
        if policy and all(part.kind in policy.allowed_block_kinds for part in body_blocks):
            trusted_entry = {
                "heading": "Overview",
                "bucket": "Purpose / Overview",
                "start": block.start,
                "end": block.end,
                "reason": "trusted_preamble",
                "classification": "trusted",
                "text": block.text,
                "trusted_body_text": block.text,
                "level": 0,
            }
            return {
                "trusted_entry": trusted_entry,
                "trusted_text": block.text,
                "untrusted_entries": [],
            }

        entry = {
            "heading": "__preamble__",
            "bucket": "Suspicious",
            "start": block.start,
            "end": block.end,
            "reason": "unexpected_preamble",
            "classification": "untrusted",
            "text": block.text,
            "level": 0,
        }
        return {"trusted_entry": None, "trusted_text": "", "untrusted_entries": [entry]}

    def _classify_section(
        self,
        block: MarkdownBlock,
        *,
        inherited_bucket: str | None,
    ) -> tuple[dict | None, list[dict], str]:
        heading = block.heading or ""
        heading_line, body_text = _split_heading_and_body(block)

        if (block.level or 0) == 1:
            return (
                {
                    "heading": heading,
                    "bucket": DOCUMENT_TITLE_BUCKET,
                    "start": block.start,
                    "end": block.end,
                    "reason": "document_title",
                    "classification": "trusted",
                    "text": block.text,
                    "trusted_body_text": "",
                    "level": block.level,
                },
                [],
                block.text,
            )

        direct_bucket = bucket_for_title(heading, mode=self.mode)
        bucket = direct_bucket
        if bucket is None and inherited_bucket:
            inherited_policy = policy_for_bucket(inherited_bucket, mode=self.mode)
            if inherited_policy and inherited_policy.allow_inherited_subheadings:
                bucket = inherited_bucket

        body_blocks = parse_section_body_blocks(body_text, base_offset=block.body_start or block.start)
        if bucket is None:
            entry = {
                "heading": heading,
                "bucket": "Suspicious",
                "start": block.start,
                "end": block.end,
                "reason": "unknown_heading",
                "classification": "untrusted",
                "text": block.text,
                "level": block.level,
            }
            return None, [entry], ""

        policy = policy_for_bucket(bucket, mode=self.mode)
        if policy is None:
            entry = {
                "heading": heading,
                "bucket": "Suspicious",
                "start": block.start,
                "end": block.end,
                "reason": "unknown_heading",
                "classification": "untrusted",
                "text": block.text,
                "level": block.level,
            }
            return None, [entry], ""

        trusted_body_parts: list[str] = []
        untrusted_entries: list[dict] = []

        for body_block in body_blocks:
            normalized_kind = _normalize_block_kind(body_block, bucket, mode=self.mode)
            allowed, reason = _is_allowed_body_block(body_block, normalized_kind, policy, mode=self.mode)
            if allowed:
                trusted_body_parts.append(body_block.text)
            else:
                untrusted_entries.append(
                    {
                        "heading": heading,
                        "bucket": "Suspicious",
                        "start": body_block.start,
                        "end": body_block.end,
                        "reason": reason,
                        "classification": "untrusted",
                        "text": body_block.text,
                        "level": block.level,
                        "details": {"block_kind": normalized_kind},
                    }
                )

        trusted_text = heading_line + "".join(trusted_body_parts)
        trusted_entry = {
            "heading": heading,
            "bucket": bucket,
            "start": block.start,
            "end": block.end,
            "reason": "mapped_section" if direct_bucket else "inherited_bucket",
            "classification": "trusted",
            "text": trusted_text,
            "trusted_body_text": "".join(trusted_body_parts),
            "level": block.level,
        }
        return trusted_entry, untrusted_entries, trusted_text

    def _render_canonical(
        self,
        *,
        name: str,
        description: str,
        trusted_sections: list[dict],
    ) -> str:
        lines: list[str] = ["---"]
        if name:
            lines.append(f'name: "{_escape_scalar(name)}"')
        if description:
            lines.append(f'description: "{_escape_scalar(description)}"')
        lines.append("---")
        lines.append("")
        lines.append(f"# {name or 'Canonical Skill'}")
        lines.append("")

        bucketed: dict[str, list[dict]] = {}
        for section in trusted_sections:
            bucket = section["bucket"]
            if bucket == DOCUMENT_TITLE_BUCKET:
                continue
            bucketed.setdefault(bucket, []).append(section)

        if description and "Purpose / Overview" not in bucketed:
            lines.append("## Purpose / Overview")
            lines.append("")
            lines.append(description)
            lines.append("")

        for bucket in bucket_order(self.mode):
            sections = bucketed.get(bucket, [])
            if not sections:
                continue
            lines.append(f"## {bucket}")
            lines.append("")
            for section in sections:
                heading = section.get("heading", "")
                body = section.get("trusted_body_text", "").strip()
                if heading and heading != "__preamble__":
                    lines.append(f"### {heading}")
                    lines.append("")
                if body:
                    lines.append(body)
                    lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def _section_summary(self, section: dict) -> dict:
        result = {
            "heading": section["heading"],
            "bucket": section["bucket"],
            "start": section["start"],
            "end": section["end"],
            "reason": section["reason"],
        }
        if section.get("details"):
            result["details"] = section["details"]
        return result


class StruQLiteFilter(StruQCanonicalizer):
    """Compatibility wrapper for legacy CLI names."""

    def __init__(self, mode: str = "struq_lite_strict"):
        super().__init__(method=mode)


def _split_heading_and_body(block: MarkdownBlock) -> tuple[str, str]:
    if block.body_start is None:
        return block.text, ""
    heading_len = max(0, block.body_start - block.start)
    return block.text[:heading_len], block.text[heading_len:]


def _normalize_block_kind(block: SectionBodyBlock, bucket: str, *, mode: str) -> str:
    if block.kind != "paragraph":
        return block.kind
    if bucket in {"Resources", "Resources / Related Skills"} and _looks_like_link_paragraph(block.text):
        return "link_paragraph"
    return block.kind


def _is_allowed_body_block(
    block: SectionBodyBlock,
    normalized_kind: str,
    policy,
    *,
    mode: str,
) -> tuple[bool, str]:
    if normalized_kind not in policy.allowed_block_kinds:
        return False, "disallowed_block_type"

    if normalized_kind == "paragraph":
        if policy.paragraph_mode == "short" and not _is_short_paragraph(block.text):
            return False, "unexpected_freeform_text"
        if policy.paragraph_mode == "none":
            return False, "disallowed_block_type"
        if policy.paragraph_mode == "link_only" and normalized_kind != "link_paragraph":
            return False, "unexpected_freeform_text"
    if policy.paragraph_mode == "link_only" and normalized_kind == "paragraph":
        return False, "unexpected_freeform_text"
    return True, "allowed"


def _is_short_paragraph(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    lines = [line for line in stripped.splitlines() if line.strip()]
    return len(lines) <= 3 and len(stripped) <= 280


def _looks_like_link_paragraph(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    return (
        "http://" in lowered
        or "https://" in lowered
        or "`" in stripped
        or ("[" in stripped and "](" in stripped)
        or lowered.startswith(("see ", "refer to ", "visit ", "resource:"))
    )


def _span_from_entry(entry: dict) -> dict:
    span = {
        "start": entry["start"],
        "end": entry["end"],
        "heading": entry["heading"],
        "reason": entry["reason"],
        "text": entry.get("text", ""),
    }
    if entry.get("details"):
        span["details"] = entry["details"]
    return span


def _escape_scalar(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
