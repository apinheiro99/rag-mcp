from app.ingest_policy import should_auto_start_upload, should_ignore_empty_upload


def test_auto_start_when_collection_is_configured():
    assert should_auto_start_upload(
        enabled=True,
        collection_config={"strategy": "recursive", "chunk_size": 1500},
        collection_model="text-embedding-qwen3-embedding-8b",
        default_model=None,
    )


def test_auto_start_can_use_global_embedding_model():
    assert should_auto_start_upload(
        enabled=True,
        collection_config={"strategy": "recursive"},
        collection_model=None,
        default_model="embedding-default",
    )


def test_unconfigured_collection_remains_staged():
    assert not should_auto_start_upload(
        enabled=True,
        collection_config={},
        collection_model="embedding-model",
        default_model=None,
    )


def test_auto_start_can_be_disabled_by_configuration():
    assert not should_auto_start_upload(
        enabled=False,
        collection_config={"strategy": "recursive"},
        collection_model="embedding-model",
        default_model=None,
    )


def test_zero_byte_upload_is_ignored():
    assert should_ignore_empty_upload(size_bytes=0)


def test_non_empty_upload_is_not_ignored():
    assert not should_ignore_empty_upload(size_bytes=1)
