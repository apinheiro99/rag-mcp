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
- Verify LM Studio is installed and the target host can reach its local-server endpoint.
- If LM Studio is missing, install it using the current official method for the target OS when that can be done safely from the host; if a GUI/interactive approval is required, tell the user the single exact action needed, then continue automatically after it is available.
- Query `<LMSTUDIO_URL>/v1/models`.
- If the reference models are not available, instruct the user to download/load them in LM Studio Discover before continuing:
  - `Qwen3-Embedding-8B` → reference runtime ID `text-embedding-qwen3-embedding-8b`;
  - `Qwen3-Reranker-4B` → reference runtime ID `qwen3-reranker-4b`.
- Prefer a 4-bit quantization or better when hardware permits. Do not hardcode a particular community GGUF filename.
- Do not substitute another embedding dimension without explicitly planning a reindex.

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

## LM Studio reference profile

Reference deployment:

```text
Embedding model: Qwen3-Embedding-8B
LM Studio ID:    text-embedding-qwen3-embedding-8b
Dimension:       4096

Reranker model:  Qwen3-Reranker-4B
LM Studio ID:    qwen3-reranker-4b
Mode:            resident
```

Verify from the host that runs RAG MCP:

```bash
curl -s http://<lm-studio-host>:1234/v1/models
```

If the IDs differ from the reference IDs, use the IDs returned by LM Studio in RAG MCP Settings. The model family/role matters; the string alias itself is not an architectural invariant.

## Connect Claude Code to the installed RAG MCP

Installing the service and connecting an MCP client are separate steps. If Claude Code runs on a host that can reach the RAG server, add it directly as a Streamable HTTP MCP server:

```bash
claude mcp add --transport http rag-mcp http://docker-prod01.lab:8000/mcp
claude mcp list
```

Inside Claude Code, run `/mcp` to inspect connection/authentication status. If `RAG_API_TOKEN` protects `/mcp`, configure the matching `Authorization: Bearer ...` header in Claude Code's **local** MCP configuration; do not commit credentials to a shared `.mcp.json`.

If Claude Code is not on a network that can reach the RAG host, provide a separate secure network path (for example VPN/WireGuard or an authenticated public HTTPS MCP endpoint). OpenAI Secure MCP Tunnel cannot be used as a generic Claude Code tunnel.

## Connect ChatGPT / OpenAI to the private RAG MCP

RAG MCP already includes optional OpenAI Secure MCP Tunnel deployment support:

- `deploy/tunnel.compose.yml`;
- `scripts/deploy-prod.sh` detects `RAG_TUNNEL_BINARY` + `RAG_TUNNEL_DIR`;
- the sidecar targets the private MCP endpoint `http://app:8000/mcp`;
- deploy readiness fails if an enabled tunnel sidecar cannot establish control-plane polling.

The repository intentionally does **not** contain tunnel credentials. The operator still needs to:

1. Create/manage the tunnel in OpenAI Platform tunnel settings.
2. Associate the intended Platform organization and ChatGPT workspace.
3. Provision a restricted runtime API key for `tunnel-client`.
4. Install/download the supported `tunnel-client` binary on the deployment host.
5. Generate/validate the local profile with `tunnel-client init` / `tunnel-client doctor`, targeting `http://app:8000/mcp`.
6. Store the resulting config/credentials outside Git and expose only the paths through `RAG_TUNNEL_BINARY` and `RAG_TUNNEL_DIR`.
7. Redeploy; confirm the `rag-mcp-tunnel` sidecar is healthy and polling.
8. In ChatGPT, add a custom MCP server with **Connection: Tunnel**, select the same tunnel, and test `list_collections` / `rag_search`.

For Responses API or Codex flows, use the resulting `tunnel_id` in the MCP tool definition rather than trying to pass the private RAG URL as `server_url`.

Current OpenAI documentation:

- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- https://developers.openai.com/api/docs/guides/tools-connectors-mcp

## About “Sign in with ChatGPT”

This is a different integration. It allows an eligible open-source application to authenticate a user with ChatGPT and call OpenAI's Responses API using the user's ChatGPT plan. It does **not** create an MCP path back into the application. RAG MCP currently does not need this provider because its inference runtime is LM Studio; MCP/Tunnel is the correct mechanism for ChatGPT to access the RAG.
