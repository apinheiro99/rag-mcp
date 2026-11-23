import pytest

from app.search_filters import SearchFilters


def test_search_filters_build_all_supported_predicates():
    filters = SearchFilters(
        document_ids=["11111111-1111-1111-1111-111111111111"],
        authors=["Fowler"],
        source_paths=["book.epub"],
        chapters=["Chapter 1"],
        pages=[7],
    )

    sql, params = filters.sql()

    assert "d.id = ANY" in sql
    assert "d.meta->>'author'" in sql
    assert "d.source_path = ANY" in sql
    assert "c.loc->>'chapter'" in sql
    assert "c.loc->>'page'" in sql
    assert len(params) == 5


def test_search_filters_reject_invalid_document_id():
    with pytest.raises(ValueError, match="document_id inválido"):
        SearchFilters(document_ids=["not-a-uuid"]).validate()


def test_search_filters_reject_non_positive_page():
    with pytest.raises(ValueError, match="inteiros positivos"):
        SearchFilters(pages=[0]).validate()
