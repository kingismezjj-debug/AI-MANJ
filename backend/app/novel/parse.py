import io
import re
import tempfile
import zipfile
from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup
from docx import Document
from ebooklib import epub


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_docx(data: bytes) -> str:
    document = Document(io.BytesIO(data))
    parts = [p.text.strip() for p in document.paragraphs if p.text and p.text.strip()]
    return "\n".join(parts)


def parse_epub(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".epub") as handle:
        handle.write(data)
        handle.flush()
        book = epub.read_epub(handle.name)
    chunks: list[str] = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        text = soup.get_text("\n")
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def parse_bytes(filename: str, data: bytes) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".docx" or data[:2] == b"PK" and suffix != ".epub":
        # docx is a zip; epub is also a zip and is handled by suffix first.
        if suffix == ".docx":
            return parse_docx(data)
    if suffix == ".epub" or (data[:2] == b"PK" and _looks_like_epub(data)):
        return parse_epub(data)
    if suffix == ".docx":
        return parse_docx(data)
    return decode_text(data)


def _looks_like_epub(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return "META-INF/container.xml" in archive.namelist()
    except zipfile.BadZipFile:
        return False


def chunk_text(text: str, size: int = 800, overlap: int = 80) -> list[str]:
    body = text.replace("\r\n", "\n").strip()
    if not body:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(body):
        end = min(len(body), start + size)
        chunks.append(body[start:end])
        if end >= len(body):
            break
        start = max(0, end - overlap)
    return chunks


def char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))
