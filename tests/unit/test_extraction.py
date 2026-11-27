import pytest

from app.extraction import (
    ExtractionError,
    ExtractedDoc,
    Segment,
    extract,
    markdown_to_segments,
    normalize_ws,
    read_text_file,
    decode_text_bytes,
)


def test_extracted_doc_round_trip():
    original = ExtractedDoc([Segment("abc", {"page": 2})], {"kind": "x"})
    restored = ExtractedDoc.from_json(original.to_json())
    assert restored == original
    assert restored.full_text() == "abc"


def test_normalize_ws():
    assert normalize_ws("a  \r\n\r\n\r\n b\t \n") == "a\n\n b"


def test_normalize_ws_removes_nul_without_losing_text():
    assert normalize_ws("before\x00after\n") == "beforeafter"


def test_extracted_doc_from_json_sanitizes_legacy_cached_nul():
    restored = ExtractedDoc.from_json({
        "segments": [{"text": "before\x00after", "loc": {"page": 1}}],
        "meta": {"kind": "pdf"},
    })

    assert restored.segments[0].text == "beforeafter"
    assert restored.segments[0].loc == {"page": 1}


def test_markdown_preserves_heading_hierarchy():
    segs = markdown_to_segments("# Top\nIntro\n## Child\nBody")
    assert segs[0].loc["heading"] == "Top"
    assert segs[1].loc["heading"] == "Top > Child"
    assert "Child" in segs[1].text




def test_decode_text_bytes_recovers_cp1252_smart_punctuation():
    raw = "‘quoted’ — €".encode("cp1252")
    assert decode_text_bytes(raw) == "‘quoted’ — €"


def test_read_text_file_prefers_cp1252_before_latin1(tmp_path):
    p = tmp_path / "cp1252.txt"
    p.write_bytes("aspas “corretas” — preço €10".encode("cp1252"))

    text = read_text_file(p)

    assert text == "aspas “corretas” — preço €10"
    assert "\x93" not in text
    assert "\x94" not in text

def test_extract_text_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello\n\nworld", encoding="utf-8")
    doc = extract(p, p.name)
    assert doc.meta["kind"] == "text"
    assert doc.meta["segments"] == 1
    assert "hello" in doc.full_text()


def test_extract_empty_text_rejected(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n", encoding="utf-8")
    with pytest.raises(ExtractionError, match="arquivo vazio"):
        extract(p, p.name)


def test_extract_unknown_extension_rejected(tmp_path):
    p = tmp_path / "x.xyz"
    p.write_text("hello", encoding="utf-8")
    with pytest.raises(ExtractionError, match="formato não suportado"):
        extract(p, p.name)


def test_extract_generic_json_structures_records_without_raw_json_noise(tmp_path):
    p = tmp_path / "records.json"
    p.write_text(
        '[{"name":"alpha","enabled":true,"count":3},{"name":"beta","enabled":false,"count":4}]',
        encoding="utf-8",
    )

    doc = extract(p, p.name)

    assert doc.meta["kind"] == "json"
    assert doc.meta["parser"] == "generic-json"
    assert len(doc.segments) == 2
    assert "name: alpha" in doc.segments[0].text
    assert "enabled: true" in doc.segments[0].text.lower()
    assert not doc.segments[0].text.lstrip().startswith("{")


def test_extract_jsonl_creates_one_segment_per_record(tmp_path):
    p = tmp_path / "records.jsonl"
    p.write_text('{"name":"alpha"}\n{"name":"beta"}\n', encoding="utf-8")

    doc = extract(p, p.name)

    assert doc.meta["kind"] == "jsonl"
    assert doc.meta["parser"] == "generic-jsonl"
    assert [s.loc["record"] for s in doc.segments] == [1, 2]
    assert "name: alpha" in doc.segments[0].text
    assert "name: beta" in doc.segments[1].text


def test_extract_chatgpt_conversations_json_keeps_semantic_messages_only(tmp_path):
    p = tmp_path / "conversations.json"
    payload = [{
        "title": "RAG parser design",
        "create_time": 123.0,
        "mapping": {
            "root": {"id": "root", "message": None, "parent": None, "children": ["u"]},
            "u": {
                "id": "u",
                "parent": "root",
                "children": ["a"],
                "message": {
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Como desenho o parser?"]},
                    "metadata": {},
                },
            },
            "a": {
                "id": "a",
                "parent": "u",
                "children": [],
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["Use detectores especializados com fallback."]},
                    "metadata": {},
                },
            },
            "sys": {
                "id": "sys",
                "parent": "root",
                "children": [],
                "message": {
                    "author": {"role": "system"},
                    "content": {"content_type": "text", "parts": ["hidden system prompt"]},
                    "metadata": {},
                },
            },
        },
    }]
    import json
    p.write_text(json.dumps(payload), encoding="utf-8")

    doc = extract(p, p.name)

    assert doc.meta["kind"] == "chatgpt-export"
    assert doc.meta["parser"] == "chatgpt-export"
    assert doc.meta["conversations"] == 1
    assert len(doc.segments) == 2
    assert doc.segments[0].loc["conversation_title"] == "RAG parser design"
    assert doc.segments[0].loc["role"] == "user"
    assert "Como desenho o parser?" in doc.segments[0].text
    assert "hidden system prompt" not in doc.full_text()
    assert '"mapping"' not in doc.full_text()


