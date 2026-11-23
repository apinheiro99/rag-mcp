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
