-- ============================================================
-- RAG Server — schema idempotente (aplicado a cada boot)
-- PostgreSQL + pgvector
-- ============================================================
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS collections (
    id              UUID PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    embedding_model TEXT,                -- travado no 1º embedding bem-sucedido
    embedding_dim   INT,
    ingest_config   JSONB NOT NULL DEFAULT '{}',
    default_search  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE collections ADD COLUMN IF NOT EXISTS default_search BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS collection_sources (
    id               UUID PRIMARY KEY,
    collection_id    UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    path             TEXT NOT NULL,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    interval_seconds INT NOT NULL,
    exclude_patterns JSONB NOT NULL DEFAULT '[]',
    last_sync_at     TIMESTAMPTZ,
    next_sync_at     TIMESTAMPTZ,
    last_error       TEXT,
    last_stats       JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection_id, path)
);
CREATE INDEX IF NOT EXISTS collection_sources_due_idx
    ON collection_sources(enabled, next_sync_at);

-- status: staged | pending | extracting | chunking | embedding | waiting | ready | error
CREATE TABLE IF NOT EXISTS documents (
    id              UUID PRIMARY KEY,
    collection_id   UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    source_path     TEXT NOT NULL,       -- caminho relativo original (árvore de pastas)
    source_id       UUID REFERENCES collection_sources(id) ON DELETE SET NULL,
    source_mtime_ns BIGINT,
    stored_path     TEXT NOT NULL,       -- blob no disco (content-addressed)
    content_sha256  TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'staged',
    error           TEXT,
    attempts        INT NOT NULL DEFAULT 0,
    retry_at        TIMESTAMPTZ,
    chunks_total    INT NOT NULL DEFAULT 0,
    chunks_embedded INT NOT NULL DEFAULT 0,
    ingest_config   JSONB,               -- snapshot efetivo dos parâmetros
    auto_suggestion JSONB,               -- sugestão do LLM, se modo automático
    meta            JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ,
    UNIQUE (collection_id, content_sha256)
);
CREATE INDEX IF NOT EXISTS documents_collection_idx ON documents(collection_id);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_id UUID REFERENCES collection_sources(id) ON DELETE SET NULL;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_mtime_ns BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS documents_source_path_idx ON documents(source_id, source_path) WHERE source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS documents_status_idx ON documents(status);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS graph_indexed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS documents_graph_stale_idx
    ON documents(status, graph_indexed_at, processed_at);

CREATE INDEX IF NOT EXISTS documents_queue_idx ON documents(status, created_at)
    WHERE status IN ('pending','extracting','chunking','embedding','waiting');

CREATE TABLE IF NOT EXISTS chunks (
    id            UUID PRIMARY KEY,      -- determinístico: uuid5(doc_id, seq)
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    seq           INT NOT NULL,
    content       TEXT NOT NULL,
    loc           JSONB NOT NULL DEFAULT '{}',  -- page / chapter / heading / char span
    embedded      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, seq)
);
CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks(document_id);
CREATE INDEX IF NOT EXISTS chunks_collection_idx ON chunks(collection_id);
CREATE INDEX IF NOT EXISTS chunks_unembedded_idx ON chunks(document_id) WHERE embedded = FALSE;

-- Tabelas de vetores por dimensão (vec_<dim>) são criadas sob demanda
-- em db.ensure_vector_table(), com índice HNSW (vector <=2000 dims,
-- halfvec <=4000 dims; acima disso, HNSW binário + refinamento cosseno).

CREATE TABLE IF NOT EXISTS events (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    level         TEXT NOT NULL DEFAULT 'info',
    collection_id UUID,
    document_id   UUID,
    message       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events(ts DESC);

CREATE TABLE IF NOT EXISTS retrieval_events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    query_text      TEXT NOT NULL,
    collections     JSONB NOT NULL DEFAULT '[]',
    filters         JSONB NOT NULL DEFAULT '{}',
    top_k           INT NOT NULL,
    min_score       DOUBLE PRECISION NOT NULL,
    embedding_ms    DOUBLE PRECISION NOT NULL DEFAULT 0,
    db_ms           DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_ms        DOUBLE PRECISION NOT NULL DEFAULT 0,
    candidate_count INT NOT NULL DEFAULT 0,
    result_count    INT NOT NULL DEFAULT 0,
    top_score       DOUBLE PRECISION,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS retrieval_events_ts_idx ON retrieval_events(ts DESC);

-- English lexical retrieval, alongside dense vectors.
CREATE INDEX IF NOT EXISTS chunks_content_english_fts_idx
    ON chunks USING gin (to_tsvector('english', content));

ALTER TABLE retrieval_events ADD COLUMN IF NOT EXISTS dense_ms DOUBLE PRECISION;
ALTER TABLE retrieval_events ADD COLUMN IF NOT EXISTS lexical_ms DOUBLE PRECISION;
ALTER TABLE retrieval_events ADD COLUMN IF NOT EXISTS heading_ms DOUBLE PRECISION;

-- Persistent CPU evidence graph: deterministic named-concept mentions per chunk.
CREATE TABLE IF NOT EXISTS graph_mentions (
    chunk_id      UUID NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    concept       TEXT NOT NULL,
    PRIMARY KEY (chunk_id, concept)
);
CREATE INDEX IF NOT EXISTS graph_mentions_concept_idx ON graph_mentions(concept);
CREATE INDEX IF NOT EXISTS graph_mentions_document_idx ON graph_mentions(document_id);