def test_extract_chatgpt_html_uses_embedded_json_instead_of_generic_html(tmp_path):
    p = tmp_path / "chat.html"
    p.write_text(
        """<!doctype html><html><head><title>ChatGPT Data Export</title></head>
        <body><script>var jsonData = [{"title":"Teste","mapping":{"u":{"id":"u","parent":null,"children":[],"message":{"author":{"role":"user"},"content":{"content_type":"text","parts":["mensagem útil"]},"metadata":{}}}}}];</script></body></html>""",
        encoding="utf-8",
    )

    doc = extract(p, p.name)

    assert doc.meta["kind"] == "chatgpt-export"
    assert doc.meta["parser"] == "chatgpt-export"
    assert "mensagem útil" in doc.full_text()
    assert "var jsonData" not in doc.full_text()


def test_extract_fb2(tmp_path):
    p = tmp_path / "book.fb2"
    p.write_text('''<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body><section><title><p>Chapter One</p></title><p>Hello FB2</p><p>Second paragraph</p></section></body></FictionBook>''', encoding="utf-8")
    doc = extract(p, p.name)
    assert doc.meta["kind"] == "fb2"
    assert "Hello FB2" in doc.full_text()
    assert doc.segments[0].loc["section"] == "Chapter One"


def test_extract_fbz(tmp_path):
    import zipfile
    p = tmp_path / "book.fbz"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("inside.fb2", '<FictionBook><body><section><p>Hello FBZ</p></section></body></FictionBook>')
    doc = extract(p, p.name)
    assert "Hello FBZ" in doc.full_text()


def test_extract_odt(tmp_path):
    import zipfile
    p = tmp_path / "book.odt"
    xml = '''<office:document-content xmlns:office="urn:o" xmlns:text="urn:t"><office:body><office:text><text:h>Heading</text:h><text:p>Hello ODT</text:p></office:text></office:body></office:document-content>'''
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("content.xml", xml)
    doc = extract(p, p.name)
    assert doc.meta["kind"] == "odt"
    assert doc.segments[0].text == "Hello ODT"
    assert doc.segments[0].loc["heading"] == "Heading"


def test_extract_htmlz(tmp_path):
    import zipfile
    p = tmp_path / "book.htmlz"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("index.html", "<html><body><h1>Title</h1><p>Hello HTMLZ</p></body></html>")
    doc = extract(p, p.name)
    assert doc.meta["kind"] == "htmlz"
    assert "Hello HTMLZ" in doc.full_text()
    assert doc.segments[0].loc["member"] == "index.html"


def test_extract_txtz(tmp_path):
    import zipfile
    p = tmp_path / "book.txtz"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("book.txt", "Hello TXTZ")
    doc = extract(p, p.name)
    assert doc.meta["kind"] == "txtz"
    assert doc.full_text() == "Hello TXTZ"


def test_mobi_family_reports_missing_parser(tmp_path, monkeypatch):
    p = tmp_path / "book.azw3"
    p.write_bytes(b"not-a-real-book")
    monkeypatch.setattr("app.extraction.shutil.which", lambda _: None)
    with pytest.raises(ExtractionError, match="mobitool"):
        extract(p, p.name)


def test_extract_python_source_preserves_symbols_and_lines(tmp_path):
    p = tmp_path / "sample.py"
    p.write_text(
        '"""module docs"""\n\n'
        '@decorator\n'
        'def alpha(x):\n'
        '    return x + 1\n\n'
        'class Beta:\n'
        '    def method(self):\n'
        '        return 2\n',
        encoding="utf-8",
    )
    doc = extract(p, p.name)
    assert doc.meta["kind"] == "source_code"
    assert doc.meta["language"] == "python"
    symbols = [seg.loc["symbol"] for seg in doc.segments]
    assert symbols == ["<module>", "alpha", "Beta"]
    alpha = doc.segments[1]
    assert alpha.loc["line_start"] == 3
    assert alpha.loc["line_end"] == 5
    assert "@decorator" in alpha.text


def test_extract_javascript_source_preserves_symbol_blocks(tmp_path):
    p = tmp_path / "sample.js"
    p.write_text(
        "import x from 'x';\n\n"
        "export function alpha() { return 1; }\n"
        "const beta = () => 2;\n",
        encoding="utf-8",
    )
    doc = extract(p, p.name)
    assert doc.meta["kind"] == "source_code"
    assert [seg.loc["symbol"] for seg in doc.segments] == ["<module>", "alpha", "beta"]


def test_external_text_tool_uses_configured_timeout(monkeypatch):
    from app import extraction
    monkeypatch.setattr(extraction.shutil, "which", lambda tool: "/usr/bin/fake")
    monkeypatch.setattr(extraction.env, "external_parser_timeout", 17)
    seen = {}
    def fake_run(args, **kwargs):
        seen.update(kwargs)
        class P:
            returncode = 0
            stdout = b"ok"
            stderr = b""
        return P()
    monkeypatch.setattr(extraction.subprocess, "run", fake_run)
    assert extraction._run_text_tool(["fake"], "fake") == "ok"
    assert seen["timeout"] == 17


def test_external_text_tool_timeout_is_reported(monkeypatch):
    from app import extraction
    import subprocess
    monkeypatch.setattr(extraction.shutil, "which", lambda tool: "/usr/bin/fake")
    monkeypatch.setattr(extraction.env, "external_parser_timeout", 9)
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    monkeypatch.setattr(extraction.subprocess, "run", fake_run)
    with pytest.raises(ExtractionError, match="timeout.*9s"):
        extraction._run_text_tool(["fake"], "fake")
