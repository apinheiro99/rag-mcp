"""Extração de conteúdo por formato, preservando referências de origem
(página de PDF, capítulo de EPUB, seção de Markdown/HTML, estilo de DOCX).

Saída: ExtractedDoc(segments=[Segment(text, loc)], meta={...})
Cada segment carrega `loc` (page/chapter/heading) que acompanha os chunks
até o resultado de busca — é assim que o cliente MCP sabe de onde veio
cada trecho.
"""
from __future__ import annotations

import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

log = logging.getLogger("rag.extract")

TEXT_EXTENSIONS = {
    ".txt", ".text", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".sh", ".bash", ".sql", ".tex",
}
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
HTML_EXTENSIONS = {".html", ".htm", ".xhtml", ".xml"}
JSON_EXTENSIONS = {".json"}
JSONL_EXTENSIONS = {".jsonl", ".ndjson"}
JSON_EXTENSIONS = {".json"}
JSONL_EXTENSIONS = {".jsonl", ".ndjson"}


class ExtractionError(Exception):
    """Arquivo ilegível/corrompido/sem texto extraível."""


@dataclass
class Segment:
    text: str
    loc: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedDoc:
    segments: list[Segment]
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"segments": [{"text": s.text, "loc": s.loc} for s in self.segments],
                "meta": self.meta}

    @classmethod
    def from_json(cls, data: dict) -> "ExtractedDoc":
        segments: list[Segment] = []
        for item in data["segments"]:
            text = normalize_ws(item["text"])
            if text:
                segments.append(Segment(text, item.get("loc", {})))
        return cls(segments=segments, meta=data.get("meta", {}))

    def full_text(self) -> str:
        return "\n\n".join(s.text for s in self.segments)


def normalize_ws(text: str) -> str:
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- HTML
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "table", "br",
               "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol"}
