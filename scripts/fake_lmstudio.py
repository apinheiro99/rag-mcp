"""LM Studio simulado (OpenAI-compatible) — para testes e validação E2E.

NÃO faz parte do runtime do produto: é um dublê determinístico usado pelos
testes automatizados e pelo script de validação quando você não tem um
LM Studio real à mão. Embeddings são vetores hashing de bag-of-words
(textos parecidos → vetores parecidos), suficientes para validar o pipeline.

Uso:
    python scripts/fake_lmstudio.py --port 1234 --dim 64

Controle de falhas (usado nos testes de retomada):
    POST /_control/fail_next {"n": 3}     → próximas n chamadas de embeddings dão 503
    POST /_control/down {"down": true}    → servidor "cai" (503 em tudo)
    GET  /_control/stats                  → contadores de chamadas
    POST /_control/reset                  → zera contadores/falhas
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Fake LM Studio")
STATE = {"dim": 64, "fail_next": 0, "down": False,
         "embed_calls": 0, "embedded_texts": 0, "chat_calls": 0}


STOPWORDS = set("""a o e é de do da dos das em um uma uns umas para por com sem sob sobre
qual quais que quem quando onde como porque pois se na no nas nos ao aos à às ou mas
the of and to in is it for on with as at by be this that these those are was were
""".split())


def embed_text(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    words = [w for w in re.findall(r"[a-záàâãéêíóôõúüç0-9]+", text.lower())
             if w not in STOPWORDS and len(w) > 1]
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    for w, n in counts.items():
        weight = 1.0 + math.log(n)
        h = int.from_bytes(hashlib.sha256(w.encode()).digest()[:8], "big")
        vec[h % dim] += weight
        vec[(h >> 16) % dim] += weight * 0.5
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


@app.get("/api/v0/models")
async def native_models():
    return {"data": [
        {"id": "fake-embedding-model", "type": "embeddings", "state": "loaded",
         "arch": "hash-bow", "publisher": "test", "max_context_length": 8192},
        {"id": "fake-llm-model", "type": "llm", "state": "loaded",
         "arch": "rule-based", "publisher": "test", "max_context_length": 32768},
    ]}


@app.get("/v1/models")
async def openai_models():
    return {"object": "list", "data": [
        {"id": "fake-embedding-model", "object": "model"},
        {"id": "fake-llm-model", "object": "model"},
    ]}


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    if STATE["down"] or STATE["fail_next"] > 0:
        if STATE["fail_next"] > 0:
            STATE["fail_next"] -= 1
        return JSONResponse({"error": "injected failure"}, status_code=503)
    body = await request.json()
    texts = body.get("input")
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, list) or not texts:
        return JSONResponse({"error": "input inválido"}, status_code=400)
    STATE["embed_calls"] += 1
    STATE["embedded_texts"] += len(texts)
    return {"object": "list", "model": body.get("model"),
            "data": [{"object": "embedding", "index": i,
                      "embedding": embed_text(t, STATE["dim"])} for i, t in enumerate(texts)],
            "usage": {"prompt_tokens": sum(len(t) // 4 for t in texts)}}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    if STATE["down"]:
        return JSONResponse({"error": "injected failure"}, status_code=503)
    STATE["chat_calls"] += 1
    body = await request.json()
    prompt = json.dumps(body.get("messages", []), ensure_ascii=False)
    if '"has_pages": true' in prompt:
        cfg = {"strategy": "pages", "chunk_size": 1400, "chunk_overlap": 140,
               "min_chunk_size": 100,
               "rationale": "PDF com páginas claras: estratégia pages preserva a referência de página."}
    elif '"markdown_headings_in_sample"' in prompt and re.search(r'"markdown_headings_in_sample": [3-9]', prompt):
        cfg = {"strategy": "markdown", "chunk_size": 1500, "chunk_overlap": 180,
               "min_chunk_size": 120,
               "rationale": "Documento bem estruturado por seções: chunking markdown mantém contexto."}
    else:
        cfg = {"strategy": "recursive", "chunk_size": 1200, "chunk_overlap": 150,
               "min_chunk_size": 100,
               "rationale": "Prosa contínua: recursive com overlap de ~12% equilibra contexto e precisão."}
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": "```json\n" + json.dumps(cfg, ensure_ascii=False) + "\n```"}}],
            "model": body.get("model")}


@app.post("/_control/fail_next")
async def fail_next(request: Request):
    STATE["fail_next"] = int((await request.json()).get("n", 0))
    return {"fail_next": STATE["fail_next"]}


@app.post("/_control/down")
async def down(request: Request):
    STATE["down"] = bool((await request.json()).get("down", True))
    return {"down": STATE["down"]}


@app.get("/_control/stats")
async def stats():
    return dict(STATE)


@app.post("/_control/reset")
async def reset():
    STATE.update(fail_next=0, down=False, embed_calls=0, embedded_texts=0, chat_calls=0)
    return dict(STATE)


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=1234)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--dim", type=int, default=64)
    args = parser.parse_args()
    STATE["dim"] = args.dim
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
