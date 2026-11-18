from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urlsplit

import cssselect2
import tinycss2
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont

DATA = Path(__file__).parent / "data"


class FontDecodeError(ValueError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or []


def glyph_digest(glyphs, name, units=1000):
    """Hash actual outlines, never trust a remapped glyph's name or code point."""
    pen = DecomposingRecordingPen(glyphs)
    glyphs[name].draw(pen)
    record = []
    for operation, points in pen.value:
        normalized = []
        for point in points:
            if point is None:
                normalized.append(None)
            else:
                x, y = point
                normalized.append([round(x * 65536 * 1000 / units), round(y * 65536 * 1000 / units)])
        record.append([operation, normalized])
    return hashlib.sha256(json.dumps(record, separators=(",", ":")).encode("ascii")).hexdigest()


def families(value):
    result = []
    for part in re.split(r",(?=(?:[^'\"]|'[^']*'|\"[^\"]*\")*$)", value):
        name = part.strip().strip("'\"").strip()
        if name:
            result.append(name.casefold())
    return result


def declarations(content):
    return [d for d in tinycss2.parse_declaration_list(content, skip_comments=True, skip_whitespace=True)
            if d.type == "declaration"]


def family_declaration(decls):
    winner = None
    priority = (-1, -1)
    for i, decl in enumerate(decls):
        if decl.lower_name != "font-family":
            continue
        score = (int(decl.important), i)
        if score >= priority:
            winner = (families(tinycss2.serialize(decl.value)), decl.important)
            priority = score
    return winner


def read_url(url, base, allow_remote=False):
    if url.startswith("data:"):
        header, sep, payload = url.partition(",")
        if not sep:
            raise ValueError("无效的 data URL")
        mime = header[5:].split(";", 1)[0].lower()
        raw = unquote_to_bytes(payload)
        return (base64.b64decode(re.sub(rb"\s+", b"", raw), validate=True) if ";base64" in header.lower() else raw), mime
    parsed = urlsplit(url)
    if parsed.scheme in {"http", "https"} or url.startswith("//"):
        if not allow_remote:
            raise ValueError(f"资源尚未保存在本地：{url[:180]}。请先保存完整网页，或使用 --allow-remote。")
        from urllib.request import Request, urlopen
        with urlopen(Request("https:" + url if url.startswith("//") else url,
                             headers={"User-Agent": "HTML-to-EPUB/1.0"}), timeout=25) as response:
            data = response.read(64 * 1024 * 1024 + 1)
            if len(data) > 64 * 1024 * 1024:
                raise ValueError("单个远程资源超过 64 MB")
            return data, response.headers.get_content_type()
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        if re.match(r"^/[a-zA-Z]:", path):
            path = path[1:]
        target = Path(path)
    elif parsed.scheme:
        raise ValueError(f"不支持的资源协议：{parsed.scheme}")
    else:
        target = base / unquote(parsed.path)
    return target.read_bytes(), ""


@dataclass
class Face:
    family: str
    data: bytes

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()


class FontContext:
    def __init__(self, document, base: Path, allow_remote=False):
        self.faces = {}
        self.matcher = cssselect2.Matcher()
        self.warnings = []
        self.shorthands = []
        self._order = 0
        for node in document.xpath("//style | //link[@rel='stylesheet']"):
            if node.tag == "style":
                css = node.text or ""
                css_base = base
            else:
                url = node.get("href", "")
                data, _ = read_url(url, base, allow_remote)
                css = data.decode("utf-8-sig")
                css_base = (base / unquote(urlsplit(url).path)).parent if not urlsplit(url).scheme else base
            self._read_rules(tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True),
                             css_base, allow_remote)
        if self.faces:
            inline_shorthand = any(d.lower_name == "font" for node in document.iter()
                                   if isinstance(node.tag, str)
                                   for d in declarations(node.get("style", "")))
            if self.shorthands or inline_shorthand:
                raise FontDecodeError("页面同时使用了内嵌字体和 font: 简写，暂不能可靠解析字体作用范围。请先展开为 font-family。")
        self.node_family = {}
        root = cssselect2.ElementWrapper.from_html_root(document)
        for wrapper in root.iter_subtree():
            node = wrapper.etree_element
            parent = wrapper.parent
            inherited = self.node_family.get(parent.etree_element, []) if parent else []
            chosen = inherited
            best = (-1, -1, -1, -1, -1, -1)
            for specificity, order, pseudo, payload in self.matcher.match(wrapper):
                if pseudo:
                    continue
                font_list, important = payload
                score = (int(important), 0, *specificity, order)
                if score >= best:
                    best, chosen = score, font_list
            inline = family_declaration(declarations(node.get("style", "")))
            if inline:
                font_list, important = inline
                score = (int(important), 1, 0, 0, 0, 0)
                if score >= best:
                    chosen = font_list
            if chosen == ["inherit"] or chosen == ["unset"]:
                chosen = inherited
            if chosen == ["initial"]:
                chosen = []
            self.node_family[node] = chosen

    def _read_rules(self, rules, base, allow_remote):
        for rule in rules:
            if rule.type == "at-rule" and rule.lower_at_keyword == "font-face" and rule.content:
                decls = {d.lower_name: tinycss2.serialize(d.value).strip() for d in declarations(rule.content)}
                if "font-family" not in decls or "src" not in decls:
                    continue
                family = families(decls["font-family"])[0]
                urls = []
                for token in tinycss2.parse_component_value_list(decls["src"]):
                    if token.type == "url":
                        urls.append(token.value)
                    elif token.type == "function" and token.lower_name == "url":
                        urls.append(tinycss2.serialize(token.arguments).strip().strip("'\""))
                last_error = None
                for url in urls:
                    try:
                        data, _ = read_url(url, base, allow_remote)
                        if family in self.faces and self.faces[family].data != data:
                            raise FontDecodeError(f"同一字体族含多个字重或样式，暂不自动还原：{family}")
                        self.faces[family] = Face(family, data)
                        break
                    except FontDecodeError:
                        raise
                    except Exception as exc:
                        last_error = exc
                else:
                    if urls:
                        raise ValueError(f"无法读取字体 {family}：{last_error}")
            elif rule.type == "qualified-rule":
                parsed = declarations(rule.content)
                self.shorthands.extend(d for d in parsed if d.lower_name == "font")
                decl = family_declaration(parsed)
                if not decl:
                    continue
                selector = tinycss2.serialize(rule.prelude)
                try:
                    compiled = cssselect2.compile_selector_list(selector)
                except cssselect2.SelectorError as exc:
                    raise FontDecodeError(f"不能可靠解析字体选择器 {selector}：{exc}") from exc
                for item in compiled:
                    self.matcher.add_selector(item, decl)
            elif rule.type == "at-rule" and rule.content and rule.lower_at_keyword in {"media", "supports", "layer"}:
                media = tinycss2.serialize(rule.prelude).strip().lower()
                if rule.lower_at_keyword == "media" and media not in {"all", "screen", ""}:
                    # This converter has no viewport. Avoid guessing conditional font cascades.
                    if "font-family" in tinycss2.serialize(rule.content):
                        raise FontDecodeError(f"存在依赖显示条件的字体规则，需先展开 CSS：{media}")
                    continue
                self._read_rules(tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True),
                                 base, allow_remote)

    def used_runs(self, document):
        """Tail text belongs to the parent, not to the preceding inline element."""
        for node in document.iter():
            if not isinstance(node.tag, str) or node.tag in {"style", "script", "title"}:
                continue
            if node.text:
                yield node, "text", self.node_family.get(node, [])
            if node.tail:
                yield node, "tail", self.node_family.get(node.getparent(), [])


