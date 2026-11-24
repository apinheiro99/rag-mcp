from pathlib import Path


def test_deploy_persists_last_good_and_rolls_back_on_post_deploy_failure():
    script = Path("scripts/deploy-prod.sh").read_text()
    assert 'LAST_GOOD_FILE="$PRIVATE/last-successful-deploy"' in script
    assert "rollback_on_failure()" in script
    assert "trap rollback_on_failure EXIT" in script
    assert 'wait_app_ready "$LAST_GOOD_COMMIT"' in script
    assert "rollback recovered production" in script

    evaluation = script.index('exec -T app "${EVAL_ARGS[@]}"')
    mark_good = script.index('> "$LAST_GOOD_FILE"', evaluation)
    disarm = script.index("ROLLBACK_ARMED=false", mark_good)
    assert evaluation < mark_good < disarm
