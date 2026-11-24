import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app/static/index.html").read_text()
JS = (ROOT / "app/static/app.js").read_text()


def test_static_ui_has_unique_ids_and_resolvable_handlers():
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    assert len(ids) == len(set(ids))

    functions = set(re.findall(r'(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(', JS))
    allowed = {"if", "document", "getElementById", "click"}
    for _, code in re.findall(r'\b(onclick|onchange|oninput|onkeydown)="([^"]+)"', HTML):
        for name in re.findall(r'\b([A-Za-z_$][\w$]*)\s*\(', code):
            assert name in functions or name in allowed, f"missing UI handler: {name}"


def test_import_keeps_live_ingestion_progress_in_import_tab():
    for element_id in (
        "importIngestSummary", "importIngestBar", "importIngestCounts", "importCurrentDoc"
    ):
        assert f'id="{element_id}"' in HTML
        assert element_id in JS
    assert 'await refreshStaged(); await pollStatus();' in JS


def test_primary_navigation_is_english():
    for label in ("Dashboard", "Import", "Documents", "Search", "Settings"):
        assert f">{label}</button>" in HTML


def test_import_exposes_automatic_classification_when_llm_is_configured():
    assert 'Automatic classification' in JS
    assert '__auto__' in JS
    assert 'staging_collection_name' in JS


def test_brand_background_and_glass_have_single_shell_definition():
    css = (ROOT / "app/static/style.css").read_text()
    assert css.count("rag-mcp-background.png") == 1
    assert len(re.findall(r"(?m)^body\s*\{", css)) == 1
    brand = re.search(r"(?m)^\.sidebar \.brand\s*\{([^}]+)\}", css).group(1)
    assert "border:0" in brand
    assert "border-bottom:1px" not in brand
    assert "background:var(--glass-sidebar)" in css
    assert "background:var(--glass-card)" in css
    assert 'class="collection-section"' in HTML
    assert 'aria-label="Collections"' in HTML
