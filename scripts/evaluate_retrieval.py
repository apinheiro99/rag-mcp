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


def evaluate(results, expected, k):
    """Document-level metrics against known relevant sources, not exhaustive recall."""
    expected = {x.casefold() for x in expected}
    hits = set()
    first = 0
    for rank, row in enumerate(results[:k], 1):
        matches = {x for x in expected if x in row["document"].casefold()}
        if matches and not first:
            first = rank
        hits.update(matches)
    return {"known_source_recall": len(hits)/len(expected),
            "reciprocal_rank": 1/first if first else 0,
            "source_hit_at_k": bool(hits),
            "heading_only_count": sum(len(x["content"].split()) <= 5 for x in results[:k])}


async def main(args):
    cases = json.loads(Path(args.cases).read_text())
    rows = []
    async with streamablehttp_client(args.url) as (r,w,_):
        async with ClientSession(r,w) as session:
            await session.initialize()
            for case in cases:
                started = time.perf_counter()
                response = await session.call_tool("rag_search", {"query":case["query"], "top_k":args.k})
                data = response.structuredContent or json.loads(response.content[0].text)
                if response.isError:
                    raise RuntimeError(str(data))
                results = data["results"]
                row = {"id":case["id"], "ms":round((time.perf_counter()-started)*1000,2),
                       **evaluate(results,case["expected_sources"],args.k),
                       "results":[{key:x.get(key) for key in ("document","chunk_id","location_label","score","rerank_score")} for x in results]}
                rows.append(row)
                print(json.dumps(row,ensure_ascii=False),flush=True)
    summary = {key:statistics.mean(r[key] for r in rows) for key in ("ms","known_source_recall","reciprocal_rank","source_hit_at_k","heading_only_count")}
    output = {"k":args.k,"cases":rows,"summary":summary}
    Path(args.output).write_text(json.dumps(output,indent=2,ensure_ascii=False))
    print(json.dumps(summary),flush=True)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",required=True)
    parser.add_argument("--cases",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--k",type=int,default=5)
    asyncio.run(main(parser.parse_args()))
