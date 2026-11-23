"""Regressão: uploads em coleções já configuradas não devem ficar em staged."""

from app.ingest_policy import should_auto_start_upload


def test_configured_collection_upload_does_not_wait_for_manual_configuration():
    collection_config = {
        "strategy": "recursive",
        "chunk_size": 1500,
        "chunk_overlap": 200,
        "min_chunk_size": 120,
        "embed_batch_size": 16,
    }

    assert should_auto_start_upload(
        enabled=True,
        collection_config=collection_config,
        collection_model="text-embedding-qwen3-embedding-8b",
        default_model="text-embedding-qwen3-embedding-8b",
    ) is True