class Decoder:
    def __init__(self, reference_fonts=()):
        self.profiles = self._load("profiles.json.gz")
        self.glyphs = self._load("glyphs.json.gz")
        for path in reference_fonts:
            self.add_reference(Path(path))

    @staticmethod
    def _load(name):
        path = DATA / name
        if not path.exists():
            return {}
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)

    def add_reference(self, path):
        font = TTFont(path)
        try:
            glyphs = font.getGlyphSet()
            for cp, name in font.getBestCmap().items():
                key = glyph_digest(glyphs, name, font["head"].unitsPerEm)
                values = set(self.glyphs.get(key, ""))
                values.add(chr(cp))
                self.glyphs[key] = "".join(sorted(values))
        finally:
            font.close()

    def mapping(self, face: Face, used):
        profile = self.profiles.get(face.sha256, {})
        mapping = dict(profile.get("mapping", {}))
        font = TTFont(BytesIO(face.data))
        try:
            cmap = font.getBestCmap() or {}
            glyphs = font.getGlyphSet()
            missing = []
            for char in sorted(used):
                if char in mapping:
                    continue
                name = cmap.get(ord(char))
                if name is None:
                    # Browser falls back to the next font when the font has no such glyph.
                    continue
                key = glyph_digest(glyphs, name, font["head"].unitsPerEm)
                candidates = set(self.glyphs.get(key, ""))
                if char in candidates:
                    mapping[char] = char
                    continue
                normalized = {unicodedata.normalize("NFKC", c) for c in candidates}
                if len(normalized) == 1 and len(next(iter(normalized))) == 1:
                    mapping[char] = next(iter(normalized))
                else:
                    missing.append(char)
            if missing:
                raise FontDecodeError(
                    f"字体 {face.family} 有 {len(missing)} 个字形尚未验证，已停止文字转换。",
                    [{"font": face.family, "sha256": face.sha256, "unresolved": "".join(missing)}],
                )
            return mapping, set(cmap)
        finally:
            font.close()


