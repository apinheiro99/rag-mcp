# Install RAG MCP with an AI coding agent

This guide is designed to be handed directly to Claude Code, Codex, ChatGPT Work or another coding agent running on the target host.

## One-shot installation prompt

```text
You are installing RAG MCP from:

https://github.com/apinheiro99/rag-mcp

Before changing anything:
1. Read README.md.
2. Read docs/architecture.md.
3. Read .env.example.
4. Inspect docker-compose.yml and the install/start scripts.

Target architecture:
- public source code stays in the rag-mcp repository;
- PostgreSQL + pgvector provides persistence and vector retrieval;
- LM Studio provides the embedding/reranker model runtime;
- runtime/private state lives outside the repository under RAG_PRIVATE_PATH;
- the application exposes REST plus Streamable HTTP MCP at /mcp.

Rules:
- Do not redesign the project.
- Do not replace existing scripts unless an existing script is broken.
- Do not hardcode hostnames, ports, model names, limits or paths when configuration already exists.
- Do not commit .env files, credentials, uploaded documents, PostgreSQL files or runtime state.
- Do not disable TLS/security checks.
- Ask the user only for values that cannot be inferred safely.
- Validate each stage before continuing.

Execute:

A. Prerequisites
- Verify git, Docker Engine and Docker Compose.
- Verify the target host can reach the configured LM Studio endpoint.

B. Source
- Clone https://github.com/apinheiro99/rag-mcp
- cd rag-mcp
- inspect the current branch/release and repository status.

C. Configuration
- Copy .env.example to .env.
- Select/create a persistent RAG_PRIVATE_PATH outside the repository.
- Configure PostgreSQL credentials locally.
- Configure the LM Studio endpoint using the project's existing settings mechanism.
- Do not invent model names if the running LM Studio instance can be inspected.

D. Installation
- Run ./install.sh.
- Run ./start.sh.
- Use the repository Docker Compose topology as provided.

E. Validation
Validate all of the following:
- docker compose services are healthy;
- GET /api/health returns OK;
- PostgreSQL and pgvector are reachable;
- the configured embedding model is reachable;
- the configured reranker is reachable;
- the web UI responds;
- /mcp responds as the MCP endpoint;
- run unit/regression/integration tests where supported.

F. Functional smoke test
- ingest or use a small non-sensitive test document;
- run one retrieval query;
- verify results include source/document provenance;
- report effective retrieval behavior and any error.

G. Final report
Return a concise report with:
- installation directory;
- RAG_PRIVATE_PATH;
- service URLs;
- models in use;
- container/service health;
- test results;
- smoke-test result;
- remaining manual configuration.

Do not merely explain the steps. Carry them out.
```

## Optional ChatGPT access

RAG MCP exposes Streamable HTTP MCP at `/mcp`.

Production can optionally use OpenAI Secure MCP Tunnel without publishing the MCP service directly to the public internet. Tunnel credentials remain deployment-owned and outside the repository.
