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


def test_import_exposes_automatic_book_classification_without_staging_collection():
    assert 'Automatic classification' in JS
    assert '__auto__' in JS
    assert 'Computer Books' in JS
    assert 'staging_collection_name' not in JS


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


def test_document_bulk_actions_and_dialogs_are_available():
    assert not re.search(r"\b(?:confirm|alert|prompt)\s*\(", JS)
    assert "tokenBanner" not in HTML + JS
    assert '<dialog id="actionDialog"' in HTML
    assert 'method="dialog"' in HTML
    for action in ("Classify", "Move", "Reindex", "Retry", "Cancel", "Delete"):
        assert f'id="btnBulk{action}Selected"' in HTML
        assert f"bulk{action}Selected()" in HTML
    assert 'onclick="selectFilteredDocs()"' in HTML
    assert 'onclick="clearDocsSelection()"' in HTML
    assert 'documentParams(limit,0)' in JS


def test_runtime_branding_assets_are_documented():
    for name in ("rag-mcp-lockup.png", "rag-mcp-icon.png", "rag-mcp-background.png"):
        assert (ROOT / "docs/screens" / name).read_bytes() == (ROOT / "app/static/branding" / name).read_bytes()


def test_api_exposes_document_supersession_governance():
    api = Path("app/api.py").read_text()
    assert '"/documents/{doc_id}/supersede"' in api
    assert "superseded_by" in api


def test_dashboard_exposes_background_runtime_state():
    js = Path("app/static/app.js").read_text()
    for token in ("Classification worker", "Source sync worker", "Graph worker", "Model runtime"):
        assert token in js


def test_search_ui_exposes_trace_and_evidence_graph():
    html = Path("app/static/index.html").read_text()
    js = Path("app/static/app.js").read_text()
    assert 'id="searchDiagnostics"' in html
    assert 'id="evidenceGraph"' in html
    assert "include_graph: true" in js
    assert "Evidence Graph" in js
    assert "trace_id" in js


def test_documents_ui_exposes_supersession_controls():
    js = Path("app/static/app.js").read_text()
    assert "openSupersedeDoc" in js
    assert "/supersede" in js

def test_search_ui_exposes_usefulness_cost_topk_and_selection_reason():
    js = Path("app/static/app.js").read_text()
    assert "usefulness" in js
    assert "returned_context_tokens" in js
    assert "effective_top_k" in js
    assert "selection?.reason" in js


def test_settings_exposes_book_categories_and_yes_no_review():
    html = Path("app/static/index.html").read_text()
    js = Path("app/static/app.js").read_text()
    assert 'id="categoriesList"' in html
    assert "createBookCategory()" in html
    assert "reviewCategoryDecision" in js
    assert "Yes</button>" in js
    assert "No</button>" in js
    assert "Uncategorized" in js
    assert "Top-level collections are fixed" in js
