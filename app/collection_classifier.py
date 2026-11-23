"""LLM-assisted collection routing for already uploaded documents.

The classifier never moves or creates anything by itself. It only returns a
suggestion with confidence and rationale; API/UI own the explicit apply step.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .lmstudio import LMStudioClient

SYSTEM_PROMPT = """You classify documents into an existing RAG library taxonomy.
Choose an existing collection only when the document clearly belongs there.
If none is a good semantic fit, propose one new collection.
Do not create broad catch-all buckets when a specific shelf is appropriate.
Return ONLY valid JSON with this shape:
{
  "existing_collection": "exact existing collection name or null",
  "confidence": 0.0,
  "proposed_collection": null or {"name": "short shelf name", "description": "one sentence"},
  "rationale": "short explanation"
}
Rules:
- existing_collection must exactly match one of the supplied names or be null.
- confidence is 0..1 and measures confidence in the routing decision.
- proposed_collection must be null when an existing collection is selected.
- prefer an existing specific collection over creating a near-duplicate.
"""


def parse_json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    try:
        value = json.loads(content)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = content.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth = 0
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                value = json.loads(content[start:i + 1])
                if not isinstance(value, dict):
                    raise ValueError("response JSON is not an object")
                return value
    raise ValueError("incomplete JSON object in response")


def validate_classification(data: dict[str, Any], collections: list[dict[str, Any]]) -> dict[str, Any]:
    names = {str(c["name"]) for c in collections}
    existing = data.get("existing_collection")
    if existing is not None:
        existing = str(existing).strip()
        if existing not in names:
            raise ValueError(f"unknown collection returned by model: {existing}")

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError) as e:
        raise ValueError("invalid confidence") from e
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")

    proposed = data.get("proposed_collection")
    if existing:
        proposed = None
    elif proposed is not None:
        if not isinstance(proposed, dict):
            raise ValueError("proposed_collection must be an object or null")
        name = str(proposed.get("name", "")).strip()
        description = str(proposed.get("description", "")).strip()
        if not name:
            raise ValueError("proposed collection name is empty")
        if name in names:
            existing, proposed = name, None
        else:
            proposed = {"name": name[:120], "description": description}

    if not existing and not proposed:
        raise ValueError("model returned neither existing nor proposed collection")

    return {
        "existing_collection": existing,
        "confidence": round(confidence, 4),
        "proposed_collection": proposed,
        "rationale": str(data.get("rationale", "")).strip(),
    }


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
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    shelves = [
        {
            "name": c["name"],
            "description": c.get("description") or "",
            "documents": int(c.get("doc_count") or 0),
        }
        for c in collections
    ]
    prompt = (
        f"EXISTING COLLECTIONS:\n{json.dumps(shelves, ensure_ascii=False, indent=2)}\n\n"
        f"DOCUMENT:\nfilename: {filename}\nsource_path: {source_path}\n\n"
        f"SAMPLE:\n{text[:sample_chars]}\n\nReturn only the requested JSON."
    )
    content = await client.chat(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        model,
        temperature=0.0,
        max_tokens=1000,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
    )
    result = validate_classification(parse_json_object(content), collections)
    result.update({"model": model, "source": "llm"})
    return result
