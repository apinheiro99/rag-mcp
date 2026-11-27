from scripts.secret_scan import likely_literal_secret

def test_secret_scan_allows_placeholders_and_environment_references():
    assert likely_literal_secret("change-me-strong-password") is False
    assert likely_literal_secret("rag-ci-only") is False
    assert likely_literal_secret("$RAG_API_TOKEN") is False
    assert likely_literal_secret("${CI_JOB_TOKEN}") is False

def test_secret_scan_flags_long_literal_credentials():
    assert likely_literal_secret("super-secret-production-token-12345") is True
