"""Split a novel into chapters without dropping the tail.

A book longer than one model context is cut into parts that are all sent.
Nothing is discarded because it sat past a fixed character cap.
"""

import re

# A heading is a short line by itself. A long paragraph that merely starts
# with 第N章 is body text, not a new chapter. That was splitting one book
# into thousands of chunks.
HEADING = re.compile(
    r"(?m)^(?:第\s*[0-9０-９一二三四五六七八九十百千零两]{1,16}\s*[章节回部][^\n]{0,20}|Chapter\s+\d{1,5}\b[^\n]{0,24})\s*$"
)
EPISODE_CAP = 100


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


def suggest_episode_count(chapter_count: int) -> int:
    """One episode per chapter while that stays a sane 漫剧 length, else 80."""
    count = max(0, int(chapter_count))
    if count <= 1:
        return 1
    if count <= 80:
        return count
    return 80


def resolved_plan(chapter_count: int, settings: dict | None) -> dict:
    settings = settings or {}
    count = max(1, int(chapter_count or 1))
    cap = min(EPISODE_CAP, count)
    if (settings.get("episode_mode") or "fixed") == "auto" or not settings.get("episode_count"):
        episodes = suggest_episode_count(count)
    else:
        episodes = int(settings.get("episode_count") or 1)
    episodes = max(1, min(cap, episodes))
    trial = int(settings.get("produce_first") or 2)
    trial = max(1, min(episodes, trial))
    return {
        "chapters": count,
        "episodes": episodes,
        "trial": trial,
        "suggested": suggest_episode_count(count),
    }


def slice_chapters(chapters: list[str], start: int | None, end: int | None) -> list[str]:
    if not chapters:
        return []
    first = max(1, int(start or 1))
    last = int(end or len(chapters))
    last = min(len(chapters), max(first, last))
    first = min(first, last)
    return chapters[first - 1 : last]


def group_chapters(chapter_count: int, episode_count: int) -> list[tuple[int, int]]:
    """Half-open ranges of source chapters, one range per episode."""
    count = max(1, int(chapter_count or 1))
    episodes = max(1, min(int(episode_count or 1), count))
    groups = []
    for index in range(episodes):
        start = index * count // episodes
        end = (index + 1) * count // episodes
        if start < end:
            groups.append((start, end))
    return groups


def llm_segments(text: str, size: int = 24000, chapters: list[str] | None = None) -> list[tuple[int, str]]:
    """Merge short chapters up to `size` characters. A long chapter is split.

    The index is the first source chapter in that window.
    """
    source = chapters if chapters is not None else split_chapters(text)
    segments: list[tuple[int, str]] = []
    overlap = 400 if size >= 8000 else 200
    bucket: list[str] = []
    bucket_len = 0
    bucket_index = 0

    def flush() -> None:
        nonlocal bucket, bucket_len
        if bucket:
            segments.append((bucket_index, "\n\n".join(bucket)))
            bucket = []
            bucket_len = 0

    for index, chapter in enumerate(source):
        if len(chapter) > size:
            flush()
            start = 0
            while start < len(chapter):
                segments.append((index, chapter[start : start + size]))
                if start + size >= len(chapter):
                    break
                start += max(1, size - overlap)
            continue
        if bucket and bucket_len + len(chapter) > size:
            flush()
        if not bucket:
            bucket_index = index
        bucket.append(chapter)
        bucket_len += len(chapter)
    flush()
    return segments


def llm_parts(text: str, size: int = 24000) -> list[str]:
    return [part for _chapter, part in llm_segments(text, size=size)]
