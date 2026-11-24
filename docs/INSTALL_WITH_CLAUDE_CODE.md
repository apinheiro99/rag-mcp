# Install with Claude Code

Paste this prompt into Claude Code on the target host:

> Install RAG Server from the public `rag-mcp` repository using Docker Compose. Read the repository README and `docs/architecture.md` first. Create a local `.env` from `.env.example`, ask me only for values that cannot be inferred safely, keep runtime data outside the public repository, start PostgreSQL + pgvector and the RAG application, then validate `/api/health`, the web UI, and the Streamable HTTP MCP endpoint at `/mcp`. Do not invent deployment defaults that are already configurable in the repository.

For development, use `./install.sh` followed by `./start.sh`. For a managed production host, keep the environment/private runtime material in a separate private directory/repository.

## Optional private ChatGPT connection with Secure MCP Tunnel

RAG MCP already exposes Streamable HTTP at `/mcp`. Production can optionally run OpenAI `tunnel-client` as a Compose sidecar without exposing that endpoint publicly.

1. Create a Secure MCP Tunnel in OpenAI Platform and associate it with the intended ChatGPT workspace/organization.
2. Provision the deployment-owned tunnel inputs once on the production host: the `tunnel-client` binary and a tunnel directory containing `tunnel-client.yaml` plus its credential file. Configure the MCP target as `http://app:8000/mcp`.
3. Production exports `RAG_TUNNEL_BINARY=$HOME/rag-prod/bin/tunnel-client` and `RAG_TUNNEL_DIR=$HOME/rag-prod/tunnel`; the public repository does not provision or inspect credential contents.
4. Redeploy. `scripts/deploy-prod.sh` adds `deploy/tunnel.compose.yml` when those host paths are provided and checks tunnel readiness after application health.
5. In ChatGPT Plugins, add a custom MCP server, choose **Tunnel**, select/paste the matching `tunnel_id`, then create it as a private plugin.
