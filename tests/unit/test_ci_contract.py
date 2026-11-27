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


def test_feature_branches_show_all_four_stages_without_production_side_effects():
    ci = yaml.safe_load(Path(".gitlab-ci.yml").read_text())
    assert ci["stages"] == ["validate", "tag", "release", "publish", "deploy"]
    for job in ("ci-tag-green", "ci-tag-red"):
        assert "$CI_PIPELINE_SOURCE == 'push'" not in str(ci[job]["rules"])  # YAML stores the quoted expression without outer YAML quotes
        assert '$CI_PIPELINE_SOURCE == "push"' in str(ci[job]["rules"])
    assert ci["Build Release"]["stage"] == "release"
    assert '$CI_PIPELINE_SOURCE == "push"' in str(ci["Build Release"]["rules"])
    assert ci["Build Release"]["artifacts"]["paths"] == ["release/"]
    for job, stage in (("Publish Gate", "publish"), ("Deploy Gate", "deploy")):
        assert ci[job]["stage"] == stage
        assert "$CI_COMMIT_BRANCH != $CI_DEFAULT_BRANCH" in str(ci[job]["rules"])
    for job in ("Publish GitHub", "Deploy Production"):
        assert "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH" in str(ci[job]["rules"])


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
