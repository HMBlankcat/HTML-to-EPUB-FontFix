from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from lxml import etree, html
from PIL import Image
from fontTools.ttLib import TTFont

from .fonts import FontContext, Decoder, FontDecodeError, declarations, read_url
import tinycss2

XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"
OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
CSS = """html { line-height: 1.8; }
body { margin: 0 5%; padding: 0; overflow-wrap: break-word; }
h1, h2, h3 { line-height: 1.4; text-indent: 0; margin: 1em 0; }
p { margin: .65em 0; }
img { max-width: 100%; height: auto; }
.cover-img, .book-title { text-align: center; }
.cover-img img { max-height: 90vh; object-fit: contain; }
.chapter { page-break-before: always; }
.meta-info { white-space: pre-wrap; }
table { max-width: 100%; border-collapse: collapse; }
pre { white-space: pre-wrap; }
"""
XML_DECL = b'<?xml version="1.0" encoding="UTF-8"?>\n'


def classes(node, name):
    return node.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), $cls)]', cls=f" {name} ")


def clean(document):
    # HTML is content. No scripts, commands, forms or embedded frames are executed.
    for node in list(document.xpath("//script | //iframe | //object | //embed | //form | //noscript | //template")):
        node.drop_tree()
    for node in list(document.iter()):
        if not isinstance(node.tag, str):
            if node.getparent() is not None:
                node.getparent().remove(node)
            continue
        for attr in list(node.attrib):
            if attr.lower().startswith("on") or (attr.startswith("data-") and attr != "data-src") or attr in {"srcdoc", "contenteditable"}:
                del node.attrib[attr]
        for attr in ("href", "src"):
            if re.match(r"\s*(?:javascript|vbscript):", node.get(attr, ""), re.I):
                del node.attrib[attr]


def normalized_styles(document, mode):
    for node in document.xpath("//style | //link[@rel='stylesheet']"):
        node.getparent().remove(node)
    for node in document.iter():
        if not isinstance(node.tag, str):
            continue
        decls = declarations(node.get("style", ""))
        kept = []
        for decl in decls:
            if decl.lower_name.startswith("--") or decl.lower_name in {"background", "background-color", "color"}:
                continue
            if mode == "text" and decl.lower_name in {"font", "font-family", "font-feature-settings"}:
                continue
            # Original website layout should not constrain a reflowable EPUB page.
            if decl.lower_name in {"position", "width", "height", "min-width", "overflow", "overflow-y"}:
                continue
            kept.append(tinycss2.serialize([decl]))
        if kept:
            node.set("style", "; ".join(kept))
        else:
            node.attrib.pop("style", None)


def xhtml_document(title, language="zh-CN"):
    root = etree.Element(f"{{{XHTML}}}html", nsmap={None: XHTML, "epub": EPUB})
    root.set("lang", language)
    root.set("{http://www.w3.org/XML/1998/namespace}lang", language)
    head = etree.SubElement(root, f"{{{XHTML}}}head")
    etree.SubElement(head, f"{{{XHTML}}}title").text = title
    etree.SubElement(head, f"{{{XHTML}}}link", rel="stylesheet", type="text/css", href="styles/book.css")
    return root, etree.SubElement(root, f"{{{XHTML}}}body")


def xml_bytes(root):
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True)


def namespaced_clone(node):
    result = copy.deepcopy(node)
    for item in result.iter():
        if not isinstance(item.tag, str):
            continue
        if not item.tag.startswith("{"):
            item.tag = f"{{{XHTML}}}{item.tag}"
        for attr in list(item.attrib):
            if ":" in attr and not attr.startswith("{"):
                # HTML parser preserves unknown HTML attributes literally; XML needs a namespace.
                if attr == "xml:lang":
                    item.set("{http://www.w3.org/XML/1998/namespace}lang", item.attrib[attr])
                del item.attrib[attr]
    return result


