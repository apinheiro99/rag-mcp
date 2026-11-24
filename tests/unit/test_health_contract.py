from pathlib import Path
API=Path('app/api.py').read_text()
JS=Path('app/static/app.js').read_text()
ARCH=Path('docs/architecture.md').read_text()
def test_health_exposes_graph_worker(): assert '"graph_worker"' in API
def test_classification_ui_not_labeled_ai(): assert 'AI queued' not in JS and 'AI classification' not in JS
def test_docs_match_current_architecture():
    assert 'Embedding-based collection classification' in ARCH
    assert 'background worker' in ARCH
    assert 'fail-open' in ARCH
