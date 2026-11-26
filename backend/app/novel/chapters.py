"""Split a novel into chapters without dropping the tail.

A book longer than one model context is cut into parts that are all sent.
Nothing is discarded because it sat past a fixed character cap.
"""

import re

HEADING = re.compile(
    r"(?m)^(第[0-9０-９一二三四五六七八九十百千零两]+[章节回部]|Chapter\s+\d+)\s*.*$"
)


def split_chapters(text: str) -> list[str]:
    body = (text or "").replace("\r\n", "\n").strip()
    if not body:
        return []
    marks = list(HEADING.finditer(body))
    if not marks:
        return [body]
    parts: list[str] = []
    if marks[0].start() > 0:
        preamble = body[: marks[0].start()].strip()
        if preamble:
            parts.append(preamble)
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        piece = body[mark.start() : end].strip()
        if piece:
            parts.append(piece)
    return parts or [body]


def skim_chapters(chapters: list[str]) -> list[dict]:
    """Cheap whole-book pass: title plus the opening and closing lines. No model call."""
    skim = []
    for index, chapter in enumerate(chapters):
        lines = [line.strip() for line in chapter.splitlines() if line.strip()]
        skim.append(
            {
                "chapter": index,
                "title": (lines[0] if lines else f"第{index + 1}章")[:40],
                "head": "\n".join(lines[:3])[:280],
                "tail": "\n".join(lines[-2:])[:180],
            }
        )
    return skim


def llm_segments(text: str, size: int = 24000) -> list[tuple[int, str]]:
    """Chapter index plus text. A chapter that fits in one context window is
    one call. Longer chapters are cut into large windows, not 6k slices."""
    segments: list[tuple[int, str]] = []
    overlap = 400 if size >= 8000 else 200
    for index, chapter in enumerate(split_chapters(text)):
        if len(chapter) <= size:
            segments.append((index, chapter))
            continue
        start = 0
        while start < len(chapter):
            segments.append((index, chapter[start : start + size]))
            if start + size >= len(chapter):
                break
            start += max(1, size - overlap)
    return segments


def llm_parts(text: str, size: int = 24000) -> list[str]:
    return [part for _chapter, part in llm_segments(text, size=size)]
