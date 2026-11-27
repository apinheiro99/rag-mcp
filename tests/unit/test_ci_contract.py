from pathlib import Path
import yaml


def test_ci_has_separate_blocking_validation_gates():
    ci = yaml.safe_load(Path(".gitlab-ci.yml").read_text())
    for job in (
        "Secret Scan",
        "Public Surface",
        "Static Checks",
        "Unit",
        "Regression",
        "Integration",
    ):
        assert job in ci
        extends = ci[job].get("extends")
        assert extends == ".validation-job" or ".validation-job" in (extends or [])


def test_publish_depends_on_all_validation_gates_and_green_tag():
    ci = yaml.safe_load(Path(".gitlab-ci.yml").read_text())
    needs = {x if isinstance(x, str) else x["job"] for x in ci["Publish GitHub"]["needs"]}
    assert {
        "Secret Scan",
        "Public Surface",
        "Static Checks",
        "Unit",
        "Regression",
        "Integration",
        "ci-tag-green",
        "Build Release",
    } <= needs


def test_feature_branches_run_validation_only_and_main_owns_release_side_effects():
    ci = yaml.safe_load(Path(".gitlab-ci.yml").read_text())
    assert ci["stages"] == ["validate", "tag", "release", "publish", "deploy"]
    for job in ("ci-tag-green", "Build Release", "Publish GitHub", "Deploy Production"):
        assert "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH" in str(ci[job]["rules"])
    assert "Publish Gate" not in ci
    assert "Deploy Gate" not in ci
    assert "ci-tag-red" not in ci

def test_publish_script_verifies_exact_github_sha():
    script = Path("scripts/publish-github.sh").read_text()
    assert "git ls-remote github refs/heads/main" in script
    assert 'REMOTE_MAIN" = "$CI_COMMIT_SHA' in script
    assert 'REMOTE_TAG" = "$CI_COMMIT_SHA' in script


def test_release_version_is_the_pipeline_tag_and_deploy_consumes_manifest():
    ci = Path(".gitlab-ci.yml").read_text()
    release = Path("scripts/build_release.py").read_text()
    deploy = Path("scripts/deploy-prod.sh").read_text()
    dockerfile = Path("Dockerfile").read_text()
    compose = Path("deploy/prod/compose.yml").read_text()
    tag_formula = 'v$(date -u -d "$CI_PIPELINE_CREATED_AT" +%Y.%m).$(printf "%06d" "$CI_PIPELINE_IID")'
    assert ci.count(tag_formula) >= 2
    assert '"tag": args.version' in release
    assert '"version": args.version' in release
    assert 'release/manifest.json' in deploy
    assert 'RELEASE_VERSION' in deploy and 'RELEASE_COMMIT' in deploy
    assert '[ "$RELEASE_VERSION" = "$RELEASE_TAG" ]' in deploy
    assert '[ "$RELEASE_COMMIT" = "$CI_COMMIT_SHA" ]' in deploy
    assert "ARG RAG_BUILD_VERSION=dev" in dockerfile
    assert "ARG RAG_BUILD_COMMIT=" in dockerfile
    assert "org.opencontainers.image.version=$RAG_BUILD_VERSION" in dockerfile
    assert "org.opencontainers.image.revision=$RAG_BUILD_COMMIT" in dockerfile
    assert "RAG_BUILD_VERSION: ${RAG_BUILD_VERSION:-dev}" in compose
    assert "RAG_BUILD_COMMIT: ${RAG_BUILD_COMMIT:-}" in compose


def test_mcp_get_document_offloads_extraction_from_event_loop():
    source = Path("app/mcp_server.py").read_text()
    assert "text = await asyncio.to_thread(_load_text)" in source


def test_ingest_start_rejects_ready_documents_and_e2e_defaults_match_prod_topology():
    api = Path("app/api.py").read_text()
    e2e = Path("scripts/e2e_check.py").read_text()
    assert "WHERE d.id = ANY(%s::uuid[]) AND d.status='staged'" in api
    assert 'http://docker-prod01.lab:8000' in e2e
    assert 'http://192.168.1.10:1234' in e2e


def test_reindex_blocks_active_documents_and_graph_enrichment_is_fail_open():
    api = Path("app/api.py").read_text()
    ingest = Path("app/ingest.py").read_text()
    assert 'SELECT id FROM documents WHERE collection_id=%s AND status IN {ACTIVE_STATUSES} FOR UPDATE' in api
    assert 'coleção possui documento em processamento; reindexe após a ingestão terminar' in api
    assert 'graph enrichment failed; background worker will retry' in ingest
    assert 'except Exception:' in ingest[ingest.index('await rebuild_document_mentions(doc_id)')-120:ingest.index('await rebuild_document_mentions(doc_id)')+220]


def test_ingest_and_search_gpu_work_use_single_runtime_fifo():
    ingest = Path("app/ingest.py").read_text()
    search = Path("app/search.py").read_text()
    rerank_runtime = Path("app/rerank_runtime.py").read_text()
    assert 'runtime_scheduler.run(\n                f"ingest-embed:{doc_id}"' in ingest
    assert 'requirements.append(ModelRequirement(reranker_model, context_length))' in ingest
    assert 'runtime_scheduler.run(\n            "search", client, requirements, execute_search_pipeline' in search
    assert 'runtime_scheduler.run(\n            "rerank"' in rerank_runtime


def test_integration_ci_reports_passed_diagnostics_for_explain_measurements():
    ci = Path(".gitlab-ci.yml").read_text()
    assert 'python3 -m pytest -q -rP tests/integration' in ci
