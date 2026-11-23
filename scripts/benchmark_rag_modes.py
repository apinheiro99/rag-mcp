#!/usr/bin/env python3
"""Resident reranker benchmark through the RAG API only.

Protocol:
- discover current embedding model and current control reranker from RAG settings
- discover downloaded Qwen3 rerankers from LM Studio
- for each model: keep embedding + that reranker resident, unload other rerankers
- configure reranker_model through /api/settings
- run 2 warm-ups + 5 identical measured questions through /api/search
- write RAW JSONL only; no statistics or conclusions
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request


WARMUPS = [
    {"id": "warmup01", "query": "What is a replicated log?", "collections": ["distributed-systems-infra"]},
    {"id": "warmup02", "query": "What is a software architecture view?", "collections": ["software-engineering"]},
]

QUESTIONS = [
    {
        "id": "q01",
        "query": "Why can a circuit breaker reduce the risk of cascading failures better than retries alone?",
        "collections": ["distributed-systems-infra"],
    },
    {
        "id": "q02",
        "query": "What trade-offs should an architect consider when deciding whether to split a service into smaller services?",
        "collections": ["software-engineering", "distributed-systems-infra"],
    },
    {
        "id": "q03",
        "query": "How does the Strategy pattern improve extensibility compared with conditionals that select an algorithm?",
        "collections": ["software-engineering", "backend-python-node-typescript"],
    },
    {
        "id": "q04",
        "query": "Why are multiple architectural views useful, and how do stakeholder concerns influence which views should be documented?",
        "collections": ["software-engineering"],
    },
    {
        "id": "q05",
        "query": "In a production RAG system, what is the difference between retrieval precision and recall, and why do both matter?",
        "collections": ["rag-ai-agents", "software-engineering"],
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def http_json(method: str, url: str, payload=None, timeout=60) -> dict:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=body, headers=headers, method=method)
    started = utc_now()
    t0 = time.perf_counter_ns()
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return {
                "ok": True,
                "http_status": resp.status,
                "started_at": started,
                "elapsed_ns": time.perf_counter_ns() - t0,
                "response": json.loads(raw.decode("utf-8")),
                "error": None,
            }
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return {
            "ok": False,
            "http_status": exc.code,
            "started_at": started,
            "elapsed_ns": time.perf_counter_ns() - t0,
            "response": parsed,
            "error": f"HTTPError: {exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "http_status": None,
            "started_at": started,
            "elapsed_ns": time.perf_counter_ns() - t0,
            "response": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()


def get_settings(base_url: str) -> dict:
    result = http_json("GET", f"{base_url}/api/settings", timeout=10)
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return result["response"]


def model_snapshot(lmstudio_url: str) -> dict:
    return http_json("GET", f"{lmstudio_url}/api/v1/models", timeout=10)


def models(snapshot: dict) -> list[dict]:
    body = snapshot.get("response")
    return body.get("models", []) if isinstance(body, dict) else []


def loaded_instance_ids(snapshot: dict, model_key: str) -> list[str]:
    for item in models(snapshot):
        if item.get("key") == model_key:
            return [
                str(inst["id"])
                for inst in (item.get("loaded_instances") or [])
                if inst.get("id")
            ]
    return []


def ensure_loaded(lmstudio_url: str, model_key: str, context_length: int) -> dict:
    before = model_snapshot(lmstudio_url)
    ids = loaded_instance_ids(before, model_key)
    if ids:
        return {"already_loaded": True, "model": model_key, "instance_ids": ids}
    op = http_json(
        "POST",
        f"{lmstudio_url}/api/v1/models/load",
        {"model": model_key, "context_length": context_length},
        timeout=180,
    )
    return {"already_loaded": False, "model": model_key, "operation": op}


def ensure_unloaded(lmstudio_url: str, model_key: str) -> dict:
    before = model_snapshot(lmstudio_url)
    ids = loaded_instance_ids(before, model_key)
    ops = [
        http_json(
            "POST",
            f"{lmstudio_url}/api/v1/models/unload",
            {"instance_id": instance_id},
            timeout=60,
        )
        for instance_id in ids
    ]
    return {"model": model_key, "instance_ids_before": ids, "operations": ops}


def discover_qwen_rerankers(lmstudio_url: str) -> list[str]:
    snap = model_snapshot(lmstudio_url)
    if not snap["ok"]:
        raise RuntimeError(snap["error"])
    found = []
    for item in models(snap):
        key = str(item.get("key") or "")
        if item.get("type") == "llm" and "qwen3-reranker" in key.lower():
            found.append((int(item.get("size_bytes") or 0), key))
    return [key for _, key in sorted(found)]


def configure_model(base_url: str, model: str) -> dict:
    return http_json(
        "PUT",
        f"{base_url}/api/settings",
        {
            "reranker_model": model,
            "overrides": {
                "rerank": {
                    "enabled": True,
                    "mode": "resident",
                }
            },
        },
        timeout=10,
    )


def run_query(base_url: str, output: Path, model: str, item: dict, warmup: bool, timeout: int) -> None:
    payload = {
        "query": item["query"],
        "collections": item["collections"],
        "top_k": 5,
    }
    result = http_json("POST", f"{base_url}/api/search", payload, timeout=timeout)
    append_jsonl(
        output,
        {
            "record_type": "query",
            "timestamp": utc_now(),
            "model": model,
            "warmup": warmup,
            "question_id": item["id"],
            "request": payload,
            "observation": result,
        },
    )
    elapsed = result["elapsed_ns"] / 1_000_000_000
    rerank = (result.get("response") or {}).get("rerank") if result["ok"] else None
    label = "WARMUP" if warmup else item["id"]
    print(f"[{model}] {label} -> {'OK' if result['ok'] else 'ERRO'} | {elapsed:.3f}s | rerank={rerank}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--output-dir", default="../temp/benchmark")
    args = parser.parse_args()

    settings = get_settings(args.base_url)
    raw = settings.get("settings", {})
    effective = settings.get("effective", {})
    lmstudio_url = str(raw["lmstudio_url"]).rstrip("/")
    embedding_model = str(raw["embedding_model"])
    control_model = str(raw["reranker_model"])
    context_length = int(effective.get("rerank", {}).get("context_length", 8192))

    qwen_models = discover_qwen_rerankers(lmstudio_url)
    test_models = [control_model] + [m for m in qwen_models if m != control_model]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"rag_reranker_benchmark_raw_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

    append_jsonl(
        output,
        {
            "record_type": "benchmark_start",
            "timestamp": utc_now(),
            "embedding_model": embedding_model,
            "control_model": control_model,
            "test_models": test_models,
            "warmups": WARMUPS,
            "questions": QUESTIONS,
            "initial_settings": settings,
            "initial_model_snapshot": model_snapshot(lmstudio_url),
        },
    )

    print("RAG RERANKER BENCHMARK — API DO RAG", flush=True)
    print(f"Embedding: {embedding_model}", flush=True)
    print(f"Models: {test_models}", flush=True)
    print(f"Output: {output}", flush=True)

    for model in test_models:
        print(f"\n=== {model} ===", flush=True)
        residency = []
        for other in test_models:
            if other != model:
                residency.append(ensure_unloaded(lmstudio_url, other))
        residency.append(ensure_loaded(lmstudio_url, embedding_model, context_length))
        residency.append(ensure_loaded(lmstudio_url, model, context_length))

        config = configure_model(args.base_url, model)
        append_jsonl(
            output,
            {
                "record_type": "model_start",
                "timestamp": utc_now(),
                "model": model,
                "configuration": config,
                "residency": residency,
                "model_snapshot": model_snapshot(lmstudio_url),
            },
        )

        for item in WARMUPS:
            run_query(args.base_url, output, model, item, True, args.timeout)

        for item in QUESTIONS:
            run_query(args.base_url, output, model, item, False, args.timeout)

        append_jsonl(
            output,
            {
                "record_type": "model_end",
                "timestamp": utc_now(),
                "model": model,
                "model_snapshot": model_snapshot(lmstudio_url),
            },
        )

    append_jsonl(
        output,
        {
            "record_type": "benchmark_end",
            "timestamp": utc_now(),
            "final_settings": get_settings(args.base_url),
            "final_model_snapshot": model_snapshot(lmstudio_url),
        },
    )

    print("\nBENCHMARK ACABOU", flush=True)
    print(f"ARQUIVO BRUTO: {output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