_SKIP_TAGS = {"script", "style", "noscript", "svg"}
_HEADING_TAGS = {"h1", "h2", "h3"}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.segments: list[Segment] = []
        self._skip_depth = 0
        self._in_title = False
        self._heading_buf: list[str] | None = None
        self._current_heading: str | None = None
        self._buf: list[str] = []

    def _flush(self) -> None:
        text = normalize_ws("".join(self._buf))
        if text:
            loc = {"heading": self._current_heading} if self._current_heading else {}
            self.segments.append(Segment(text, loc))
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in _HEADING_TAGS:
            self._flush()
            self._heading_buf = []
        elif tag in _BLOCK_TAGS:
            self._buf.append("\n\n" if tag != "br" else "\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _HEADING_TAGS and self._heading_buf is not None:
            heading = normalize_ws("".join(self._heading_buf))
            self._heading_buf = None
            if heading:
                self._current_heading = heading
        elif tag in _BLOCK_TAGS:
            self._buf.append("\n\n" if tag != "br" else "\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        if self._heading_buf is not None:
            self._heading_buf.append(data)
        self._buf.append(data)

    def finish(self) -> list[Segment]:
        self._flush()
        return self.segments


def html_to_segments(html: str, base_loc: dict | None = None) -> tuple[list[Segment], str]:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        log.warning("HTML malformado; extração parcial")
    segments = parser.finish()
    if base_loc:
        for s in segments:
            s.loc = {**base_loc, **s.loc}
    return segments, normalize_ws(parser.title)


# ---------------------------------------------------------------- Markdown
def markdown_to_segments(text: str) -> list[Segment]:
    lines = text.split("\n")
    segments: list[Segment] = []
    heading_stack: list[tuple[int, str]] = []
    buf: list[str] = []
    in_code = False

    def flush():
        body = normalize_ws("\n".join(buf))
        if body:
            heading = " > ".join(h for _, h in heading_stack) or None
            loc = {"heading": heading} if heading else {}
            segments.append(Segment(body, loc))
        buf.clear()

    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip().rstrip("#").strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            buf.append(title)  # título entra no texto do chunk
        else:
            buf.append(line)
    flush()
    if not segments:
        body = normalize_ws(text)
        if body:
            segments.append(Segment(body))
    return segments


# ---------------------------------------------------------------- PDF
def extract_pdf(path: Path, max_pages: int | None = None) -> ExtractedDoc:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:
                raise ExtractionError(f"PDF protegido por senha: {e}") from e
        total = len(reader.pages)
        segments: list[Segment] = []
        limit = min(total, max_pages) if max_pages else total
        empty_pages = 0
        for i in range(limit):
            try:
                text = reader.pages[i].extract_text() or ""
            except Exception as e:
                log.warning("página %d de %s ilegível: %s", i + 1, path.name, e)
                text = ""
            text = normalize_ws(text)
            if text:
                segments.append(Segment(text, {"page": i + 1}))
            else:
                empty_pages += 1
        meta: dict[str, Any] = {"kind": "pdf", "pages": total}
        try:
            info = reader.metadata or {}
            if info.title:
                meta["title"] = str(info.title)
            if info.author:
                meta["author"] = str(info.author)
        except Exception:
            pass
        if empty_pages:
            meta["empty_pages"] = empty_pages
        if not segments:
            raise ExtractionError(
                "PDF sem texto extraível (provavelmente escaneado; requer OCR, não suportado)")
        return ExtractedDoc(segments, meta)
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"falha ao ler PDF: {e}") from e


# ---------------------------------------------------------------- EPUB
def _epub_chapter_titles(opf_dir: str, manifest: dict, zf: zipfile.ZipFile) -> dict[str, str]:
    """Mapa href→título a partir do NCX (EPUB2) ou nav (EPUB3)."""
    titles: dict[str, str] = {}

    def norm(href: str) -> str:
        return (opf_dir + "/" + href if opf_dir else href).lstrip("./").split("#")[0]

    # EPUB3 nav
    for item in manifest.values():
        if "nav" in (item.get("properties") or ""):
            try:
                html = zf.read(item["href"]).decode("utf-8", errors="replace")
                root = ET.fromstring(re.sub(r'xmlns="[^"]+"', "", html, count=1))
                for a in root.iter("a"):
                    href = a.get("href")
                    text = "".join(a.itertext()).strip()
                    if href and text:
                        titles.setdefault(norm(href), text)
            except Exception:
                pass
    # EPUB2 NCX
    ncx = next((i for i in manifest.values()
                if i.get("media-type") == "application/x-dtbncx+xml"), None)
    if ncx:
        try:
            root = ET.fromstring(zf.read(ncx["href"]))
            ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
            for point in root.iter():
                if point.tag.endswith("navPoint"):
                    label = point.find(".//n:text", ns)
                    content = point.find(".//n:content", ns)
                    if label is not None and content is not None and content.get("src"):
                        titles.setdefault(norm(content.get("src")), (label.text or "").strip())
        except Exception:
            pass
    return titles


def extract_epub(path: Path, max_chapters: int | None = None) -> ExtractedDoc:
    try:
        with zipfile.ZipFile(path) as zf:
            container = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
            root = ET.fromstring(container)
            opf_path = None
            for rf in root.iter():
                if rf.tag.endswith("rootfile"):
                    opf_path = rf.get("full-path")
                    break
            if not opf_path:
                raise ExtractionError("EPUB sem rootfile OPF")
            opf_dir = str(Path(opf_path).parent).strip(".")
            opf_dir = opf_dir.strip("/")
            opf = ET.fromstring(zf.read(opf_path))
            ns = {"o": "http://www.idpf.org/2007/opf"}
            manifest = {}
            for item in opf.findall(".//o:manifest/o:item", ns):
                manifest[item.get("id")] = {
                    "href": item.get("href"),
                    "media-type": item.get("media-type", ""),
                    "properties": item.get("properties", ""),
                }
            spine = [ref.get("idref") for ref in opf.findall(".//o:spine/o:itemref", ns)]
            titles = _epub_chapter_titles(opf_dir, manifest, zf)
            meta: dict[str, Any] = {"kind": "epub"}
            for el in opf.iter():
                if el.tag.endswith("}title") and el.text:
                    meta["title"] = el.text.strip()
                if el.tag.endswith("}creator") and el.text:
                    meta["author"] = el.text.strip()
            segments: list[Segment] = []
            chapter_idx = 0
            for idref in spine:
                item = manifest.get(idref)
                if not item or "html" not in item["media-type"]:
                    continue
                href = (opf_dir + "/" + item["href"]).lstrip("/") if opf_dir else item["href"]
                try:
                    html = zf.read(href).decode("utf-8", errors="replace")
                except KeyError:
                    continue
                chapter_idx += 1
                if max_chapters and chapter_idx > max_chapters:
                    break
                title = titles.get(href.split("#")[0]) or f"Capítulo {chapter_idx}"
                chap_segments, _ = html_to_segments(html, {"chapter": title,
                                                           "chapter_index": chapter_idx})
                segments.extend(chap_segments)
            meta["chapters"] = chapter_idx if not max_chapters else min(chapter_idx, max_chapters)
            if not segments:
                raise ExtractionError("EPUB sem conteúdo textual extraível")
            return ExtractedDoc(segments, meta)
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"falha ao ler EPUB: {e}") from e


