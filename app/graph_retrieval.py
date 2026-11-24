"""Experimental evidence graph: document sections and shared named concepts.

No generated facts or graph database required. Edges are co-occurrence, not
asserted semantic relations. This experiment is deliberately opt-in.
"""
from collections import defaultdict
import re

CONCEPT = re.compile(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+|[A-Z][A-Z0-9]{2,})\b")


def evidence_graph(rows, query, limit):
    nodes = {r["chunk_id"]: r for r in rows}
    inverted = defaultdict(set)
    for key, row in nodes.items():
        for term in set(CONCEPT.findall(row["content"])):
            inverted[term.casefold()].add(key)
    query_terms = {t.casefold() for t in CONCEPT.findall(query)}
    edges = []
    for concept, ids in sorted(inverted.items()):
        if len(ids) < 2:
            continue
        for a in sorted(ids):
            for b in sorted(ids):
                if a >= b or nodes[a]["document_id"] == nodes[b]["document_id"]:
                    continue
                edges.append({"source": a, "target": b, "concept": concept,
                              "type": "shared_mention", "query_match": concept in query_terms})
    edges.sort(key=lambda e: (not e["query_match"], e["concept"], e["source"], e["target"]))
    return {"experimental": True, "nodes": [{"chunk_id": k, "document": r["document"],
            "location": r["location"]} for k,r in nodes.items()], "edges": edges[:limit]}
