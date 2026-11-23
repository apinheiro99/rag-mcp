"""RAG Server — ponto de entrada.

Sobe no mesmo processo/porta:
  /            interface web administrativa
  /api/...     API REST
  /mcp         MCP Server (Streamable HTTP) para Claude Code & cia.
  /docs        documentação OpenAPI interativa
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .config import env
from .db import db
from .ingest import worker
from .mcp_server import create_mcp
from .settings import settings

logging.basicConfig(
    level=getattr(logging, env.log_level, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("rag.main")

STATIC_DIR = Path(__file__).parent / "static"

mcp = create_mcp() if env.mcp_enabled else None
mcp_app = mcp.streamable_http_app() if mcp else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    env.data_dir.mkdir(parents=True, exist_ok=True)
    env.blobs_dir.mkdir(parents=True, exist_ok=True)
    env.extracted_dir.mkdir(parents=True, exist_ok=True)
    await db.start()
    await settings.load()
    await worker.start()
    await db.event("Servidor RAG iniciado")
    log.info("RAG server pronto em %s:%s (MCP: %s)", env.app_host, env.app_port,
             "/mcp" if mcp else "desabilitado")
    if mcp:
        async with mcp.session_manager.run():
            yield
    else:
        yield
    await worker.stop()
    await db.stop()


app = FastAPI(title="RAG Server", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id"],
)

OPEN_PATHS = {"/", "/favicon.ico", "/api/health", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if env.api_token:
        path = request.url.path
        protected = (path.startswith("/api") or path.startswith("/mcp")) and path not in OPEN_PATHS
        if protected and request.method != "OPTIONS":
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {env.api_token}":
                return JSONResponse({"detail": "token de API inválido ou ausente"},
                                    status_code=401)
    response = await call_next(request)
    if env.dev_reload and (request.url.path == "/" or request.url.path.startswith("/static/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


app.include_router(api_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


# MCP montado por último: o sub-app atende exatamente /mcp;
# as rotas acima têm precedência para todo o resto.
if mcp_app:
    app.mount("/", mcp_app)


def run() -> None:
    import uvicorn
    uvicorn.run(app, host=env.app_host, port=env.app_port, log_level=env.log_level.lower())


if __name__ == "__main__":
    run()
