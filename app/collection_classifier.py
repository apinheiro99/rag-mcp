"""Embedding-based collection routing for already indexed documents."""
from __future__ import annotations

import math
import re
from typing import Any

from .lmstudio import LMStudioClient



TITLE_NOISE = {
    "a", "an", "and", "art", "as", "at", "automating", "building", "by",
    "edition", "for", "from", "guide", "handbook", "in", "introduction", "of",
    "on", "or", "practical", "revisited", "the", "to", "with", "welib", "org", "gl",
}


def suggest_category(filename: str, source_path: str) -> dict[str, str]:
    raw = (filename or source_path or "New Category").rsplit("/", 1)[-1]
    raw = re.sub(r"\.(?:pdf|epub|azw3|mobi|txt|html?|md|docx?)$", "", raw, flags=re.I)
    raw = re.sub(r"\([^)]*\)", " ", raw)
    raw = re.split(r"\s+--\s+|\s+-\s+|:\s+", raw, maxsplit=1)[0]
    words = re.findall(r"[A-Za-z0-9+#.]+", raw)
    meaningful = []
    for word in words:
        clean = word.strip(".")
        if not clean or clean.casefold() in TITLE_NOISE:
            continue
        if clean.casefold() not in {w.casefold() for w in meaningful}:
            meaningful.append(clean)
        if len(meaningful) >= 2:
            break
    name = " ".join(meaningful).strip() or "New Category"
    return {
        "name": name,
        "description": f"Documents primarily about {name}.",
    }

def representative_sample(text: str, sample_chars: int) -> str:
    text = (text or "").strip()
    sample_chars = max(300, int(sample_chars))
    if len(text) <= sample_chars:
        return text
    labels = ("[BEGIN]\n", "\n\n[MIDDLE]\n", "\n\n[END]\n")
    overhead = sum(len(x) for x in labels)
    budget = max(90, sample_chars - overhead)
    first_n = budget // 3
    middle_n = budget // 3
    end_n = budget - first_n - middle_n
    middle_start = max(0, (len(text) // 2) - (middle_n // 2))
    return (
        labels[0] + text[:first_n]
        + labels[1] + text[middle_start:middle_start + middle_n]
        + labels[2] + text[-end_n:]
    )


def collection_profile(collection: dict[str, Any]) -> str:
    parts = [f"Category: {collection['name']}"]
    description = str(collection.get("description") or "").strip()
    if description:
        parts.append(f"Scope: {description}")
    examples = [str(x).strip() for x in (collection.get("examples") or []) if str(x).strip()]
    if examples:
        parts.append("Representative documents: " + "; ".join(examples[:6]))
    return "\n".join(parts)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


async def classify_document(
    client: LMStudioClient,
    model: str,
    *,
    filename: str,
    source_path: str,
    text: str,
    collections: list[dict[str, Any]],
    sample_chars: int,
    timeout: float,
    match_threshold: float = 0.55,
    margin_threshold: float = 0.03,
    **_: Any,
) -> dict[str, Any]:
    if not collections:
        return {
            "existing_collection": None,
            "confidence": 0.0,
            "proposed_collection": suggest_category(filename, source_path),
            "new_collection_suggested": True,
            "rationale": "No existing user-defined categories are available.",
            "model": model,
            "source": "embedding",
        }

    doc_text = f"Document: {filename}\nSource: {source_path}\n\n" + representative_sample(text, sample_chars)
    profiles = [collection_profile(c) for c in collections]
    vectors = await client.embed([doc_text, *profiles], model)
    document_vector, category_vectors = vectors[0], vectors[1:]
    ranked = sorted(
        (
            {"name": str(collections[i]["name"]), "score": cosine_similarity(document_vector, category_vectors[i])}
            for i in range(len(collections))
        ),
        key=lambda x: (-x["score"], x["name"]),
    )
    best = ranked[0]
    second_score = ranked[1]["score"] if len(ranked) > 1 else -1.0
    margin = best["score"] - second_score
    matched = best["score"] >= float(match_threshold) and margin >= float(margin_threshold)

    return {
        "existing_collection": best["name"] if matched else None,
        "confidence": round(float(best["score"]), 4),
        "proposed_collection": None if matched else suggest_category(filename, source_path),
        "new_collection_suggested": not matched,
        "rationale": f"Best semantic category: {best['name']} (score={best['score']:.4f}, margin={margin:.4f}).",
        "model": model,
        "source": "embedding",
        "candidates": [
            {"collection": x["name"], "score": round(float(x["score"]), 4)}
            for x in ranked[:5]
        ],
    }