# ---------------------------------------------------------------- DOCX
def extract_docx(path: Path) -> ExtractedDoc:
    try:
        import docx
        d = docx.Document(str(path))
        segments: list[Segment] = []
        heading: str | None = None
        buf: list[str] = []

        def flush():
            text = normalize_ws("\n".join(buf))
            if text:
                segments.append(Segment(text, {"heading": heading} if heading else {}))
            buf.clear()

        for p in d.paragraphs:
            style = (p.style.name or "").lower() if p.style else ""
            if style.startswith("heading") or style.startswith("título"):
                flush()
                heading = p.text.strip() or heading
                if p.text.strip():
                    buf.append(p.text.strip())
            elif p.text.strip():
                buf.append(p.text)
        flush()
        for t in d.tables:
            rows = [" | ".join(c.text.strip() for c in row.cells) for row in t.rows]
            text = normalize_ws("\n".join(rows))
            if text:
                segments.append(Segment(text, {"table": True}))
        if not segments:
            raise ExtractionError("DOCX sem texto extraível")
        return ExtractedDoc(segments, {"kind": "docx"})
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"falha ao ler DOCX: {e}") from e


# ---------------------------------------------------------------- structured JSON / specialized detectors
def _json_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def _json_record_text(value: Any, prefix: str = "") -> str:
    lines: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                walk(child, child_path)
            return
        if isinstance(node, list):
            if all(not isinstance(item, (dict, list)) for item in node):
                rendered = ", ".join(_json_scalar(item) for item in node)
                if path:
                    lines.append(f"{path}: {rendered}")
            else:
                for idx, child in enumerate(node, start=1):
                    child_path = f"{path}[{idx}]" if path else f"[{idx}]"
                    walk(child, child_path)
            return
        if path:
            lines.append(f"{path}: {_json_scalar(node)}")
        else:
            lines.append(_json_scalar(node))

    walk(value, prefix)
    return normalize_ws("\n".join(lines))


def _generic_json_segments(data: Any) -> list[Segment]:
    if isinstance(data, list):
        segments: list[Segment] = []
        for idx, item in enumerate(data, start=1):
            text = _json_record_text(item)
            if text:
                segments.append(Segment(text, {"record": idx}))
        return segments
    text = _json_record_text(data)
    return [Segment(text, {"record": 1})] if text else []


