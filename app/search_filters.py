"""Filtros estruturados para busca semântica."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class SearchFilters:
    document_ids: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)
    chapters: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)

    def active(self) -> bool:
        return any(asdict(self).values())

    def validate(self) -> None:
        for value in self.document_ids:
            try:
                uuid.UUID(str(value))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"document_id inválido: {value}") from exc
        if any(int(page) <= 0 for page in self.pages):
            raise ValueError("pages deve conter inteiros positivos")

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v}

    def sql(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.document_ids:
            clauses.append("d.id = ANY(%s::uuid[])")
            params.append(self.document_ids)
        if self.authors:
            clauses.append("(d.meta->>'author') ILIKE ANY(%s)")
            params.append([f"%{a}%" for a in self.authors])
        if self.source_paths:
            clauses.append("d.source_path = ANY(%s)")
            params.append(self.source_paths)
        if self.chapters:
            clauses.append("(c.loc->>'chapter') = ANY(%s)")
            params.append(self.chapters)
        if self.pages:
            clauses.append("((c.loc->>'page')::int) = ANY(%s)")
            params.append(self.pages)
        return (" AND ".join(clauses), params)
