import uuid

from app.ingest import chunk_uuid


def test_chunk_uuid_is_deterministic():
    doc = uuid.uuid4()
    assert chunk_uuid(doc, 3) == chunk_uuid(str(doc), 3)


def test_chunk_uuid_changes_with_sequence():
    doc = uuid.uuid4()
    assert chunk_uuid(doc, 1) != chunk_uuid(doc, 2)


def test_chunk_uuid_is_valid_uuid():
    value = chunk_uuid(uuid.uuid4(), 0)
    assert str(uuid.UUID(value)) == value
