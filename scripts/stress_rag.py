#!/usr/bin/env python3
"""Concurrent read-only RAG stress test with latency, throughput and retrieval diagnostics."""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_QUERIES = [
    "How does the Strategy pattern encapsulate interchangeable algorithms?",
    "How does the Observer pattern decouple publishers and subscribers?",
    "How do PostgreSQL transaction isolation levels work?",
    "Why are Python mutable default arguments dangerous?",
    "How does distributed consensus work?",
    "What is an Aggregate Root in Domain-Driven Design?",
    "What makes a REST operation idempotent?",
    "How do microservices handle resilience and partial failure?",
    "What retrieval metrics measure precision and recall in RAG?",
    "How does GraphRAG use knowledge graphs and entity relationships?",
    "How does transformer attention work?",
    "What are ports and adapters in hexagonal architecture?",
    "How do event sourcing and CQRS relate?",
    "What are TCP slow start and congestion avoidance?",
    "How does PostgreSQL MVCC interact with VACUUM?",
    "How does reranking improve RAG retrieval?",
]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * p))]


async def wait_ready(url: str, timeout_seconds: float) -> None:
    health_url = url.rsplit('/mcp', 1)[0] + '/api/health'
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(health_url)
                if response.status_code == 200 and response.json().get('status') == 'ok':
                    return
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(1)
    raise RuntimeError(f'target did not become ready within {timeout_seconds:.0f}s: {last_error}')


async def stress(args: argparse.Namespace) -> dict:
    await wait_ready(args.url, args.startup_timeout)
    queries = json.loads(Path(args.queries).read_text()) if args.queries else DEFAULT_QUERIES
    expanded = [queries[i % len(queries)] for i in range(args.requests)]
    semaphore = asyncio.Semaphore(args.concurrency)
    rows: list[dict] = []
    headers = {"Authorization": f"Bearer {args.bearer_token}"} if args.bearer_token else None

    async with streamablehttp_client(
        args.url,
        headers=headers,
        timeout=args.timeout,
        sse_read_timeout=max(args.timeout * 2, 60),
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            async def one(index: int, query: str) -> None:
                async with semaphore:
                    started = time.perf_counter()
                    try:
                        response = await session.call_tool("rag_search", {"query": query, "top_k": args.top_k})
                        wall_ms = (time.perf_counter() - started) * 1000
                        if response.isError:
                            rows.append({"index": index, "ok": False, "wall_ms": wall_ms, "error": str(response.content)})
                            return
                        data = response.structuredContent or json.loads(response.content[0].text)
                        timing = data.get("retrieval", {}).get("timings_ms", {})
                        rows.append({
                            "index": index,
                            "ok": True,
                            "wall_ms": wall_ms,
                            "trace_id": data.get("trace_id"),
                            "results": len(data.get("results") or []),
                            "lexical_ms": float(timing.get("lexical") or 0),
                            "dense_ms": float(timing.get("dense") or 0),
                            "embedding_ms": float(timing.get("embedding") or 0),
                            "rerank_ms": float(data.get("rerank", {}).get("duration_ms") or 0),
                        })
                    except Exception as exc:
                        rows.append({
                            "index": index,
                            "ok": False,
                            "wall_ms": (time.perf_counter() - started) * 1000,
                            "error": f"{type(exc).__name__}: {exc}",
                        })

            started = time.perf_counter()
            await asyncio.gather(*(one(i, q) for i, q in enumerate(expanded)))
            total_wall_ms = (time.perf_counter() - started) * 1000

    ok = [row for row in rows if row["ok"]]
    latency = [row["wall_ms"] for row in ok]
    summary = {
        "requests": len(rows),
        "success": len(ok),
        "errors": len(rows) - len(ok),
        "concurrency": args.concurrency,
        "total_wall_ms": round(total_wall_ms, 2),
        "throughput_rps": round(len(rows) / (total_wall_ms / 1000), 2) if total_wall_ms else 0,
        "latency_ms": {
            "avg": round(statistics.mean(latency), 2) if latency else 0,
            "p50": round(percentile(latency, 0.50), 2),
            "p95": round(percentile(latency, 0.95), 2),
            "p99": round(percentile(latency, 0.99), 2),
            "max": round(max(latency), 2) if latency else 0,
        },
        "trace_ids": sum(row.get("trace_id") is not None for row in ok),
        "lexical_triggered": sum(row.get("lexical_ms", 0) > 0 for row in ok),
        "zero_results": sum(row.get("results", 0) == 0 for row in ok),
        "avg_dense_ms": round(statistics.mean(row["dense_ms"] for row in ok), 2) if ok else 0,
        "avg_embedding_ms": round(statistics.mean(row["embedding_ms"] for row in ok), 2) if ok else 0,
        "avg_rerank_ms": round(statistics.mean(row["rerank_ms"] for row in ok), 2) if ok else 0,
    }
    output = {"summary": summary, "errors": [row for row in rows if not row["ok"]]}
    if args.output:
        Path(args.output).write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))
    if summary["errors"] or summary["zero_results"]:
        raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--requests", type=int, default=64)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--startup-timeout", type=float, default=60)
    parser.add_argument("--queries")
    parser.add_argument("--output")
    parser.add_argument("--bearer-token")
    asyncio.run(stress(parser.parse_args()))


if __name__ == "__main__":
    main()
