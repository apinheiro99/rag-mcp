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
    for tab, label in (("dashboard","Dashboard"),("import","Import"),("documents","Documents"),("search","Search"),("settings","Settings")):
        assert f'data-tab="{tab}"' in HTML
        assert f'<span>{label}</span></button>' in HTML


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
    assert 'id="dashboardStats"' in HTML
    assert 'id="collectionBehaviorTable"' in HTML


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


def test_api_does_not_expose_document_supersession():
    api = Path("app/api.py").read_text()
    schema = Path("app/schema.sql").read_text()
    assert '"/documents/{doc_id}/supersede"' not in api
    assert "superseded_by UUID" not in schema


def test_dashboard_matches_reference_overview_structure():
    html = Path("app/static/index.html").read_text()
    js = Path("app/static/app.js").read_text()
    for element_id in (
        "dashboardStats",
        "runtimeHealth",
        "collectionStatus",
        "dashboardActivity",
        "modelRuntimeGrid",
    ):
        assert f'id="{element_id}"' in html
    assert "renderDashboardOverview" in js
    assert "Embedding + Reranker" in js
    assert "Existing categories only" in js


def test_search_ui_exposes_trace_and_evidence_graph():
    html = Path("app/static/index.html").read_text()
    js = Path("app/static/app.js").read_text()
    assert 'id="searchDiagnostics"' in html
    assert 'id="evidenceGraph"' in html
    assert "include_graph: true" in js
    assert "Evidence Graph" in js
    assert "trace_id" in js


def test_documents_ui_uses_delete_not_supersession_controls():
    js = Path("app/static/app.js").read_text()
    assert "openSupersedeDoc" not in js
    assert "/supersede" not in js
    assert "deleteDoc" in js

def test_search_ui_exposes_usefulness_cost_topk_and_selection_reason():
    js = Path("app/static/app.js").read_text()
    assert "usefulness" in js
    assert "returned_context_tokens" in js
    assert "effective_top_k" in js
    assert "selection?.reason" in js


def test_settings_exposes_manual_book_category_management_only():
    html = Path("app/static/index.html").read_text()
    js = Path("app/static/app.js").read_text()
    assert 'id="categoriesList"' in html
    assert "createBookCategory()" in html
    assert "renameBookCategory" in js
    assert "deleteBookCategory" in js
    assert "reviewCategoryDecision" not in js
    assert "Yes, add category" not in js
    assert "Uncategorized" in js
    assert "Top-level collections are fixed" in js


def test_directory_sources_support_target_category_edit_and_clone():
    js = Path("app/static/app.js").read_text()
    assert "cloneSource" in js
    assert "Clone directory source" in js
    assert 'id="sourceEditCollection"' in js
    assert 'id="sourceEditCategory"' in js
    assert "renameSelectedSourceCategory" in js
    assert "category_id:" in js
    assert "source_type:" in js


def test_classify_applies_directly_without_success_modal():
    js = Path("app/static/app.js").read_text()
    start = js.index("async function classifyDoc")
    end = js.index("\nasync function deleteDoc", start)
    block = js[start:end]
    assert "Categorized as" in block
    assert 'openModal("Category not found"' in block
    success_start = block.index("if (existing && r.auto_applied)")
    success_end = block.index("return;", success_start)
    success_branch = block[success_start:success_end]
    assert "openModal" not in success_branch


def test_manual_classify_route_uses_same_ready_classification_runtime_as_worker():
    api = Path("app/api.py").read_text()
    runtime = Path("app/classification_runtime.py").read_text()
    route_start = api.index('async def classify_document_route')
    route_end = api.index('class BulkClassifyRequest', route_start)
    route = api[route_start:route_end]
    assert "classify_ready_document(doc_id, model)" in route
    assert "await classify_ready_document(self.current_document)" in runtime


def test_classification_decision_endpoint_is_removed():
    api = Path("app/api.py").read_text()
    assert "classification-decision" not in api
    assert "ClassificationDecision" not in api


