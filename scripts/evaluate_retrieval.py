#!/usr/bin/env python3
"""Read-only retrieval evaluation via MCP; no collection or document filters."""
import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _expected_source_groups(expected):
    groups = []
    for item in expected:
        values = item if isinstance(item, list) else [item]
        group = {str(x).casefold() for x in values if str(x).strip()}
        if group:
            groups.append(group)
    return groups


def evaluate(results, expected, k):
    """Document-level metrics against known relevant source groups, not exhaustive recall.

    Each expected item may be one source substring or a list of acceptable alternatives
    for the same evidence need. This avoids treating an equally valid book as a miss.
    """
    groups = _expected_source_groups(expected)
    hits = set()
    first = 0
    for rank, row in enumerate(results[:k], 1):
        document = row["document"].casefold()
        matched = {i for i, group in enumerate(groups) if any(x in document for x in group)}
        if matched and not first:
            first = rank
        hits.update(matched)
    return {"known_source_recall": len(hits)/len(groups) if groups else 1.0,
            "reciprocal_rank": 1/first if first else 0,
            "source_hit_at_k": bool(hits),
            "heading_only_count": sum(len(x["content"].split()) <= 5 for x in results[:k])}


async def main(args):
    cases = json.loads(Path(args.cases).read_text())
    rows = []
    async with streamablehttp_client(args.url, headers=({"Authorization": f"Bearer {args.bearer_token}"} if args.bearer_token else None)) as (r,w,_):
        async with ClientSession(r,w) as session:
            await session.initialize()
            for warmup_round in range(args.warmup_rounds):
                warmup_started = time.perf_counter()
                for case in cases:
                    response = await session.call_tool(
                        "rag_search", {"query": case["query"], "top_k": args.k}
                    )
                    if response.isError:
                        data = response.structuredContent or json.loads(response.content[0].text)
                        raise RuntimeError(str(data))
                print(json.dumps({
                    "warmup_round": warmup_round + 1,
                    "cases": len(cases),
                    "ms": round((time.perf_counter() - warmup_started) * 1000, 2),
                }), flush=True)

            for case in cases:
                started = time.perf_counter()
                response = await session.call_tool("rag_search", {"query":case["query"], "top_k":args.k})
                data = response.structuredContent or json.loads(response.content[0].text)
                if response.isError:
                    raise RuntimeError(str(data))
                results = data["results"]
                retrieval = data.get("retrieval") or {}
                decomposition = retrieval.get("decomposition") or {}
                row = {"id":case["id"], "ms":round((time.perf_counter()-started)*1000,2),
                       "trace_id": data.get("trace_id"),
                       "effective_top_k": retrieval.get("effective_top_k", len(results)),
                       "hybrid": bool(retrieval.get("hybrid")),
                       "decomposition": bool(decomposition.get("enabled")),
                       "lexical_reason": retrieval.get("lexical_skipped_reason"),
                       **evaluate(results,case["expected_sources"],args.k),
                       "results":[{key:x.get(key) for key in ("document","chunk_id","location_label","score","rerank_score")} for x in results]}
                rows.append(row)
                print(json.dumps(row,ensure_ascii=False),flush=True)
    summary = {key:statistics.mean(r[key] for r in rows) for key in ("ms","known_source_recall","reciprocal_rank","source_hit_at_k","heading_only_count")}
    summary["avg_effective_top_k"] = statistics.mean(r["effective_top_k"] for r in rows)
    summary["hybrid_rate"] = statistics.mean(1.0 if r["hybrid"] else 0.0 for r in rows)
    summary["decomposition_rate"] = statistics.mean(1.0 if r["decomposition"] else 0.0 for r in rows)
    output = {"k":args.k,"cases":rows,"summary":summary}
    failures = []
    if summary["known_source_recall"] < args.min_recall:
        failures.append(f"known_source_recall {summary['known_source_recall']:.3f} < {args.min_recall:.3f}")
    if summary["reciprocal_rank"] < args.min_mrr:
        failures.append(f"reciprocal_rank {summary['reciprocal_rank']:.3f} < {args.min_mrr:.3f}")
    if summary["ms"] > args.max_avg_ms:
        failures.append(f"avg latency {summary['ms']:.1f}ms > {args.max_avg_ms:.1f}ms")
    if any(row["trace_id"] is None for row in rows):
        failures.append("one or more retrievals did not return trace_id")
    output["gates"] = {
        "min_recall": args.min_recall, "min_mrr": args.min_mrr,
        "max_avg_ms": args.max_avg_ms, "passed": not failures, "failures": failures,
    }
    Path(args.output).write_text(json.dumps(output,indent=2,ensure_ascii=False))
    print(json.dumps({"summary": summary, "gates": output["gates"]}),flush=True)
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",required=True)
    parser.add_argument("--cases",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--k",type=int,default=5)
    parser.add_argument("--min-recall",type=float,default=0.80)
    parser.add_argument("--min-mrr",type=float,default=0.60)
    parser.add_argument("--max-avg-ms",type=float,default=1500.0)
    parser.add_argument("--warmup-rounds",type=int,default=0)
    parser.add_argument("--bearer-token",default=None)
    asyncio.run(main(parser.parse_args()))
