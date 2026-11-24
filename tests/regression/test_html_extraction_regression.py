import pytest

from app.extraction import html_to_segments


pytestmark = pytest.mark.regression


def test_html_title_survives_head_filtering_regression():
    """Regression: <head> used to be skipped wholesale, losing <title>."""
    segs, title = html_to_segments(
        "<html><head><title>T</title></head>"
        "<body><h1>H</h1><p>Hello</p><script>bad()</script></body></html>"
    )
    assert title == "T"
    joined = " ".join(s.text for s in segs)
    assert "Hello" in joined
    assert "bad()" not in joined
    assert any(s.loc.get("heading") == "H" for s in segs)


def test_html_head_title_is_metadata_not_a_searchable_segment():
    segs, title = html_to_segments(
        "<html><head><title>Laws of UX</title><style>noise</style></head>"
        "<body><h1>Jakob's Law</h1><p>Users prefer familiar interfaces.</p></body></html>"
    )
    assert title == "Laws of UX"
    assert [s.text for s in segs] == ["Jakob's Law\n\nUsers prefer familiar interfaces."]
    assert segs[0].loc["heading"] == "Jakob's Law"


def test_epub_repeated_head_titles_never_become_chunks(tmp_path):
    import zipfile
    from app.extraction import extract
    from app.chunking import chunk_segments, validate_ingest_config

    book = tmp_path / "sample.epub"
    chapters = ["Jakob's Law", "Miller's Law", "Law of Proximity"]
    with zipfile.ZipFile(book, "w") as zf:
        zf.writestr("META-INF/container.xml",
                    '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        manifest = "".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>'
                           for i in range(len(chapters)))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        zf.writestr("book.opf", '<package xmlns="http://www.idpf.org/2007/opf">'
                    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                    '<dc:title>Laws of UX</dc:title></metadata>'
                    f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        for i, heading in enumerate(chapters):
            zf.writestr(f"c{i}.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml">'
                        '<head><title>Laws of UX</title></head>'
                        f'<body><h1>{heading}</h1><p>Relevant explanation {i}.</p></body></html>')

    doc = extract(book, book.name)
    assert doc.meta["title"] == "Laws of UX"
    chunks = chunk_segments(doc.segments, validate_ingest_config({}, {}))
    assert len(chunks) == len(chapters)
    assert all("Laws of UX" not in chunk.text for chunk in chunks)
    assert [chunk.loc["heading"] for chunk in chunks] == chapters
    assert [chunk.loc["chapter_index"] for chunk in chunks] == [1, 2, 3]


def test_html_visible_title_and_short_body_are_not_discarded():
    segs, title = html_to_segments(
        "<html><head><title>Book</title></head><body><h1>Book</h1><p>Yes.</p></body></html>"
    )
    assert title == "Book"
    assert "Book" in segs[0].text and "Yes." in segs[0].text
