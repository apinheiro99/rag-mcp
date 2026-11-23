#!/usr/bin/env python3
"""Validação end-to-end do RAG Server — os 13 passos do critério de conclusão.

Roda somente com a stdlib do Python 3.10+ (nenhum pip install necessário).

Uso típico (stack Docker no ar):
    python3 scripts/e2e_check.py --url http://localhost:8000 \
        --lm-url http://192.168.1.50:1234 \
        --restart-cmd "docker compose restart app"

Com LM Studio simulado (validação sem LM Studio real):
    python3 scripts/fake_lmstudio.py --port 1234 &
    python3 scripts/e2e_check.py --url http://localhost:8000 \
        --lm-url http://localhost:1234 --fake-control http://localhost:1234 \
        --restart-cmd "docker compose restart app"

Passos validados:
 1 servidor no ar            2 interface web             3 conexão LM Studio
 4 modelos detectados        5 modelo embedding salvo    6 importação de docs
 7 ingestão (manual+auto)    8 embeddings gerados        9 vetores no pgvector
10 restart + persistência   11 ingestão interrompida → retomada
12 consulta via MCP         13 trechos com referência de fonte
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = "\033[1;32mPASS\033[0m", "\033[1;31mFAIL\033[0m"


def step(n, title):
    print(f"\n\033[1;36m[{n:02d}]\033[0m {title}")


def ok(msg):
    print(f"     {PASS} {msg}")


def die(msg):
    print(f"     {FAIL} {msg}")
    sys.exit(1)


class HTTP:
    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token

    def req(self, method: str, path: str, body=None, headers=None, timeout=120):
        url = path if path.startswith("http") else self.base + path
        data = None
        hdrs = dict(headers or {})
        if self.token:
            hdrs["Authorization"] = "Bearer " + self.token
        if body is not None and not isinstance(body, bytes):
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        elif isinstance(body, bytes):
            data = body
        r = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            resp = urllib.request.urlopen(r, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            raise RuntimeError(f"{method} {url} → HTTP {e.code}: {detail}") from None
        raw = resp.read()
        ctype = resp.headers.get("content-type", "")
        return resp.status, dict(resp.headers), (json.loads(raw) if "json" in ctype and raw else raw)

    def get(self, path, **kw):
        return self.req("GET", path, **kw)[2]

    def post(self, path, body=None, **kw):
        return self.req("POST", path, body if body is not None else {}, **kw)[2]

    def put(self, path, body, **kw):
        return self.req("PUT", path, body, **kw)[2]

    def delete(self, path, **kw):
        return self.req("DELETE", path, **kw)[2]

    def upload(self, col_id: str, files: list[tuple[str, bytes]]):
        boundary = uuid.uuid4().hex
        buf = io.BytesIO()

        def part(name, value, filename=None):
            buf.write(f"--{boundary}\r\n".encode())
            if filename:
                buf.write(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
                buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
                buf.write(value)
            else:
                buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
                buf.write(value.encode())
            buf.write(b"\r\n")

        part("collection_id", col_id)
        for name, content in files:
            part("paths", name)
            part("files", content, filename=Path(name).name)
        buf.write(f"--{boundary}--\r\n".encode())
        return self.req("POST", "/api/documents/upload", body=buf.getvalue(),
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                        timeout=300)[2]


class MCP:
    """Cliente MCP Streamable HTTP mínimo (stdlib)."""

    def __init__(self, url: str, token: str = ""):
        self.url, self.token, self.sid, self.rid = url, token, None, 0

    def _post(self, payload):
        hdrs = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        if self.token:
            hdrs["Authorization"] = "Bearer " + self.token
        if self.sid:
            hdrs["mcp-session-id"] = self.sid
        r = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                   headers=hdrs, method="POST")
        try:
            resp = urllib.request.urlopen(r, timeout=120)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"MCP HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
        self.sid = resp.headers.get("mcp-session-id") or self.sid
        raw = resp.read().decode()
        ctype = resp.headers.get("content-type", "")
        if not raw:
            return None
        if ctype.startswith("text/event-stream"):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == payload.get("id"):
                        return msg
            return None
        return json.loads(raw)

    def call(self, method, params=None, notify=False):
        self.rid += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self.rid
        resp = self._post(payload)
        if notify:
            return None
        if resp is None:
            raise RuntimeError(f"MCP sem resposta para {method}")
        if "error" in resp:
            raise RuntimeError(f"MCP erro: {resp['error']}")
        return resp["result"]

    def initialize(self):
        result = self.call("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "rag-e2e-check", "version": "1.0"},
        })
        self.call("notifications/initialized", notify=True)
        return result


def wait_health(http: HTTP, timeout: float):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            h = http.get("/api/health")
            if h.get("database"):
                return h
        except Exception:
            pass
        time.sleep(2)
    die(f"servidor não ficou saudável em {timeout}s")


def wait_restart(http: HTTP, timeout_down: float = 30, timeout_up: float = 180):
    """Espera o processo antigo cair (se ainda visível) e o novo subir.

    Comandos síncronos (docker compose restart) já retornam com o novo
    container no ar; nesse caso a fase 'down' pode não ser observada.
    """
    t0 = time.time()
    while time.time() - t0 < timeout_down:
        try:
            http.get("/api/health")
        except Exception:
            break  # caiu — agora espera subir
        time.sleep(0.3)
    wait_health(http, timeout_up)


def wait_docs(http: HTTP, col_id: str, timeout: float, expect_ready: int):
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        docs = http.get(f"/api/documents?collection_id={col_id}&limit=500")["items"]
        ready = sum(1 for d in docs if d["status"] == "ready")
        errors = [d for d in docs if d["status"] == "error"]
        state = ",".join(sorted({d["status"] for d in docs}))
        if state != last:
            print(f"     … status: {state} ({ready}/{expect_ready} prontos)")
            last = state
        if errors:
            die(f"documento em erro: {errors[0]['filename']}: {errors[0]['error']}")
        if ready >= expect_ready:
            return docs
        time.sleep(2)
    die(f"ingestão não concluiu em {timeout}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--lm-url", default=None, help="URL do LM Studio a configurar no servidor")
    ap.add_argument("--embed-model", default=None)
    ap.add_argument("--llm-model", default=None)
    ap.add_argument("--token", default=os.environ.get("RAG_API_TOKEN", ""))
    ap.add_argument("--restart-cmd", default=None,
                    help='comando que reinicia o app, ex.: "docker compose restart app"')
    ap.add_argument("--fake-control", default=None,
                    help="URL do LM Studio simulado p/ injetar falhas (ex.: http://localhost:1234)")
    ap.add_argument("--timeout", type=float, default=300, help="timeout por ingestão (s)")
    ap.add_argument("--keep", action="store_true", help="não exclui a coleção de teste no final")
    args = ap.parse_args()

    http = HTTP(args.url, args.token)
    col_name = f"e2e-{uuid.uuid4().hex[:8]}"
    col_id = None

    try:
        # 1 ----------------------------------------------------------
        step(1, "Servidor no ar")
        h = wait_health(http, 60)
        ok(f"health ok (db={h['database']}, worker={h['worker']['running']})")

        # 2 ----------------------------------------------------------
        step(2, "Interface web acessível")
        _, _, page = http.req("GET", "/")
        if b"RAG Server" not in page:
            die("página inicial não parece a interface do RAG Server")
        ok("GET / retorna a interface")

        # 3 ----------------------------------------------------------
        step(3, "Conexão com o LM Studio")
        if args.lm_url:
            http.put("/api/settings", {"lmstudio_url": args.lm_url})
        t = http.post("/api/lmstudio/test", {})
        if not t.get("reachable"):
            die(f"LM Studio inacessível: {t.get('error')}")
        ok(f"conectado em {t['url']} ({t.get('models')} modelos)")

        # 4 ----------------------------------------------------------
        step(4, "Detecção de modelos")
        models = http.get("/api/lmstudio/models")["models"]
        if not models:
            die("nenhum modelo disponível no LM Studio")
        emb = args.embed_model or next((m["id"] for m in models if m["type"] == "embedding"), None) \
            or next((m["id"] for m in models if m["type"] != "llm"), None)
        llm = args.llm_model or next((m["id"] for m in models if m["type"] == "llm"), None) \
            or next((m["id"] for m in models if m["type"] != "embedding"), None)
        if not emb:
            die("nenhum modelo de embedding encontrado (use --embed-model)")
        ok(f"embedding: {emb} | llm: {llm or '(nenhum)'}")

        # 5 ----------------------------------------------------------
        step(5, "Seleção do modelo de embedding")
        http.put("/api/settings", {"embedding_model": emb, **({"llm_model": llm} if llm else {})})
        ok("modelos salvos nas settings")

        # 6 ----------------------------------------------------------
        step(6, "Importação de documentos")
        col_id = http.post("/api/collections", {"name": col_name,
                                                "description": "coleção de validação e2e"})["id"]
        samples = sorted((ROOT / "examples" / "sample_docs").glob("*.md"))
        files = [(f"pasta/{p.name}", p.read_bytes()) for p in samples]
        big_text = "\n\n".join(
            f"Seção {i} do documento extenso. A retenção de backups anuais é de cinco anos "
            f"em armazenamento frio, com criptografia AES-256 e rotação anual de chaves. "
            f"O collector do ARGUS envia métricas a cada quinze segundos via gRPC. " * 3
            for i in range(120))
        files.append(("pasta/grande.txt", big_text.encode()))
        up = http.upload(col_id, files)
        if up["staged"] != len(files):
            die(f"upload: esperava {len(files)} staged, veio {up}")
        ok(f"{up['staged']} documentos importados (incluindo subpasta)")
        dup = http.upload(col_id, files[:1])
        if dup["duplicates"] != 1:
            die("deduplicação falhou: reupload do mesmo conteúdo não foi marcado como duplicado")
        ok("reupload do mesmo conteúdo → duplicado ignorado (idempotência)")

        # 7 ----------------------------------------------------------
        step(7, "Ingestão: modo manual + modo automático")
        r = http.post("/api/ingest/start", {
            "collection_id": col_id, "mode": "manual",
            "config": {"chunk_size": 900, "chunk_overlap": 120, "strategy": "recursive",
                       "embed_batch_size": 8},
            "embedding_model": emb})
        ok(f"manual: {r['started']} docs iniciados (chunk_size=900, overlap=120)")
        docs = wait_docs(http, col_id, args.timeout, len(files))
        if llm:
            sug_doc = http.upload(col_id, [("auto/sugestao.md",
                                           ("# Manual do telescópio\n\n" + ("O telescópio deve ser calibrado "
                                            "antes de cada sessão de observação noturna. Use a estrela polar "
                                            "como referência inicial de alinhamento.\n\n") * 40).encode())])
            sug = http.post("/api/ingest/suggest", {"collection_id": col_id, "model": llm})
            ok(f"automático: sugestão via {sug['source']} → {sug['config']['strategy']}, "
               f"chunk_size={sug['config']['chunk_size']} ({sug['rationale'][:70]}…)")
            http.post("/api/ingest/start", {"collection_id": col_id, "mode": "auto",
                                            "config": sug["config"], "embedding_model": emb,
                                            "suggestion": sug})
            docs = wait_docs(http, col_id, args.timeout, len(files) + 1)
        total_docs = len(docs)

        # 8 ----------------------------------------------------------
        step(8, "Embeddings gerados")
        total_chunks = sum(d["chunks_total"] for d in docs)
        embedded = sum(d["chunks_embedded"] for d in docs)
        if not (total_chunks > 0 and embedded == total_chunks):
            die(f"chunks: {embedded}/{total_chunks} embedados")
        ok(f"{embedded}/{total_chunks} chunks embedados em {total_docs} documentos")

        # 9 ----------------------------------------------------------
        step(9, "Vetores armazenados no pgvector (busca semântica)")
        s = http.post("/api/search", {"query": "qual a retenção dos backups anuais?",
                                      "collections": [col_name], "top_k": 5})
        if not s["results"]:
            die("busca não retornou resultados")
        top = s["results"][0]
        if not any("backup" in r["content"].lower() or "retenção" in r["content"].lower()
                   for r in s["results"][:3]):
            die(f"nenhum dos 3 primeiros resultados parece relevante: {top['content'][:120]}")
        ok(f"busca ok: top score={top['score']} de {top['source_path']}")

        # 10 ---------------------------------------------------------
        step(10, "Restart dos containers + persistência")
        if args.restart_cmd:
            print(f"     … executando: {args.restart_cmd}")
            subprocess.run(args.restart_cmd, shell=True, check=True, cwd=ROOT)
            wait_restart(http)
            docs2 = http.get(f"/api/documents?collection_id={col_id}&status=ready&limit=500")
            if docs2["total"] != total_docs:
                die(f"após restart: {docs2['total']} prontos (esperava {total_docs})")
            s2 = http.post("/api/search", {"query": "retenção de backups anuais",
                                           "collections": [col_name]})
            if not s2["results"]:
                die("busca vazia após restart — dados não persistiram")
            ok(f"restart ok: {docs2['total']} docs prontos e busca funcionando (dados persistidos)")
        else:
            print("     (pulado: sem --restart-cmd; rode com 'docker compose restart app' para validar)")

        # 11 ---------------------------------------------------------
        step(11, "Ingestão interrompida → retomada do ponto em que parou")
        interrupt_text = "\n\n".join(
            f"Parágrafo {i} sobre o runbook de disco do ARGUS: verifique a partição com df -h, "
            f"remova logs antigos com mais de sete dias e nunca apague o diretório wal manualmente. " * 3
            for i in range(150))
        http.upload(col_id, [("interrupcao/interrompido.txt", interrupt_text.encode())])
        http.post("/api/ingest/start", {"collection_id": col_id, "mode": "manual",
                                        "config": {"chunk_size": 700, "chunk_overlap": 80,
                                                   "embed_batch_size": 4},
                                        "embedding_model": emb})
        target = total_docs + 1
        if args.fake_control:
            ctl = HTTP(args.fake_control)
            t0 = time.time()
            progressed = False
            while time.time() - t0 < 60:
                items = http.get(f"/api/documents?collection_id={col_id}&limit=500")["items"]
                d = next((x for x in items if x["filename"] == "interrompido.txt"), None)
                if d and d["chunks_embedded"] > 0 and d["status"] != "ready":
                    progressed = True
                    break
                if d and d["status"] == "ready":
                    break
                time.sleep(0.3)
            ctl.post("/_control/down", {"down": True})
            ok("LM Studio derrubado no meio da ingestão" + (" (após progresso parcial)" if progressed else ""))
            t0 = time.time()
            saw_waiting = False
            while time.time() - t0 < 90:
                items = http.get(f"/api/documents?collection_id={col_id}&limit=500")["items"]
                d = next((x for x in items if x["filename"] == "interrompido.txt"), None)
                if d and d["status"] == "waiting":
                    saw_waiting = True
                    break
                if d and d["status"] == "ready":
                    break
                time.sleep(1)
            if saw_waiting:
                ok("documento entrou em 'waiting' com backoff (progresso preservado)")
            before = ctl.get("/_control/stats")["embedded_texts"]
            ctl.post("/_control/down", {"down": False})
            docs = wait_docs(http, col_id, args.timeout, target)
            d = next(x for x in docs if x["filename"] == "interrompido.txt")
            after = ctl.get("/_control/stats")["embedded_texts"]
            reembeds = after - before
            ok(f"LM Studio voltou → retomou sozinho e concluiu ({d['chunks_embedded']}/{d['chunks_total']} chunks)")
            if progressed and reembeds > d["chunks_total"]:
                die(f"retomada re-embedou chunks demais ({reembeds} > {d['chunks_total']})")
            if progressed:
                ok(f"idempotência: apenas {reembeds} textos re-enviados após a queda (não recomeçou do zero)")
        else:
            print("     (sem --fake-control: usando pausa/retomada via API)")
            time.sleep(2)
            http.post("/api/ingest/pause")
            time.sleep(1)
            st = http.get("/api/ingest/status")
            if not st["paused"]:
                die("pausa não registrada")
            ok("ingestão pausada (estado persistido no banco)")
            http.post("/api/ingest/resume")
            docs = wait_docs(http, col_id, args.timeout, target)
            ok("ingestão retomada e concluída")
        if args.restart_cmd:
            http.upload(col_id, [("interrupcao/crash.txt", (interrupt_text + "\n\ncrash test").encode())])
            http.post("/api/ingest/start", {"collection_id": col_id,
                                            "config": {"chunk_size": 700, "embed_batch_size": 2},
                                            "embedding_model": emb})
            time.sleep(1.5)
            print(f"     … reiniciando no meio do processamento: {args.restart_cmd}")
            subprocess.run(args.restart_cmd, shell=True, check=True, cwd=ROOT)
            wait_restart(http)
            docs = wait_docs(http, col_id, args.timeout, target + 1)
            ok("processo reiniciado no meio da ingestão → retomou e concluiu sozinho")
            total_docs = target + 1
        else:
            total_docs = target

        # 12 ---------------------------------------------------------
        step(12, "Consulta via MCP (cliente remoto)")
        mcp = MCP(args.url.rstrip("/") + "/mcp", args.token)
        init = mcp.initialize()
        ok(f"MCP initialize ok: {init['serverInfo']['name']} (protocolo {init['protocolVersion']})")
        tools = mcp.call("tools/list")["tools"]
        names = {t["name"] for t in tools}
        if "rag_search" not in names:
            die(f"rag_search não exposto; tools: {names}")
        ok(f"tools: {', '.join(sorted(names))}")
        res = mcp.call("tools/call", {"name": "rag_search",
                                      "arguments": {"query": "como proceder no alerta de disco do ARGUS?",
                                                    "collections": [col_name], "top_k": 3}})
        structured = res.get("structuredContent") or json.loads(res["content"][0]["text"])
        results = structured.get("results", [])
        if not results:
            die(f"MCP rag_search sem resultados: {structured}")
        ok(f"rag_search retornou {len(results)} trecho(s)")

        # 13 ---------------------------------------------------------
        step(13, "Trechos com referência completa da fonte")
        top = results[0]
        required = ["content", "score", "document", "source_path", "collection", "location", "metadata"]
        missing = [k for k in required if k not in top]
        if missing:
            die(f"campos ausentes no resultado: {missing}")
        if not any("argus" in (r["source_path"] + r["content"]).lower() for r in results):
            die(f"nenhum resultado veio da fonte esperada (ARGUS): {[r['source_path'] for r in results]}")
        ok(f"fonte: {top['source_path']} · coleção {top['collection']} · "
           f"score {top['score']} · local: {top.get('location_label') or top['location']}")
        print(f"\n     exemplo de trecho: \"{top['content'][:140].strip()}…\"")

        print(f"\n\033[1;32m✔ VALIDAÇÃO END-TO-END COMPLETA — todos os passos passaram\033[0m")
    finally:
        if col_id and not args.keep:
            try:
                http.delete(f"/api/collections/{col_id}")
                print(f"(coleção de teste '{col_name}' removida; use --keep para mantê-la)")
            except Exception:
                pass


if __name__ == "__main__":
    main()
