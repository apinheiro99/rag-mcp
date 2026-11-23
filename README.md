# RAG MCP

**Self-hosted Retrieval-Augmented Generation platform.** RAG MCP ingests private knowledge, indexes it with local embeddings, retrieves source-traceable context through PostgreSQL/pgvector, and exposes the same knowledge to ChatGPT, Claude and other clients through REST and MCP.

> Status: **operational, under active development.** Ingestion, semantic retrieval, Qwen reranking, MCP access, collection routing, incremental directory sync, CI/CD publication and production deployment are operational.

## How it fits together

```text
ChatGPT / Claude / Clients
          |
      REST / MCP
          |
       RAG MCP
      /       \
Ingestion   Retrieval
    |          |
    +---- LM Studio
    |
PostgreSQL + pgvector
```

The architecture, model roles, ingestion lifecycle, retrieval/reranking pipeline, Mermaid diagrams, configuration, decisions, validation evidence and current engineering state live in one canonical document: [docs/architecture.md](docs/architecture.md).

## Key capabilities

- local embeddings with PostgreSQL/pgvector retrieval;
- configurable Qwen3 reranking with fail-open behavior;
- collection-wide default search with explicit collection override;
- source-traceable REST and read-only MCP search;
- LLM-assisted collection classification with configurable high-confidence auto-routing and review inbox;
- incremental directory sources: new/changed/deleted file synchronization;
- text encoding recovery before quality gating;
- GitLab CI validation, version tags, GitHub publication and production deployment.

## What RAG MCP is not

Not a generic file server, not a secrets manager, not an infrastructure control plane, and not a replacement for corpus governance. It retrieves the knowledge intentionally indexed into its collections.

## Startup DEV

```sh
./start.sh
```

- `./start.sh` — normal startup
- `./start.sh --build` — rebuild before startup
- `make start` — shortcut

The Web UI, REST API and MCP endpoint are exposed on `RAG_PORT` (default `8000`).

## Repository/private split

```text
rag/
├── rag-mcp/       # product source/configuration
└── rag-private/   # installation-private persistent state
```

Bootstrap:

```sh
cp .env.example .env
bash install.sh
```

For implementation details, operational invariants and current work, use [docs/architecture.md](docs/architecture.md).
