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
    needs = set(ci["Publish GitHub"]["needs"])
    assert {
        "Secret Scan",
        "Public Surface",
        "Static Checks",
        "Unit",
        "Regression",
        "Integration",
        "ci-tag-green",
    } <= needs


def test_feature_branches_validate_but_do_not_publish_or_deploy():
    ci = Path(".gitlab-ci.yml").read_text()
    assert "$CI_PIPELINE_SOURCE == \"push\"" in ci
    assert "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH" in ci


def test_publish_script_verifies_exact_github_sha():
    script = Path("scripts/publish-github.sh").read_text()
    assert "git ls-remote github refs/heads/main" in script
    assert 'REMOTE_MAIN" = "$CI_COMMIT_SHA' in script
    assert 'REMOTE_TAG" = "$CI_COMMIT_SHA' in script