def decode_document(document, context, decoder, log=lambda _: None):
    runs = list(context.used_runs(document))
    used = {name: set() for name in context.faces}
    # Resolve the font stack independently for each character, including glyph fallback.
    cmaps = {}
    for name, face in context.faces.items():
        font = TTFont(BytesIO(face.data))
        cmaps[name] = set(font.getBestCmap() or {})
        font.close()
    def selected(stack, char):
        for name in stack:
            if name in cmaps:
                if ord(char) in cmaps[name]:
                    return name
                continue
            # A local/system font before a webfont makes the actual glyph platform-dependent.
            if any(candidate in cmaps for candidate in stack[stack.index(name)+1:]):
                raise FontDecodeError(f"系统字体排在混淆字体前，无法确定页面字形：{', '.join(stack)}")
            break
        return None
    for node, attr, stack in runs:
        for char in getattr(node, attr):
            name = selected(stack, char)
            if name:
                used[name].add(char)
    mappings = {}
    report = []
    errors = []
    for name, chars in used.items():
        if not chars:
            continue
        log(f"校验并还原字体：{name}（{len(chars)} 个实际用字）")
        try:
            mapping, _ = decoder.mapping(context.faces[name], chars)
            mappings[name] = mapping
            report.append({"family": name, "sha256": context.faces[name].sha256,
                           "used_glyphs": len(chars),
                           "changed_glyphs": sum(mapping.get(c, c) != c for c in chars)})
        except FontDecodeError as exc:
            errors.extend(exc.details)
    if errors:
        raise FontDecodeError("部分字体无法完整验证，未生成 EPUB。详见报告中的 unresolved。", errors)
    changed = 0
    for node, attr, stack in runs:
        original = getattr(node, attr)
        result = []
        for char in original:
            name = selected(stack, char)
            value = mappings.get(name, {}).get(char, char)
            changed += value != char
            result.append(value)
        setattr(node, attr, "".join(result))
    return report, changed
