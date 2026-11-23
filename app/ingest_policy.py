"""Políticas de workflow para ingestão.

Mantém decisões de fluxo separadas do transporte HTTP e da persistência.
"""
from __future__ import annotations


def should_auto_start_upload(
    *,
    enabled: bool,
    collection_config: dict | None,
    collection_model: str | None,
    default_model: str | None,
) -> bool:
    """Auto-inicia apenas quando a coleção já tem configuração e modelo utilizável."""
    if not enabled:
        return False
    if not collection_config:
        return False
    return bool(collection_model or default_model)


def should_ignore_empty_upload(*, size_bytes: int) -> bool:
    return size_bytes == 0
