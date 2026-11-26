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


def llm_parts(text: str, size: int = 6000) -> list[str]:
    """Every character of the book lands in some part. Windows overlap so a
    sentence on a boundary is not the only copy of itself."""
    parts: list[str] = []
    for chapter in split_chapters(text):
        if len(chapter) <= size:
            parts.append(chapter)
            continue
        start = 0
        while start < len(chapter):
            parts.append(chapter[start : start + size])
            if start + size >= len(chapter):
                break
            start += size - 200
    return parts
