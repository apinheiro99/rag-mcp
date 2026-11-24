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


def test_deploy_migrates_explicitly_then_waits_for_quiescence_and_warms_up():
    script = Path("scripts/deploy-prod.sh").read_text()
    migration = script.index("python -m app.migrate_library_taxonomy")
    quiescent = script.index("wait_system_quiescent ||")
    evaluation = script.index('exec -T app "${EVAL_ARGS[@]}"')
    assert migration < quiescent < evaluation
    assert "--warmup-rounds" in script
    assert "source_worker" in script
    assert "model_runtime" in script


def test_application_startup_does_not_run_destructive_taxonomy_migration():
    main = Path("app/main.py").read_text()
    assert "await ensure_core_collections()" in main
    assert "apply_migration=True" not in main


def test_repository_bootstrap_does_not_force_resync_on_every_restart():
    source = Path("app/source_sync.py").read_text()
    assert "next_sync_at=coalesce(collection_sources.next_sync_at, now())" in source


def test_taxonomy_migration_runs_as_package_module_not_loose_script():
    script = Path("scripts/deploy-prod.sh").read_text()
    assert "python -m app.migrate_library_taxonomy" in script
    assert "python /srv/rag/scripts/migrate_library_taxonomy.py" not in script


def test_taxonomy_migration_refreshes_postgres_planner_statistics():
    migration = Path("app/migrate_library_taxonomy.py").read_text()
    assert "refresh_planner_statistics" in migration
    assert '("collections", "documents", "chunks")' in migration
    assert "ANALYZE {table}" in migration
    assert "await refresh_planner_statistics(db)" in migration
