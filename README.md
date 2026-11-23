# RAG MCP

**Self-hosted Retrieval-Augmented Generation platform.** RAG MCP ingests private knowledge, indexes it with local embeddings, retrieves source-traceable context through PostgreSQL/pgvector, and exposes the same knowledge to ChatGPT, Claude and other clients through REST and MCP.

> Status: **active development.** Ingestion, semantic retrieval, MCP access, parser specialization and per-document cancellation are operational. Optional LLM reranking is implemented and is in final end-to-end validation.

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