class Resources:
    def __init__(self, source, allow_remote):
        self.base = source.parent
        self.allow_remote = allow_remote
        self.entries = {}
        self.lookup = {}

    def image(self, url):
        if url in self.lookup:
            return self.lookup[url]
        data, mime = read_url(url, self.base, self.allow_remote)
        if mime == "image/svg+xml" or data.lstrip().startswith((b"<svg", b"<?xml")):
            svg = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
            if etree.QName(svg).localname != "svg":
                raise ValueError("图片内容不是 SVG")
            if svg.xpath('//*[local-name()="script"]') or svg.xpath('//@*[starts-with(local-name(), "on")]'):
                raise ValueError("SVG 包含脚本，需先清理后再导入")
            for value in svg.xpath('//@href | //@*[local-name()="href"]'):
                if value and not value.startswith("#"):
                    raise ValueError("SVG 引用了外部资源，请先将其转换为 PNG")
            mime, ext = "image/svg+xml", "svg"
        else:
            with Image.open(BytesIO(data)) as image:
                fmt = image.format
                image.verify()
            known = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "GIF": ("image/gif", "gif")}
            if fmt in known:
                mime, ext = known[fmt]
            else:
                with Image.open(BytesIO(data)) as image:
                    output = BytesIO()
                    image.convert("RGBA").save(output, format="PNG")
                    data = output.getvalue()
                mime, ext = "image/png", "png"
        href = f"images/{hashlib.sha256(data).hexdigest()[:24]}.{ext}"
        self.entries[href] = (data, mime)
        self.lookup[url] = href
        return href

    def rewrite_images(self, node):
        for image in node.xpath('.//img'):
            url = image.get("src") or image.get("data-src")
            if not url:
                raise ValueError("发现缺少 src 的图片")
            image.set("src", self.image(url))
            image.attrib.pop("data-src", None)
            image.attrib.pop("srcset", None)
            image.attrib.pop("sizes", None)
            image.set("alt", image.get("alt", "插图"))


