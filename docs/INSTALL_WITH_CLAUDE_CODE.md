# Install with Claude Code

Paste this prompt into Claude Code on the target host:

> Install RAG Server from the public `rag-mcp` repository using Docker Compose. Read the repository README and `docs/architecture.md` first. Create a local `.env` from `.env.example`, ask me only for values that cannot be inferred safely, keep runtime data outside the public repository, start PostgreSQL + pgvector and the RAG application, then validate `/api/health`, the web UI, and the Streamable HTTP MCP endpoint at `/mcp`. Do not invent deployment defaults that are already configurable in the repository.

For development, use `./install.sh` followed by `./start.sh`. For a managed production host, keep the environment/private runtime material in a separate private directory/repository.
