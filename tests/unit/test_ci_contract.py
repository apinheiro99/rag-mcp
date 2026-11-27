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
        "Release Gate",
    } <= needs


def test_feature_branches_show_all_four_stages_without_production_side_effects():
    ci = yaml.safe_load(Path(".gitlab-ci.yml").read_text())
    assert ci["stages"] == ["validate", "tag", "release", "publish", "deploy"]
    for job in ("ci-tag-green", "ci-tag-red"):
        assert "$CI_PIPELINE_SOURCE == 'push'" not in str(ci[job]["rules"])  # YAML stores the quoted expression without outer YAML quotes
        assert '$CI_PIPELINE_SOURCE == "push"' in str(ci[job]["rules"])
    assert ci["Release Gate"]["stage"] == "release"
    assert '$CI_PIPELINE_SOURCE == "push"' in str(ci["Release Gate"]["rules"])
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
