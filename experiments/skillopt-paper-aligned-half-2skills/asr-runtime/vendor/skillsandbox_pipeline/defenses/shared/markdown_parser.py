from __future__ import annotations

import re

from .types import MarkdownBlock, SectionBodyBlock


def parse_frontmatter(frontmatter_text: str) -> dict[str, str]:
    """Parse simple YAML frontmatter key/value pairs without external deps."""
    result: dict[str, str] = {}
    if not frontmatter_text:
        return result

    lines = frontmatter_text.splitlines()
    if len(lines) >= 2 and lines[0].strip() == "---":
        lines = lines[1:]
    if lines and lines[-1].strip() == "---":
        lines = lines[:-1]

    current_key: str | None = None
    current_value: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            continue
        if not raw_line.startswith((" ", "\t")) and ":" in line:
            if current_key is not None:
                result[current_key] = "\n".join(current_value).strip().strip("\"'")
            key, value = line.split(":", 1)
            current_key = key.strip()
            current_value = [value.strip()]
        elif current_key is not None:
            current_value.append(line.strip())

    if current_key is not None:
        result[current_key] = "\n".join(current_value).strip().strip("\"'")

    return result


def parse_markdown_blocks(text: str) -> list[MarkdownBlock]:
    """
    Parse markdown into stable blocks:
    - frontmatter
    - preamble
    - heading sections

    The parser is code-fence aware so headings inside fenced code blocks are ignored.
    """
    if not text:
        return []

    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    blocks: list[MarkdownBlock] = []
    next_index = 0
    line_start = 0
    frontmatter_end = 0

    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                frontmatter_end = offsets[i] + len(lines[i])
                blocks.append(
                    MarkdownBlock(
                        index=next_index,
                        kind="frontmatter",
                        text=text[:frontmatter_end],
                        start=0,
                        end=frontmatter_end,
                    )
                )
                next_index += 1
                line_start = i + 1
                break

    headings: list[tuple[int, int, str]] = []
    in_fence = False
    fence_marker = ""
    for i in range(line_start, len(lines)):
        raw_line = lines[i]
        stripped = raw_line.lstrip()

        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif stripped.startswith(fence_marker):
                in_fence = False
            continue

        if in_fence:
            continue

        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", raw_line)
        if match:
            headings.append((i, len(match.group(1)), match.group(2).strip()))

    content_start = frontmatter_end
    if not headings:
        remainder = text[content_start:]
        if remainder.strip():
            blocks.append(
                MarkdownBlock(
                    index=next_index,
                    kind="preamble",
                    text=remainder,
                    start=content_start,
                    end=len(text),
                )
            )
        return blocks

    first_heading_start = offsets[headings[0][0]]
    if text[content_start:first_heading_start].strip():
        blocks.append(
            MarkdownBlock(
                index=next_index,
                kind="preamble",
                text=text[content_start:first_heading_start],
                start=content_start,
                end=first_heading_start,
            )
        )
        next_index += 1

    for idx, (line_idx, level, title) in enumerate(headings):
        start = offsets[line_idx]
        end = offsets[headings[idx + 1][0]] if idx + 1 < len(headings) else len(text)
        body_start = offsets[line_idx] + len(lines[line_idx])
        blocks.append(
            MarkdownBlock(
                index=next_index,
                kind="section",
                text=text[start:end],
                start=start,
                end=end,
                heading=title,
                level=level,
                body_start=body_start,
            )
        )
        next_index += 1

    return blocks


def parse_section_body_blocks(text: str, *, base_offset: int = 0) -> list[SectionBodyBlock]:
    """Parse a heading body into coarse block kinds."""
    if not text:
        return []

    blocks: list[SectionBodyBlock] = []
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if _is_fence_start(line):
            start = offsets[i]
            fence_marker = line.lstrip()[:3]
            i += 1
            while i < len(lines):
                if lines[i].lstrip().startswith(fence_marker):
                    i += 1
                    break
                i += 1
            end = offsets[i] if i < len(lines) else len(text)
            blocks.append(
                SectionBodyBlock(
                    kind="fenced_code",
                    text=text[start:end],
                    start=base_offset + start,
                    end=base_offset + end,
                )
            )
            continue

        if _looks_like_table_line(line):
            start = offsets[i]
            i += 1
            while i < len(lines) and _looks_like_table_line(lines[i]):
                i += 1
            end = offsets[i] if i < len(lines) else len(text)
            blocks.append(
                SectionBodyBlock(
                    kind="table",
                    text=text[start:end],
                    start=base_offset + start,
                    end=base_offset + end,
                )
            )
            continue

        list_kind = _list_kind(line)
        if list_kind:
            start = offsets[i]
            i += 1
            while i < len(lines):
                current_kind = _list_kind(lines[i])
                if current_kind and current_kind == list_kind:
                    i += 1
                    continue
                if lines[i].strip() == "":
                    i += 1
                    continue
                break
            end = offsets[i] if i < len(lines) else len(text)
            blocks.append(
                SectionBodyBlock(
                    kind=list_kind,
                    text=text[start:end],
                    start=base_offset + start,
                    end=base_offset + end,
                )
            )
            continue

        start = offsets[i]
        i += 1
        while i < len(lines):
            if not lines[i].strip():
                i += 1
                break
            if _is_fence_start(lines[i]) or _looks_like_table_line(lines[i]) or _list_kind(lines[i]):
                break
            i += 1
        end = offsets[i] if i < len(lines) else len(text)
        blocks.append(
            SectionBodyBlock(
                kind="paragraph",
                text=text[start:end],
                start=base_offset + start,
                end=base_offset + end,
            )
        )

    return blocks


def _is_fence_start(line: str) -> bool:
    return line.lstrip().startswith(("```", "~~~"))


def _looks_like_table_line(line: str) -> bool:
    stripped = line.strip()
    if "|" not in stripped:
        return False
    if stripped.startswith(("-", "*", "+")):
        return False
    return True


def _list_kind(line: str) -> str | None:
    stripped = line.lstrip()
    if re.match(r"^[-*+]\s+\[[ xX]\]\s+", stripped):
        return "checklist"
    if re.match(r"^[-*+]\s+", stripped):
        return "list"
    if re.match(r"^\d+[\.\)]\s+", stripped):
        return "numbered_list"
    return None