def test_visual_reference_contract_uses_docs_screens_but_preserves_runtime_background():
    css = Path("app/static/style.css").read_text()
    architecture = Path("docs/architecture.md").read_text()
    assert "Visual reference contract" in css
    assert "--sidebar-w:198px" in css
    assert "docs/screens/02..06" in css
    assert "07_background.png" in architecture
    assert "not authoritative for the runtime background" in architecture
    assert "app/static/branding/rag-mcp-background.png" in architecture


def test_ui_copy_matches_existing_category_only_classification_contract():
    html = Path("app/static/index.html").read_text()
    assert "asks you to approve a new one" not in html
    assert "unknown topics wait for your approval" not in html
    assert "otherwise the document stays uncategorized" in html
    assert "unmatched books remain uncategorized" in html


def test_portfolio_readme_uses_versioned_product_screenshots():
    readme = Path("README.md").read_text()
    for name in (
        "02_dashboard.png",
        "03_import.png",
        "04_documents.png",
        "05_search.png",
        "06_settings.png",
    ):
        assert f"docs/screens/{name}" in readme


def test_reference_search_and_settings_structure_are_real_data_driven():
    html = Path("app/static/index.html").read_text()
    js = Path("app/static/app.js").read_text()
    for element_id in (
        "evidencePreview",
        "rankedResults",
        "collectionBehaviorTable",
        "sourcesList",
        "systemServerGrid",
    ):
        assert f'id="{element_id}"' in html
    assert "renderReferenceSearchResults" in js
    assert "top.content" in js
    assert "state.collections" in js
    assert "state.sources" in js
    assert "Classification LLM" not in html + js


def test_reference_layout_keeps_current_background_exception():
    html = Path("app/static/index.html").read_text()
    css = Path("app/static/style.css").read_text()
    assert "dashboard-kpis" in html
    assert "dashboard-ops-grid" in html
    assert "search-output-grid" in html
    assert "settings-two-col" in html
    assert css.count("rag-mcp-background.png") == 1
    assert "07_background.png" not in css


def test_dashboard_pixel_contract_contains_reference_iconography_and_dimensions():
    html = Path("app/static/index.html").read_text()
    js = Path("app/static/app.js").read_text()
    css = Path("app/static/style.css").read_text()
    assert 'id="pageHeaderIcon"' in html
    assert '<span>Dashboard</span></button>' in html
    assert 'layers: `<svg' in js
    assert '["layers", totals.collections, "Collections"]' in js
    assert 'stroke-dasharray="1.2 3.1"' in js
    assert '["embedding","Embedding Model"' in js
    assert '["rerank","Reranker (4B)"' in js
    assert '["classify","Classification"' in js
    assert '["resident","Resident-Model Behavior"' in js
    assert ':root{--sidebar-w:214px}' in css
    assert 'height:115px!important' in css
    assert 'height:300px;min-height:300px' in css
    assert 'height:310px;min-height:310px' in css


def test_remaining_reference_screens_have_pixel_contract_structure():
    html = Path("app/static/index.html").read_text()
    js = Path("app/static/app.js").read_text()
    css = Path("app/static/style.css").read_text()
    # Import
    for token in ("import-destination-grid", "import-progress-grid", "import-config-layout", "reference-upload-results"):
        assert token in html + css
    # Documents
    for token in ("documents-filter-row", "documents-bulk-row", "documents-table-wrap", "documents-pagination"):
        assert token in html + css
    assert 'docsLimit: 25' in js
    # Search
    for token in ("search-main-bar", "search-tuning-strip", "search-scope-card", "reference-ranked-list"):
        assert token in html + js + css
    assert "selection?.reason" in js
    # Settings
    for token in ("settings-runtime-layout", "settings-worker-panel", "reference-collection-behavior", "reference-sources", "reference-system"):
        assert token in html + css


def test_reference_page_subtitles_match_approved_screens():
    js = Path("app/static/app.js").read_text()
    assert "Add files, folders, and automate routing into collections." in js
    assert "Inspect, classify, organize, and maintain your indexed library." in js
    assert "Query the retrieval system across enabled collections." in js
    assert "Configure runtime, collection behavior, and directory sources." in js
