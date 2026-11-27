import io
import zipfile

from docx import Document

from app.novel.parse import char_count, chunk_text, parse_bytes


def test_txt_gbk_and_chunks():
    raw = "林夏：雨还没停。\n陈默：那就站一会儿。".encode("gb18030")
    text = parse_bytes("novel.txt", raw)
    assert "林夏" in text
    assert char_count(text) > 8
    parts = chunk_text("甲" * 1000, size=400, overlap=40)
    assert len(parts) >= 3
    assert parts[0].startswith("甲")


def test_docx_roundtrip():
    document = Document()
    document.add_paragraph("旧楼的灯灭了。")
    document.add_paragraph("林夏：你还在吗？")
    buf = io.BytesIO()
    document.save(buf)
    text = parse_bytes("story.docx", buf.getvalue())
    assert "旧楼的灯灭了" in text
    assert "你还在吗" in text


def test_epub_roundtrip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
            <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
            </container>""",
        )
        archive.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0"?>
            <package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
              <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
                <dc:title>雨</dc:title><dc:language>zh</dc:language><dc:identifier id="id">1</dc:identifier>
              </metadata>
              <manifest><item id="c" href="ch.xhtml" media-type="application/xhtml+xml"/></manifest>
              <spine><itemref idref="c"/></spine>
            </package>""",
        )
        archive.writestr(
            "OEBPS/ch.xhtml",
            """<?xml version="1.0"?>
            <html xmlns="http://www.w3.org/1999/xhtml"><body><p>林夏把信收好。</p></body></html>""",
        )
    text = parse_bytes("book.epub", buf.getvalue())
    assert "林夏把信收好" in text
