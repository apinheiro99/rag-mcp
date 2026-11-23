"""Chunking configurável: recursive | fixed | markdown | pages.

Unidades em caracteres (independente de tokenizer/modelo).
- recursive: divide por hierarquia de separadores e funde até chunk_size,
  com sobreposição (overlap) entre chunks consecutivos do mesmo segmento.
- fixed: janelas de tamanho fixo com passo (size - overlap).
- markdown: agrupa segmentos por seção (heading) e nunca mistura seções.
- pages: agrupa por página (PDF); páginas pequenas viram 1 chunk,
  páginas grandes são divididas com recursive. Sem páginas → recursive.

Cada chunk preserva `loc` (page/chapter/heading + span de caracteres)
para rastreabilidade da fonte no resultado de busca.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .extraction import Segment


@dataclass
class ChunkDraft:
    seq: int
    text: str
    loc: dict[str, Any]


class ConfigError(ValueError):
    pass


def validate_ingest_config(cfg: dict, limits: dict) -> dict:
    """Valida/clampa parâmetros de ingestão. Levanta ConfigError se inválido."""
    out = dict(cfg)
    strategy = out.get("strategy", "recursive")
    if strategy not in ("recursive", "fixed", "markdown", "pages"):
        raise ConfigError(f"estratégia inválida: {strategy}")
    lo, hi = limits.get("chunk_size_range", [200, 8000])
    size = int(out.get("chunk_size", 1500))
    if not (lo <= size <= hi):
        raise ConfigError(f"chunk_size deve estar entre {lo} e {hi}")
    olo, ohi = limits.get("chunk_overlap_range", [0, 1000])
    overlap = int(out.get("chunk_overlap", 200))
    if not (olo <= overlap <= ohi):
        raise ConfigError(f"chunk_overlap deve estar entre {olo} e {ohi}")
    if overlap >= size:
        raise ConfigError("chunk_overlap deve ser menor que chunk_size")
    overlap = min(overlap, size // 2)
    mlo, mhi = limits.get("min_chunk_size_range", [0, 2000])
    min_size = int(out.get("min_chunk_size", 120))
    min_size = max(mlo, min(min_size, mhi, size))
    seps = out.get("separators", ["\n\n", "\n", ". ", " "])
    if not isinstance(seps, list) or not all(isinstance(s, str) for s in seps):
        raise ConfigError("separators deve ser lista de strings")
    out.update({"strategy": strategy, "chunk_size": size, "chunk_overlap": overlap,
                "min_chunk_size": min_size, "separators": seps})
    return out


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def _hard_split(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size) if text[i:i + size].strip()]


def _recursive_split(text: str, separators: list[str], size: int) -> list[str]:
    if len(text) <= size:
        return [text] if text.strip() else []
    if not separators:
        return _hard_split(text, size)
    sep, rest = separators[0], separators[1:]
    if sep == "":
        return _hard_split(text, size)
    parts = text.split(sep)
    pieces: list[str] = []
    for i, part in enumerate(parts):
        candidate = part + (sep if i < len(parts) - 1 else "")
        if not candidate.strip():
            continue
        if len(candidate) <= size:
            pieces.append(candidate)
        elif rest:
            pieces.extend(_recursive_split(candidate, rest, size))
        else:
            pieces.extend(_hard_split(candidate, size))
    return pieces


def _merge_pieces(pieces: list[str], size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) > size:
            chunks.append(cur.strip())
            tail = cur[-overlap:] if overlap else ""
            if tail:
                # não começar o próximo chunk no meio de uma palavra
                m = re.search(r"\s", tail)
                if m and m.start() < len(tail) - 1:
                    tail = tail[m.start() + 1:]
                if len(tail) + len(p) > size:
                    tail = tail[:max(0, size - len(p))]
            cur = tail + p
        else:
            cur += p
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if c]


def _chunks_for_text(text: str, cfg: dict) -> list[str]:
    size, overlap = cfg["chunk_size"], cfg["chunk_overlap"]
    if cfg["strategy"] == "fixed":
        step = max(1, size - overlap)
        return [text[i:i + size].strip() for i in range(0, len(text), step)
                if text[i:i + size].strip()]
    pieces = _recursive_split(text, list(cfg["separators"]), size)
    return _merge_pieces(pieces, size, overlap)


def _emit(segments: list[Segment], cfg: dict, group_key=None) -> list[ChunkDraft]:
    """Agrupa segmentos por `group_key` (opcional) e chunka cada grupo."""
    drafts: list[ChunkDraft] = []
    groups: list[tuple[dict, str]] = []
    for seg in segments:
        key = group_key(seg) if group_key else id(seg)
        if groups and group_key and groups[-1][0] == key:
            groups[-1] = (key, groups[-1][1] + "\n\n" + seg.text)
        else:
            groups.append((key if group_key else seg.loc, seg.text))
    for loc, text in groups:
        loc = {k: v for k, v in loc.items() if v is not None}
        pos = 0
        for piece in _chunks_for_text(text, cfg):
            start = text.find(piece[:60], max(0, pos - cfg["chunk_overlap"] - 80))
            if start < 0:
                start = pos
            end = start + len(piece)
            chunk_loc = {**loc, "char_start": start, "char_end": end}
            drafts.append(ChunkDraft(len(drafts), piece, chunk_loc))
            pos = max(pos, end - cfg["chunk_overlap"])
    return drafts


def _merge_small(drafts: list[ChunkDraft], cfg: dict) -> list[ChunkDraft]:
    """Funde chunks minúsculos com o anterior quando têm a mesma origem."""
    if not drafts:
        return drafts
    min_size = cfg["min_chunk_size"]
    out: list[ChunkDraft] = []
    for d in drafts:
        base_loc = {k: v for k, v in d.loc.items() if k not in ("char_start", "char_end")}
        if out:
            prev = out[-1]
            prev_loc = {k: v for k, v in prev.loc.items() if k not in ("char_start", "char_end")}
            if (len(d.text) < min_size and base_loc == prev_loc
                    and len(prev.text) + len(d.text) + 2 <= cfg["chunk_size"] * 1.25):
                prev.text = prev.text + "\n\n" + d.text
                prev.loc["char_end"] = d.loc.get("char_end", prev.loc.get("char_end"))
                continue
        out.append(d)
    for i, d in enumerate(out):
        d.seq = i
    return out


def chunk_segments(segments: list[Segment], cfg: dict) -> list[ChunkDraft]:
    strategy = cfg["strategy"]
    has_pages = any("page" in s.loc for s in segments)
    if strategy == "pages" and has_pages:
        drafts = _emit(segments, cfg, group_key=lambda s: {"page": s.loc.get("page")})
    elif strategy == "markdown":
        drafts = _emit(segments, cfg, group_key=lambda s: {"heading": s.loc.get("heading"),
                                                           "chapter": s.loc.get("chapter")})
    else:
        drafts = _emit(segments, cfg)
    return _merge_small(drafts, cfg)