def validate_epub(path):
    """Check ZIP layout, XML, manifest, spine and every internal XHTML reference."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with ZipFile(path) as archive:
        items = archive.infolist()
        if not items or items[0].filename != "mimetype" or items[0].compress_type != ZIP_STORED:
            raise ValueError("EPUB mimetype 必须是首个且不压缩的文件")
        if archive.read("mimetype") != b"application/epub+zip":
            raise ValueError("EPUB mimetype 内容无效")
        if archive.testzip():
            raise ValueError("EPUB ZIP 校验失败")
        names = set(archive.namelist())
        container = etree.fromstring(archive.read("META-INF/container.xml"), parser)
        rootfile = container.xpath('//*[local-name()="rootfile"]/@full-path')[0]
        package = etree.fromstring(archive.read(rootfile), parser)
        prefix = posixpath.dirname(rootfile)
        manifest = {}
        docs = {}
        for item in package.findall(f"{{{OPF}}}manifest/{{{OPF}}}item"):
            name = posixpath.normpath(posixpath.join(prefix, unquote(item.get("href"))))
            if name not in names:
                raise ValueError(f"EPUB 资源缺失：{name}")
            manifest[item.get("id")] = name
            if item.get("media-type") in {"application/xhtml+xml", "application/x-dtbncx+xml"}:
                docs[name] = etree.fromstring(archive.read(name), parser)
        for item in package.findall(f"{{{OPF}}}spine/{{{OPF}}}itemref"):
            if item.get("idref") not in manifest:
                raise ValueError("EPUB spine 引用了不存在的资源")
        for name, doc in docs.items():
            if name.endswith(".ncx"):
                urls = doc.xpath('//@src')
            else:
                urls = doc.xpath('//@src | //@href')
            for url in urls:
                parts = urlsplit(url)
                if parts.scheme or url.startswith("//"):
                    continue
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parts.path))) if parts.path else name
                if target not in names:
                    raise ValueError(f"EPUB 内部链接无效：{name} -> {url}")
                if parts.fragment and target in docs:
                    if not docs[target].xpath('//*[@id=$id]', id=unquote(parts.fragment)):
                        raise ValueError(f"EPUB 锚点无效：{name} -> {url}")
    return {"xml_documents": len(docs), "manifest_items": len(manifest), "zip_crc": "passed"}


def convert(source, output=None, *, mode="text", reference_fonts=(), title=None, author=None,
            language=None, restored_html=None, allow_remote=False, force=False, log=print):
    source = Path(source).resolve()
    output = Path(output).resolve() if output else source.with_suffix(".epub")
    if mode not in {"text", "visual"}:
        raise ValueError("mode 只能是 text 或 visual")
    if not source.is_file():
        raise FileNotFoundError(f"输入文件不存在：{source}")
    if source == output or (restored_html and Path(restored_html).resolve() == source):
        raise ValueError("输出路径不能覆盖源 HTML")
    if output.exists() and not force:
        raise FileExistsError(f"输出已存在：{output}。换一个路径或使用 --force。")
    if restored_html and Path(restored_html).exists() and not force:
        raise FileExistsError(f"还原 HTML 已存在：{restored_html}")
    output.parent.mkdir(parents=True, exist_ok=True)
    report_path = output.with_suffix(".report.json")
    report = {"source": str(source), "output": str(output), "mode": mode, "status": "working"}
    log("读取 HTML（支持大体积内嵌图片和字体）…")
    raw = source.read_bytes()
    default_encoding = None
    if not re.search(br'(?:charset\s*=|encoding\s*=)', raw[:4096], re.I):
        try:
            raw.decode("utf-8-sig")
            default_encoding = "utf-8"
        except UnicodeDecodeError:
            pass
    parser = html.HTMLParser(huge_tree=True, no_network=True, recover=True, encoding=default_encoding)
    document = html.document_fromstring(raw, parser=parser)
    # libxml2 otherwise silently truncates very large base64 attributes at its safety limit.
    fatal = [entry.message for entry in parser.error_log if entry.level_name == "FATAL"]
    if fatal:
        raise ValueError("HTML 解析未完成：" + "; ".join(fatal[:3]))
    body = document.find("body")
    if body is None:
        raise ValueError("HTML 缺少正文")
    clean(document)
    title = title or document.findtext("head/title") or source.stem
    language = language or document.get("lang") or "zh-CN"
    if not author:
        info = classes(document, "meta-info")
        match = re.search(r"(?:作者|Author)\s*[:：]\s*([^\r\n]+)", info[0].text_content() if info else "", re.I)
        author = match.group(1).strip() if match else ""
    try:
        context = FontContext(document, source.parent, allow_remote)
        if mode == "text":
            font_report, changed = decode_document_safe(document, context, reference_fonts, log)
        else:
            font_report, changed = [], 0
            # Freeze the resolved webfont family inline before removing original CSS.
            for node, stack in context.node_family.items():
                if any(name in context.faces for name in stack):
                    safe = ", ".join('"' + name.replace('"', '') + '"' for name in stack)
                    node.set("style", node.get("style", "") + f";font-family:{safe};font-feature-settings:'locl' 0")
            report["copy_search_correct"] = not bool(context.faces)
        normalized_styles(document, mode)
        report.update(fonts=font_report, replaced_characters=changed)
        if mode == "text":
            report["copy_search_correct"] = True
        if restored_html:
            preview = copy.deepcopy(document)
            head = preview.find("head")
            for meta in head.xpath('./meta[@charset] | ./meta[translate(@http-equiv,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="content-type"]'):
                head.remove(meta)
            head.insert(0, etree.Element("meta", {"http-equiv": "Content-Type", "content": "text/html; charset=UTF-8"}))
            head.insert(1, etree.Element("meta", charset="utf-8"))
            etree.SubElement(head, "style").text = CSS
            if mode == "visual" and context.faces:
                raise ValueError("--restored-html 仅适用于真实文字模式")
            restored_path = Path(restored_html)
            restored_path.parent.mkdir(parents=True, exist_ok=True)
            restored_path.write_bytes(html.tostring(preview, encoding="UTF-8", doctype="<!DOCTYPE html>",
                                                    include_meta_content_type=True))
        chapters = classes(body, "chapter")
        if not chapters:
            # Standard HTML: keep the whole body; this avoids losing nested heading content.
            chapters = [body]
        resources = Resources(source, allow_remote)
        files = {"styles/book.css": (CSS.encode(), "text/css")}
        if mode == "visual":
            font_css = []
            for i, (name, face) in enumerate(context.faces.items()):
                font = TTFont(BytesIO(face.data))
                font.flavor = None
                unique = f"H2E{face.sha256[:16]}"
                for nid, value in [(1, unique), (2, "Regular"), (4, unique), (6, unique)]:
                    for platform, encoding, lang in [(3, 1, 0x409), (1, 0, 0)]:
                        font["name"].setName(value, nid, platform, encoding, lang)
                buf = BytesIO()
                font.save(buf)
                ext = "otf" if "CFF " in font or "CFF2" in font else "ttf"
                font.close()
                href = f"fonts/font-{i+1:03d}.{ext}"
                files[href] = (buf.getvalue(), f"font/{ext}")
                font_css.append(f'@font-face{{font-family:"{name}";src:url("../{href}");}}')
            files["styles/book.css"] = ((CSS + "\n" + "\n".join(font_css)).encode(), "text/css")
        toc = []
        id_targets = {}
        xdocs = []
        cover_href = None
        cover_nodes = classes(body, "cover-img") if chapters != [body] else []
        if cover_nodes:
            cover_node = copy.deepcopy(cover_nodes[0])
            resources.rewrite_images(cover_node)
            imgs = cover_node.xpath('.//img/@src')
            cover_href = imgs[0] if imgs else None
            root, page = xhtml_document("封面", language)
            page.append(namespaced_clone(cover_node))
            files["cover.xhtml"] = (xml_bytes(root), "application/xhtml+xml")
        root, page = xhtml_document(title, language)
        etree.SubElement(page, f"{{{XHTML}}}h1", {"class": "book-title"}).text = title
        if author:
            etree.SubElement(page, f"{{{XHTML}}}p").text = "作者：" + author
        files["title.xhtml"] = (xml_bytes(root), "application/xhtml+xml")
        log(f"生成 {len(chapters)} 个章节、提取插图…")
        for i, chapter in enumerate(chapters):
            href = f"chapter-{i+1:04d}.xhtml"
            headings = chapter.xpath('.//h1 | .//h2 | .//h3')
            label = headings[0].text_content().strip() if headings else title if len(chapters) == 1 else f"第 {i+1} 章"
            copied = copy.deepcopy(chapter)
            if copied.tag == "body":
                copied.tag = "div"
                for nav in copied.xpath('.//nav'):
                    nav.drop_tree()
            resources.rewrite_images(copied)
            for node in copied.iter():
                if isinstance(node.tag, str) and node.get("id"):
                    old = node.get("id")
                    if old in id_targets:
                        raise ValueError(f"HTML 包含重复 id，无法可靠重写目录：{old}")
                    id_targets[old] = href
            root, page = xhtml_document(label, language)
            page.append(namespaced_clone(copied))
            toc.append((label, href))
            xdocs.append((href, root))
        for href, root in xdocs:
            for link in root.xpath('//*[@href]'):
                url = link.get("href")
                parts = urlsplit(url)
                # Same-document links can include the original HTML file name.
                if not parts.scheme and parts.fragment and (not parts.path or unquote(parts.path) == source.name):
                    target = id_targets.get(unquote(parts.fragment))
                    if target:
                        link.set("href", target + "#" + parts.fragment)
                    elif link.tag == f"{{{XHTML}}}a":
                        link.attrib.pop("href", None)
                elif not parts.scheme and parts.path and link.tag == f"{{{XHTML}}}a":
                    # Local attachments cannot be read inside a portable book.
                    raise ValueError(f"正文含未打包的本地附件链接：{url}")
            files[href] = (xml_bytes(root), "application/xhtml+xml")
        files.update(resources.entries)
        root, page = xhtml_document("目录", language)
        nav = etree.SubElement(page, f"{{{XHTML}}}nav", {f"{{{EPUB}}}type": "toc", "id": "toc"})
        etree.SubElement(nav, f"{{{XHTML}}}h1").text = "目录"
        ol = etree.SubElement(nav, f"{{{XHTML}}}ol")
        for label, href in toc:
            etree.SubElement(etree.SubElement(ol, f"{{{XHTML}}}li"), f"{{{XHTML}}}a", href=href).text = label
        files["nav.xhtml"] = (xml_bytes(root), "application/xhtml+xml")
        identifier = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, hashlib.sha256(raw).hexdigest()))
        ncx = etree.Element(f"{{{NCX}}}ncx", nsmap={None: NCX}, version="2005-1")
        head = etree.SubElement(ncx, f"{{{NCX}}}head")
        for name, content in [("dtb:uid", identifier), ("dtb:depth", "1"), ("dtb:totalPageCount", "0"), ("dtb:maxPageNumber", "0")]:
            etree.SubElement(head, f"{{{NCX}}}meta", name=name, content=content)
        etree.SubElement(etree.SubElement(ncx, f"{{{NCX}}}docTitle"), f"{{{NCX}}}text").text = title
        navmap = etree.SubElement(ncx, f"{{{NCX}}}navMap")
        for i, (label, href) in enumerate(toc, 1):
            point = etree.SubElement(navmap, f"{{{NCX}}}navPoint", id=f"nav-{i}", playOrder=str(i))
            etree.SubElement(etree.SubElement(point, f"{{{NCX}}}navLabel"), f"{{{NCX}}}text").text = label
            etree.SubElement(point, f"{{{NCX}}}content", src=href)
        files["toc.ncx"] = (xml_bytes(ncx), "application/x-dtbncx+xml")
        package = etree.Element(f"{{{OPF}}}package", nsmap={None: OPF, "dc": DC},
                                version="3.0", attrib={"unique-identifier": "book-id"})
        metadata = etree.SubElement(package, f"{{{OPF}}}metadata")
        etree.SubElement(metadata, f"{{{DC}}}identifier", id="book-id").text = identifier
        etree.SubElement(metadata, f"{{{DC}}}title").text = title
        etree.SubElement(metadata, f"{{{DC}}}language").text = language
        if author:
            etree.SubElement(metadata, f"{{{DC}}}creator").text = author
        etree.SubElement(metadata, f"{{{OPF}}}meta", property="dcterms:modified").text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = etree.SubElement(package, f"{{{OPF}}}manifest")
        ids = {}
        for i, (href, (_, mime)) in enumerate(files.items()):
            item_id = f"resource-{i}"
            ids[href] = item_id
            item = etree.SubElement(manifest, f"{{{OPF}}}item", id=item_id, href=href, attrib={"media-type": mime})
            if href == "nav.xhtml":
                item.set("properties", "nav")
            if href == cover_href:
                item.set("properties", "cover-image")
                etree.SubElement(metadata, f"{{{OPF}}}meta", name="cover", content=item_id)
        spine = etree.SubElement(package, f"{{{OPF}}}spine", toc=ids["toc.ncx"])
        order = (["cover.xhtml"] if "cover.xhtml" in files else []) + ["title.xhtml", "nav.xhtml"] + [href for _, href in toc]
        for href in order:
            etree.SubElement(spine, f"{{{OPF}}}itemref", idref=ids[href])
        files["package.opf"] = (xml_bytes(package), "application/oebps-package+xml")
        container = b'<?xml version="1.0" encoding="UTF-8"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
        temporary = output.parent / ("." + output.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
                archive.writestr("META-INF/container.xml", container)
                for href, (data, _) in files.items():
                    archive.writestr("EPUB/" + href, data)
            checks = validate_epub(temporary)
            if output.exists() and not force:
                raise FileExistsError(f"输出已在转换期间创建：{output}")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        report.update(status="success", title=title, author=author, chapter_count=len(chapters),
                      image_count=len(resources.entries), checks=checks, output_bytes=output.stat().st_size)
        log(f"完成：{output}")
        return report
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        if isinstance(exc, FontDecodeError):
            report["unresolved_fonts"] = exc.details
        raise
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def decode_document_safe(document, context, reference_fonts, log):
    from .fonts import decode_document
    return decode_document(document, context, Decoder(reference_fonts), log)
