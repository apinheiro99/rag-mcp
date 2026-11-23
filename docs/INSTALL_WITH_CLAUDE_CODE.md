# Install with Claude Code

Paste this prompt into Claude Code on the target host:

> Install RAG Server from the public `rag-mcp` repository using Docker Compose. Read the repository README and `docs/architecture.md` first. Create a local `.env` from `.env.example`, ask me only for values that cannot be inferred safely, keep runtime data outside the public repository, start PostgreSQL + pgvector and the RAG application, then validate `/api/health`, the web UI, and the Streamable HTTP MCP endpoint at `/mcp`. Do not invent deployment defaults that are already configurable in the repository.

For development, use `./install.sh` followed by `./start.sh`. For a managed production host, keep the environment/private runtime material in a separate private directory/repository.

## Optional private ChatGPT connection with Secure MCP Tunnel

RAG MCP already exposes Streamable HTTP at `/mcp`. Production can optionally run OpenAI `tunnel-client` as a Compose sidecar without exposing that endpoint publicly.

1. Create a Secure MCP Tunnel in OpenAI Platform and associate it with the intended ChatGPT workspace/organization.
2. Prepare a private tunnel directory containing `tunnel-client.yaml` and the credentials produced by the tunnel onboarding flow. Configure the MCP target as `http://app:8000/mcp` because the tunnel sidecar shares the RAG Compose network.
3. Set `RAG_TUNNEL_BINARY` and `RAG_TUNNEL_DIR` in the production private `.env`. Leaving both unset keeps the tunnel disabled.
4. Redeploy. `scripts/deploy-prod.sh` adds `deploy/tunnel.compose.yml` only when the two tunnel inputs are configured.
5. In ChatGPT Plugins, add a custom MCP server, choose **Tunnel**, select/paste the matching `tunnel_id`, then create it as a private plugin.