def _chatgpt_conversations(data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(data, list) or not data:
        return None
    conversations = [
        item for item in data
        if isinstance(item, dict) and isinstance(item.get("mapping"), dict)
    ]
    if not conversations:
        return None
    return conversations


def _chatgpt_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    texts: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                candidate = part.get("text")
                if isinstance(candidate, str):
                    texts.append(candidate)
    text = content.get("text")
    if isinstance(text, str):
        texts.append(text)
    return normalize_ws("\n".join(texts))


def _chatgpt_active_nodes(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = conversation.get("mapping") or {}
    current = conversation.get("current_node")
    if isinstance(current, str) and current in mapping:
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        node_id: str | None = current
        while node_id and node_id not in seen:
            seen.add(node_id)
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                break
            chain.append(node)
            parent = node.get("parent")
            node_id = parent if isinstance(parent, str) else None
        chain.reverse()
        return chain
    return [node for node in mapping.values() if isinstance(node, dict)]


def _extract_chatgpt_payload(data: Any) -> ExtractedDoc | None:
    conversations = _chatgpt_conversations(data)
    if conversations is None:
        return None

    segments: list[Segment] = []
    for conv_index, conversation in enumerate(conversations, start=1):
        title = normalize_ws(str(conversation.get("title") or f"Conversation {conv_index}"))
        message_index = 0
        for node in _chatgpt_active_nodes(conversation):
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            metadata = message.get("metadata") or {}
            if isinstance(metadata, dict) and metadata.get("is_visually_hidden_from_conversation"):
                continue
            author = message.get("author") or {}
            role = author.get("role") if isinstance(author, dict) else None
            if role not in {"user", "assistant"}:
                continue
            body = _chatgpt_message_text(message)
            if not body:
                continue
            message_index += 1
            loc: dict[str, Any] = {
                "conversation": conv_index,
                "conversation_title": title,
                "message": message_index,
                "role": role,
            }
            create_time = message.get("create_time")
            if create_time is not None:
                loc["create_time"] = create_time
            segments.append(Segment(body, loc))

    if not segments:
        raise ExtractionError("ChatGPT export sem mensagens user/assistant extraíveis")
    return ExtractedDoc(
        segments,
        {
            "kind": "chatgpt-export",
            "parser": "chatgpt-export",
            "conversations": len(conversations),
        },
    )


def _json_from_chatgpt_html(html: str) -> Any | None:
    if "ChatGPT Data Export" not in html or "var jsonData" not in html:
        return None
    match = re.search(r"\bvar\s+jsonData\s*=\s*", html)
    if not match:
        return None
    candidate = html[match.end():].lstrip()
    try:
        data, _ = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        return None
    return data


def extract_json_document(path: Path, *, sample: bool = False) -> ExtractedDoc:
    text = read_text_file(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"JSON inválido: {exc}") from exc

    specialized = _extract_chatgpt_payload(data)
    if specialized is not None:
        if sample:
            specialized.segments = specialized.segments[:20]
        return specialized
    segments = _generic_json_segments(data)
    if sample:
        segments = segments[:20]
    if not segments:
        raise ExtractionError("JSON sem conteúdo textual extraível")
    return ExtractedDoc(segments, {"kind": "json", "parser": "generic-json"})


def extract_jsonl_document(path: Path, *, sample: bool = False) -> ExtractedDoc:
    text = read_text_file(path)
    if sample:
        text = text[:20000]
    segments: list[Segment] = []
    lines = text.splitlines()
    if sample:
        lines = lines[:100]
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"JSONL inválido na linha {line_number}: {exc}") from exc
        rendered = _json_record_text(data)
        if rendered:
            segments.append(Segment(rendered, {"record": len(segments) + 1, "line": line_number}))
    if not segments:
        raise ExtractionError("JSONL sem conteúdo textual extraível")
    return ExtractedDoc(segments, {"kind": "jsonl", "parser": "generic-jsonl"})


def _try_chatgpt_html(path: Path, filename: str, sample: bool) -> ExtractedDoc | None:
    if Path(filename).suffix.lower() not in HTML_EXTENSIONS:
        return None
    payload = _json_from_chatgpt_html(read_text_file(path))
    if payload is None:
        return None
    doc = _extract_chatgpt_payload(payload)
    if doc is not None and sample:
        doc.segments = doc.segments[:20]
    return doc


def _try_json(path: Path, filename: str, sample: bool) -> ExtractedDoc | None:
    if Path(filename).suffix.lower() not in JSON_EXTENSIONS:
        return None
    return extract_json_document(path, sample=sample)


def _try_jsonl(path: Path, filename: str, sample: bool) -> ExtractedDoc | None:
    if Path(filename).suffix.lower() not in JSONL_EXTENSIONS:
        return None
    return extract_jsonl_document(path, sample=sample)


_SPECIALIZED_EXTRACTORS = (
    _try_chatgpt_html,
    _try_json,
    _try_jsonl,
)


def _extract_specialized(path: Path, filename: str, *, sample: bool) -> ExtractedDoc | None:
    # Ordered Chain of Responsibility: strong/specialized detectors first,
    # generic extension fallbacks remain in extract().
    for handler in _SPECIALIZED_EXTRACTORS:
        doc = handler(path, filename, sample)
        if doc is not None:
            return doc
    return None


# ---------------------------------------------------------------- dispatcher
def extract(path: Path, filename: str, *, sample: bool = False,
            strip_whitespace: bool = True) -> ExtractedDoc:
    ext = Path(filename).suffix.lower()
    doc = _extract_specialized(path, filename, sample=sample)
    if doc is not None:
        pass
    elif ext == ".pdf":
        doc = extract_pdf(path, max_pages=5 if sample else None)
    elif ext == ".epub":
        doc = extract_epub(path, max_chapters=3 if sample else None)
    elif ext in MARKDOWN_EXTENSIONS:
        text = read_text_file(path)
        if sample:
            text = text[:20000]
        doc = ExtractedDoc(markdown_to_segments(text), {"kind": "markdown"})
    elif ext in HTML_EXTENSIONS:
        html = read_text_file(path)
        if sample:
            html = html[:60000]
        segments, title = html_to_segments(html)
        doc = ExtractedDoc(segments, {"kind": "html", **({"title": title} if title else {})})
    elif ext == ".docx":
        doc = extract_docx(path)
    elif ext in TEXT_EXTENSIONS or ext == "":
        text = read_text_file(path)
        if sample:
            text = text[:20000]
        text = normalize_ws(text) if strip_whitespace else text
        if not text.strip():
            raise ExtractionError("arquivo vazio")
        doc = ExtractedDoc([Segment(text)], {"kind": "text"})
    else:
        raise ExtractionError(f"formato não suportado: {ext}")
    if strip_whitespace:
        for s in doc.segments:
            s.text = normalize_ws(s.text)
        doc.segments = [s for s in doc.segments if s.text]
    if not doc.segments:
        raise ExtractionError("nenhum texto extraído")
    doc.meta.setdefault("kind", ext.lstrip(".") or "text")
    doc.meta["segments"] = len(doc.segments)
    doc.meta["chars"] = sum(len(s.text) for s in doc.segments)
    return doc
