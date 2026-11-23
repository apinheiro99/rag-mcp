"""Modo automático: um LLM do LM Studio analisa uma amostra representativa
dos documentos e recomenda parâmetros de ingestão.

Fluxo: estatísticas + amostra → prompt → JSON do LLM → validação/clamps →
sugestão exibida na UI (aceitar / editar / voltar ao manual).
Se o LLM falhar ou responder lixo, cai para uma heurística determinística
baseada nas características do conteúdo (e a UI avisa que foi heurística).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .chunking import ConfigError, validate_ingest_config
from .lmstudio import LMStudioClient, LMStudioError

log = logging.getLogger("rag.auto")

SYSTEM_PROMPT = """Você é um engenheiro de RAG especialista em chunking e embeddings.
Analise as características e a amostra dos documentos e escolha parâmetros de ingestão.
Responda SOMENTE com um objeto JSON válido, sem comentários, sem markdown, no formato:
{
  "strategy": "recursive" | "fixed" | "markdown" | "pages",
  "chunk_size": <inteiro, caracteres>,
  "chunk_overlap": <inteiro, caracteres>,
  "min_chunk_size": <inteiro, caracteres>,
  "rationale": "<explicação curta em português>"
}
Diretrizes:
- Documentos com páginas claras (PDF de livro/artigo) favorecem "pages".
- Markdown/HTML bem estruturado por seções favorece "markdown".
- Prosa contínua e densa favorece "recursive" com overlap de 10-15% do chunk_size.
- Código, tabelas, logs ou texto muito regular favorecem "fixed" com overlap menor.
- Texto denso/técnico: chunks menores (800-1200). Narrativa longa: 1500-2500.
- chunk_overlap deve ser < metade de chunk_size. Respeite os limites informados."""


def build_stats(samples: list[dict]) -> dict:
    """samples: [{filename, meta, text}]"""
    total_chars = sum(len(s["text"]) for s in samples)
    paras = [p for s in samples for p in re.split(r"\n\s*\n", s["text"]) if p.strip()]
    avg_para = sum(len(p) for p in paras) / max(1, len(paras))
    headings = sum(1 for s in samples for line in s["text"].split("\n")
                   if re.match(r"^#{1,6}\s", line))
    return {
        "documents": len(samples),
        "filenames": [s["filename"] for s in samples],
        "kinds": sorted({s["meta"].get("kind", "?") for s in samples}),
        "has_pages": any("pages" in s["meta"] for s in samples),
        "has_chapters": any("chapters" in s["meta"] for s in samples),
        "total_sample_chars": total_chars,
        "avg_paragraph_chars": round(avg_para),
        "markdown_headings_in_sample": headings,
        "total_file_bytes": sum(s.get("size_bytes", 0) for s in samples),
    }


def heuristic_suggestion(stats: dict, defaults: dict, limits: dict) -> dict:
    ing = defaults["ingest"]
    kinds = stats.get("kinds", [])
    if stats.get("has_pages"):
        strategy, size, overlap = "pages", 1500, 150
        why = "PDF com páginas bem definidas: chunking por página preserva a referência de origem."
    elif "markdown" in kinds or stats.get("markdown_headings_in_sample", 0) >= 3:
        strategy, size, overlap = "markdown", 1500, 150
        why = "Documento estruturado por seções: chunking por seção mantém contexto e título."
    elif stats.get("has_chapters"):
        strategy, size, overlap = "recursive", 1800, 250
        why = "Livro/EPUB com capítulos: prosa longa pede chunks maiores com overlap moderado."
    elif stats.get("avg_paragraph_chars", 0) < 160:
        strategy, size, overlap = "recursive", 1000, 120
        why = "Parágrafos curtos e texto denso: chunks menores melhoram a precisão da busca."
    else:
        strategy, size, overlap = ing["strategy"], ing["chunk_size"], ing["chunk_overlap"]
        why = "Sem estrutura forte detectada: estratégia recursive padrão com overlap de ~13%."
    cfg = validate_ingest_config(
        {"strategy": strategy, "chunk_size": size, "chunk_overlap": overlap,
         "min_chunk_size": ing["min_chunk_size"], "separators": ing["separators"]}, limits)
    cfg["rationale"] = why
    return cfg


def _parse_llm_json(content: str) -> dict:
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    start, depth = content.find("{"), 0
    if start < 0:
        raise ValueError("nenhum objeto JSON na resposta")
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(content[start:i + 1])
    raise ValueError("JSON incompleto na resposta")


async def suggest_config(client: LMStudioClient, model: str, samples: list[dict],
                         defaults: dict, auto_cfg: dict) -> dict[str, Any]:
    """Retorna {config, rationale, source: llm|heuristic, stats, warning?}."""
    limits = defaults["limits"]
    stats = build_stats(samples)
    sample_block = "\n\n".join(
        f"--- AMOSTRA DE {s['filename']} (tipo: {s['meta'].get('kind')}) ---\n"
        f"{s['text'][:auto_cfg['sample_chars']]}"
        for s in samples)
    user_prompt = (
        f"ESTATÍSTICAS:\n{json.dumps(stats, ensure_ascii=False, indent=2)}\n\n"
        f"LIMITES OBRIGATÓRIOS:\n"
        f"- chunk_size: {limits['chunk_size_range'][0]}..{limits['chunk_size_range'][1]}\n"
        f"- chunk_overlap: {limits['chunk_overlap_range'][0]}..{limits['chunk_overlap_range'][1]} (e < chunk_size/2)\n"
        f"- min_chunk_size: {limits['min_chunk_size_range'][0]}..{limits['min_chunk_size_range'][1]}\n\n"
        f"DEFAULTS ATUAIS: chunk_size={defaults['ingest']['chunk_size']}, "
        f"overlap={defaults['ingest']['chunk_overlap']}, strategy={defaults['ingest']['strategy']}\n\n"
        f"{sample_block}\n\nResponda somente com o JSON pedido.")
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}]
    warning = None
    last_content = ""
    for attempt in range(2):
        try:
            content = await client.chat(messages, model,
                                        temperature=auto_cfg["temperature"],
                                        max_tokens=auto_cfg["max_tokens"],
                                        timeout=auto_cfg["request_timeout"])
            last_content = content
            data = _parse_llm_json(content)
            cfg = validate_ingest_config({
                "strategy": data.get("strategy", "recursive"),
                "chunk_size": data.get("chunk_size", defaults["ingest"]["chunk_size"]),
                "chunk_overlap": data.get("chunk_overlap", defaults["ingest"]["chunk_overlap"]),
                "min_chunk_size": data.get("min_chunk_size", defaults["ingest"]["min_chunk_size"]),
                "separators": defaults["ingest"]["separators"],
            }, limits)
            rationale = str(data.get("rationale", "")).strip() or "Sem justificativa informada pelo modelo."
            return {"config": cfg, "rationale": rationale, "source": "llm",
                    "stats": stats, "model": model}
        except (LMStudioError, ValueError, ConfigError, KeyError, TypeError) as e:
            log.warning("sugestão LLM falhou (tentativa %d): %s", attempt + 1, e)
            warning = f"LLM não retornou configuração válida: {e}"
            if isinstance(e, LMStudioError) and "conexão" in str(e):
                break  # servidor fora do ar: não adianta repetir
            messages.append({"role": "assistant", "content": last_content})
            messages.append({"role": "user", "content":
                             "Sua resposta não era um JSON válido com os campos pedidos. "
                             "Responda SOMENTE com o objeto JSON, sem nenhum outro texto."})
    cfg = heuristic_suggestion(stats, defaults, limits)
    return {"config": cfg, "rationale": cfg.pop("rationale"), "source": "heuristic",
            "stats": stats, "model": model, "warning": warning}
