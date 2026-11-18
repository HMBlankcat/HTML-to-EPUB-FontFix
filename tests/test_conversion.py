import base64
import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from lxml import etree, html
from PIL import Image

from html_to_epub.converter import convert, validate_epub
from html_to_epub.fonts import FontDecodeError


def font_bytes(character):
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "shape"])
    pen = TTGlyphPen(None)
    pen.moveTo((50, 50))
    pen.lineTo((50, 800))
    pen.lineTo((700, 800))
    pen.lineTo((450, 50))
    pen.closePath()
    empty = TTGlyphPen(None)
    fb.setupCharacterMap({ord(character): "shape"})
    fb.setupGlyf({".notdef": empty.glyph(), "shape": pen.glyph()})
    fb.setupHorizontalMetrics({".notdef": (1000, 0), "shape": (1000, 50)})
    fb.setupHorizontalHeader(ascent=900, descent=-100)
    fb.setupNameTable({"familyName": "Fixture", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=900, sTypoDescender=-100, usWinAscent=900, usWinDescent=100)
    fb.setupPost()
    fb.setupMaxp()
    stream = BytesIO()
    fb.save(stream)
    return stream.getvalue()


def make_html(tmp_path, body, style="", name="input.html"):
    path = tmp_path / name
    path.write_text(f'<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><title>测试书</title><style>{style}</style></head><body>{body}</body></html>', "utf-8")
    return path


def test_real_text_scoped_font_and_tail(tmp_path):
    # A source code point has the shape of a DIFFERENT reference character.
    encoded = base64.b64encode(font_bytes("易")).decode()
    reference = tmp_path / "reference.ttf"
    reference.write_bytes(font_bytes("漫"))
    css = f'@font-face{{font-family:encoded;src:url("data:font/ttf;base64,{encoded}")}} .encoded{{font-family:encoded}}'
    source = make_html(tmp_path, '<div class="chapter" id="c1"><h2>第一章</h2><p class="encoded">易<span style="font-family:serif">易</span>易</p><a href="#c2">下一章</a></div><div class="chapter" id="c2"><h2>第二章</h2><p>易</p></div>', css)
    output = tmp_path / "book.epub"
    report = convert(source, output, reference_fonts=[reference], log=lambda _: None)
    assert report["chapter_count"] == 2
    assert report["replaced_characters"] == 2
    assert report["copy_search_correct"] is True
    with ZipFile(output) as archive:
        chapter = etree.fromstring(archive.read("EPUB/chapter-0001.xhtml"))
        assert "漫易漫" in "".join(chapter.itertext())
        assert b'chapter-0002.xhtml#c2' in archive.read("EPUB/chapter-0001.xhtml")
        assert "易" in archive.read("EPUB/chapter-0002.xhtml").decode()
        assert not any(name.startswith("EPUB/fonts/") for name in archive.namelist())
    validate_epub(output)


def test_unknown_glyph_stops_and_reports(tmp_path):
    encoded = base64.b64encode(font_bytes("易")).decode()
    source = make_html(tmp_path, '<p style="font-family:unknown">易</p>',
                       f'@font-face{{font-family:unknown;src:url("data:font/ttf;base64,{encoded}")}}')
    target = tmp_path / "unverified.epub"
    with pytest.raises(FontDecodeError):
        convert(source, target, log=lambda _: None)
    assert not target.exists()
    report = json.loads(target.with_suffix(".report.json").read_text("utf-8"))
    assert report["status"] == "failed"
    assert report["unresolved_fonts"][0]["unresolved"] == "易"


def test_images_cover_script_removal_and_restored_html(tmp_path):
    image = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image, "PNG")
    data = base64.b64encode(image.getvalue()).decode()
    src = "data:image/png;base64," + data
    source = make_html(tmp_path, f'<div class="cover-img"><img src="{src}"></div><div class="meta-info">作者: 测试作者</div><div class="chapter"><h2>217. 贝尔格（4）</h2><p>漫长的悼词结束后，葬礼的最后一个环节终于开始。</p><img src="{src}" onload="alert(1)"><script>alert(1)</script></div>')
    restored = tmp_path / "restored.html"
    target = tmp_path / "book.epub"
    result = convert(source, target, restored_html=restored, log=lambda _: None)
    assert result["image_count"] == 1  # Deduplicate cover and reused illustration.
    assert result["author"] == "测试作者"
    assert b"<script" not in restored.read_bytes()
    assert src.encode() in restored.read_bytes()
    assert "漫长的悼词结束后" in html.fromstring(restored.read_bytes()).text_content()
    with ZipFile(target) as archive:
        assert archive.read("mimetype") == b"application/epub+zip"
        assert archive.infolist()[0].compress_type == 0
        assert "217. 贝尔格（4）" in archive.read("EPUB/nav.xhtml").decode()
        assert b"cover-image" in archive.read("EPUB/package.opf")


def test_large_attribute_does_not_silently_drop_later_chapters(tmp_path):
    source = make_html(tmp_path, '<div title="' + "x" * 10_100_000 + '"></div><div class="chapter"><h2>后面的章节</h2><p>不能丢失。</p></div>')
    target = tmp_path / "book.epub"
    result = convert(source, target, log=lambda _: None)
    assert result["chapter_count"] == 1
    with ZipFile(target) as archive:
        assert "不能丢失。" in archive.read("EPUB/chapter-0001.xhtml").decode()


def test_existing_output_is_not_overwritten(tmp_path):
    source = make_html(tmp_path, "<p>正文</p>")
    target = tmp_path / "book.epub"
    target.write_bytes(b"existing output")
    with pytest.raises(FileExistsError):
        convert(source, target, log=lambda _: None)
    assert target.read_bytes() == b"existing output"


def test_visual_mode_embeds_sfnt_and_labels_copy_limitation(tmp_path):
    encoded = base64.b64encode(font_bytes("易")).decode()
    source = make_html(tmp_path, '<p style="font-family:encoded">易</p>',
                       f'@font-face{{font-family:encoded;src:url("data:font/ttf;base64,{encoded}")}}')
    output = tmp_path / "visual.epub"
    result = convert(source, output, mode="visual", log=lambda _: None)
    assert result["copy_search_correct"] is False
    with ZipFile(output) as archive:
        assert "易" in archive.read("EPUB/chapter-0001.xhtml").decode()
        assert any(name.endswith(".ttf") for name in archive.namelist())
