"""MCP Server (Streamable HTTP) — consulta RAG sobre o conteúdo indexado.

Ferramentas (somente leitura; administração é exclusiva da interface web):
- rag_search: busca semântica com referência completa da fonte.
- list_collections: coleções disponíveis para direcionar a busca.
- get_document: texto integral (truncado) de um documento específico.

Montado em /mcp no mesmo processo/porta da interface web.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import env
from .db import db
from .extraction import ExtractionError, extract
from .ingest import extraction_cache_path
from .lmstudio import LMStudioError
from .retrieval_observability import retrieval_metrics
from .search import SearchError, semantic_search
from .search_filters import SearchFilters

log = logging.getLogger("rag.mcp")

INSTRUCTIONS = """Servidor RAG self-hosted. Use rag_search para buscar conhecimento
nas bases indexadas. Por padrão, NUNCA filtre por coleção: omita `collections` e
pesquise todas as coleções marcadas para busca padrão. Só use `collections` quando
o usuário pedir explicitamente uma coleção/base específica; esse pedido explícito
funciona mesmo se a coleção estiver fora da busca padrão. Cada resultado traz o
trecho (content), score de relevância, documento/caminho de origem, coleção,
localização e metadados — cite a fonte ao responder. Erros de coleção/filtro são
erros da ferramenta, não resultados vazios. Use get_document quando o trecho não
for suficiente."""


def create_mcp() -> FastMCP:
    if env.mcp_allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=env.mcp_allowed_hosts,
            allowed_origins=[f"http://{h}" for h in env.mcp_allowed_hosts]
                            + [f"https://{h}" for h in env.mcp_allowed_hosts],
        )
    else:
        # Servidor LAN self-hosted: sem proteção DNS-rebinding por padrão.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp = FastMCP(
        env.mcp_name,
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )

    @mcp.tool()
    async def rag_search(
        query: str,
        collections: list[str] | None = None,
        document_ids: list[str] | None = None,
        authors: list[str] | None = None,
        source_paths: list[str] | None = None,
        chapters: list[str] | None = None,
        pages: list[int] | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> dict[str, Any]:
        """Busca semântica (RAG) com filtros opcionais de fonte.

        Args:
            query: pergunta ou texto a buscar (linguagem natural).
            collections: nomes/ids das coleções SOMENTE quando o usuário pedir explicitamente.
                Omita para pesquisar todas as coleções habilitadas para busca padrão.
            document_ids: restringe a documentos específicos por UUID.
            authors: restringe por autor (match parcial, case-insensitive).
            source_paths: restringe ao caminho original exato.
            chapters: restringe ao capítulo exato salvo no chunk.
            pages: restringe a páginas específicas de PDF.
            top_k: número de trechos retornados.
            min_score: relevância mínima 0..1 (cosseno).

        Returns:
            results: trechos com score e rastreabilidade da fonte.
        """
        try:
            filters = SearchFilters(
                document_ids=document_ids or [],
                authors=authors or [],
                source_paths=source_paths or [],
                chapters=chapters or [],
                pages=pages or [],
            )
            return await semantic_search(query, collections, top_k, min_score, filters)
        except SearchError as e:
            raise ValueError(str(e)) from e
        except LMStudioError as e:
            raise RuntimeError(f"LM Studio indisponível no servidor RAG: {e}") from e

    @mcp.tool()
    async def rag_search_metrics(hours: int = 24, recent_limit: int = 20) -> dict[str, Any]:
        """Resumo de observabilidade das buscas RAG recentes."""
        return await retrieval_metrics(hours, recent_limit)

    @mcp.tool()
    async def list_collections() -> dict[str, Any]:
        """Lista as coleções/bases de conhecimento disponíveis no servidor RAG,
        com contagem de documentos e chunks indexados."""
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT c.name, c.description, c.embedding_model, c.default_search,
                    (SELECT count(*) FROM documents d WHERE d.collection_id=c.id AND d.status='ready') AS documents,
                    (SELECT coalesce(sum(d.chunks_total),0) FROM documents d
                      WHERE d.collection_id=c.id AND d.status='ready') AS chunks
                   FROM collections c ORDER BY c.name""")
            rows = await cur.fetchall()
        return {"collections": rows}

    @mcp.tool()
    async def get_document(document_id: str, max_chars: int = 20000) -> dict[str, Any]:
        """Retorna o texto extraído de um documento específico (até max_chars).

        Args:
            document_id: id retornado em um resultado de rag_search.
            max_chars: limite de caracteres do texto (padrão 20000, máx 200000).
        """
        max_chars = max(1, min(int(max_chars), 200000))
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT d.*, c.name AS collection FROM documents d
                   JOIN collections c ON c.id=d.collection_id WHERE d.id::text=%s""",
                (document_id,))
            doc = await cur.fetchone()
        if not doc:
            return {"error": f"documento {document_id} não encontrado"}
        text = None
        cache = extraction_cache_path(str(doc["id"]))
        if cache.exists():
            try:
                from .extraction import ExtractedDoc
                data = json.loads(cache.read_text(encoding="utf-8"))
                if data.get("sha256") == doc["content_sha256"]:
                    text = ExtractedDoc.from_json(data).full_text()
            except Exception:
                text = None
        if text is None:
            try:
                extracted = extract(env.data_dir / doc["stored_path"], doc["filename"])
                text = extracted.full_text()
            except ExtractionError as e:
                return {"error": f"não foi possível extrair o documento: {e}"}
        return {
            "document_id": str(doc["id"]),
            "document": doc["filename"],
            "source_path": doc["source_path"],
            "collection": doc["collection"],
            "metadata": doc["meta"],
            "status": doc["status"],
            "total_chars": len(text),
            "truncated": len(text) > max_chars,
            "content": text[:max_chars],
        }

    return mcp
