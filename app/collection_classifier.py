"""Embedding-based collection routing for already indexed documents."""
from __future__ import annotations

import json
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

def parse_category_proposal(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1:]
        if text.endswith(fence):
            text = text[:-3]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("category proposal must be a JSON object")
    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("category proposal name is missing or too long")
    words = name.split()
    if len(words) > 3:
        raise ValueError("category proposal must be a broad 1-3 word category")
    return {
        "name": name,
        "description": description or f"Books primarily about {name}.",
    }


async def propose_category_with_llm(
    client: LMStudioClient,
    model: str,
    *,
    filename: str,
    text: str,
    existing_categories: list[str],
    sample_chars: int,
    timeout: float,
) -> dict[str, str]:
    sample = representative_sample(text, sample_chars)
    existing = ", ".join(existing_categories) if existing_categories else "(none)"
    prompt = (
        "Classify this book into a broad reusable subject category. "
        "If none of the existing categories fits, propose a NEW category name. "
        "Use a short 1-3 word subject, not the book title, author, edition, or publisher. "
        "Examples: Software Testing, Economics, Nuclear Engineering, PostgreSQL. "
        "Return ONLY JSON with keys name and description.\\n\\n"
        f"Existing categories: {existing}\\n"
        f"Filename: {filename}\\n\\n"
        f"Book sample:\\n{sample}"
    )
    raw = await client.chat(
        [{"role": "user", "content": prompt}],
        model,
        temperature=0.0,
        max_tokens=180,
        timeout=timeout,
        reasoning_effort="none",
    )
    return parse_category_proposal(raw)


def parse_existing_category_choice(raw: str, allowed: set[str]) -> dict[str, Any]:
    text = (raw or "").strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1:]
        if text.endswith(fence):
            text = text[:-3]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("category choice must be a JSON object")
    category = data.get("category")
    if category is None:
        return {
            "category": None,
            "confidence": 0.0,
            "rationale": str(data.get("rationale") or "").strip(),
        }
    category = str(category).strip()
    if category not in allowed:
        raise ValueError("classifier returned a category outside the allowed catalog")
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    return {
        "category": category,
        "confidence": confidence,
        "rationale": str(data.get("rationale") or "").strip(),
    }


async def classify_existing_category_with_llm(
    client: LMStudioClient,
    model: str,
    *,
    filename: str,
    text: str,
    categories: list[dict[str, Any]],
    sample_chars: int,
    timeout: float,
) -> dict[str, Any]:
    allowed = {str(c["name"]) for c in categories}
    catalog = "\n".join(
        f"- {c['name']}: {str(c.get('description') or '').strip()}"
        for c in categories
    )
    sample = representative_sample(text, sample_chars)
    prompt = (
        "Choose the single best category for this document from the catalog below. "
        "You MUST choose only from the provided category names, or null if none fits. "
        "Do not invent, rename, broaden, or create a category. "
        "Return ONLY JSON with keys category, confidence, rationale.\n\n"
        f"Categories:\n{catalog}\n\n"
        f"Filename: {filename}\n\n"
        f"Document sample:\n{sample}"
    )
    raw = await client.chat(
        [{"role": "user", "content": prompt}],
        model,
        temperature=0.0,
        max_tokens=180,
        timeout=timeout,
        reasoning_effort="none",
    )
    return parse_existing_category_choice(raw, allowed)


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
