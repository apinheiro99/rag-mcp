"""Reranking opcional de candidatos recuperados."""
from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any


class RerankError(Exception):
    pass


_QWEN_SYSTEM_PROMPT = (
    'Judge whether the Document meets the requirements based on the Query and the '
    'Instruct provided. Note that the answer can only be "yes" or "no".'
)


def select_rerank_strategy(model: str) -> str:
    normalized = (model or "").strip().lower()
    if "qwen3-reranker-8b" in normalized:
        return "qwen_rank_backend_required"
    if "qwen3-reranker" in normalized:
        return "qwen_binary"
    return "generic_json"


def parse_rerank_response(raw: str) -> dict[str, float]:
    text = (raw or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
    if fence:
        text = fence.group(1).strip()
    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RerankError(f"resposta de rerank não é JSON válido: {exc}") from exc
    if not isinstance(items, list):
        raise RerankError("resposta de rerank deve ser uma lista JSON")

    scores: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            raise RerankError("cada item de rerank deve ser um objeto")
        chunk_id = str(item.get("chunk_id") or item.get("id") or "").strip()
        if not chunk_id:
            raise RerankError("item de rerank sem chunk_id")
        try:
            score = float(item["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankError(f"score inválido para {chunk_id}") from exc
        if not 0.0 <= score <= 1.0:
            raise RerankError(f"score fora de 0..1 para {chunk_id}: {score}")
        scores[chunk_id] = score
    return scores


def _normalize_binary_token(token: str) -> str:
    return re.sub(r"[^a-z]", "", (token or "").strip().lower())


def qwen_yes_probability(payload: dict[str, Any]) -> float:
    """Approximate P(yes) from LM Studio top-logprobs for Qwen3-Reranker.

    The official model is a binary yes/no classifier. LM Studio exposes token
    logprobs rather than the raw two logits, so we aggregate case variants of
    yes/no when present. If only one class is visible in top_logprobs, the
    residual probability mass is used as the other class estimate.
    """
    try:
        choice = payload["choices"][0]
        content_rows = choice["logprobs"]["content"]
        row = content_rows[0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RerankError(f"resposta Qwen sem logprobs utilizáveis: {exc}") from exc

    top = row.get("top_logprobs") or []
    if not top:
        top = [{"token": row.get("token"), "logprob": row.get("logprob")}]

    yes_prob = 0.0
    no_prob = 0.0
    for item in top:
        token = _normalize_binary_token(str(item.get("token") or ""))
        try:
            prob = math.exp(float(item["logprob"]))
        except (KeyError, TypeError, ValueError):
            continue
        if token == "yes":
            yes_prob += prob
        elif token == "no":
            no_prob += prob

    yes_prob = min(max(yes_prob, 0.0), 1.0)
    no_prob = min(max(no_prob, 0.0), 1.0)

    if yes_prob > 0.0 and no_prob > 0.0:
        denom = yes_prob + no_prob
        return min(max(yes_prob / denom, 0.0), 1.0)
    if yes_prob > 0.0:
        return yes_prob
    if no_prob > 0.0:
        # top_logprobs is truncated; unseen residual mass is unknown, not evidence for "yes".
        return 0.0

    generated = _normalize_binary_token(
        str((choice.get("message") or {}).get("content") or row.get("token") or "")
    )
    if generated == "yes":
        return 1.0
    if generated == "no":
        return 0.0
    raise RerankError("resposta Qwen não contém probabilidade nem token yes/no")




def _build_prompt(query: str, results: list[dict[str, Any]], max_chars: int) -> str:
    rows = []
    for result in results:
        content = str(result.get("content") or "")[:max_chars]
        rows.append(f'chunk_id={result["chunk_id"]}\n{content}')
    return (
        "Você é um reranker de recuperação. Compare a pergunta com cada trecho. "
        "Responda SOMENTE uma lista JSON, sem explicação, no formato "
        '[{"chunk_id":"...","score":0.0}]. '
        "Use score entre 0 e 1, onde 1 significa que o trecho responde diretamente à pergunta.\n\n"
        f"PERGUNTA:\n{query}\n\nCANDIDATOS:\n\n" + "\n\n---\n\n".join(rows)
    )


def _qwen_messages(query: str, document: str, instruction: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _QWEN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"<Instruct>: {instruction}\n\n"
                f"<Query>: {query}\n\n"
                f"<Document>: {document}"
            ),
        },
    ]


def _rank_from_scores(
    results: list[dict[str, Any]],
    scores: dict[str, float],
) -> list[dict[str, Any]]:
    ranked = []
    for result in results:
        item = dict(result)
        item["rerank_score"] = round(float(scores.get(item["chunk_id"], 0.0)), 4)
        ranked.append(item)
    ranked.sort(key=lambda item: (item["rerank_score"], item["score"]), reverse=True)
    return ranked


async def _rerank_generic_json(
    query: str,
    results: list[dict[str, Any]],
    config: dict[str, Any],
    client: Any,
    model: str,
) -> list[dict[str, Any]]:
    max_chars = int(config.get("max_chars_per_candidate", 1800))
    prompt = _build_prompt(query, results, max_chars)
    raw = await client.chat(
        [{"role": "user", "content": prompt}],
        model,
        temperature=float(config.get("temperature", 0.0)),
        max_tokens=int(config.get("max_tokens", 1200)),
        timeout=float(config.get("timeout", 120.0)),
        reasoning_effort=str(config.get("reasoning_effort", "none")),
    )
    scores = parse_rerank_response(raw)
    return _rank_from_scores(results, scores)


async def _rerank_qwen_binary(
    query: str,
    results: list[dict[str, Any]],
    config: dict[str, Any],
    client: Any,
    model: str,
) -> list[dict[str, Any]]:
    max_chars = int(config.get("max_chars_per_candidate", 1800))
    instruction = str(
        config.get(
            "qwen_instruction",
            "Given a web search query, retrieve relevant passages that answer the query",
        )
    )
    parallelism = max(1, int(config.get("qwen_parallelism", 4)))
    top_logprobs = max(2, min(20, int(config.get("qwen_top_logprobs", 20))))
    timeout = float(config.get("timeout", 120.0))
    semaphore = asyncio.Semaphore(parallelism)

    async def score_one(result: dict[str, Any]) -> tuple[str, float]:
        document = str(result.get("content") or "")[:max_chars]
        async with semaphore:
            payload = await client.chat_completion(
                _qwen_messages(query, document, instruction),
                model,
                temperature=0.0,
                max_tokens=1,
                timeout=timeout,
                reasoning_effort="none",
                logprobs=True,
                top_logprobs=top_logprobs,
            )
        return str(result["chunk_id"]), qwen_yes_probability(payload)

    scored = await asyncio.gather(*(score_one(result) for result in results))
    return _rank_from_scores(results, dict(scored))




async def rerank_results(
    query: str,
    results: list[dict[str, Any]],
    config: dict[str, Any],
    client: Any,
) -> list[dict[str, Any]]:
    model = str(config.get("model") or "").strip()
    if not model:
        raise RerankError("modelo de rerank não configurado")
    if not results:
        return []

    strategy = select_rerank_strategy(model)
    if strategy == "qwen_rank_backend_required":
        raise RerankError(
            "qwen3-reranker-8b requer backend de rerank/RANK pooling; "
            "o LM Studio atual expõe este GGUF apenas como llm"
        )
    if strategy == "qwen_binary":
        return await _rerank_qwen_binary(query, results, config, client, model)
    return await _rerank_generic_json(query, results, config, client, model)

