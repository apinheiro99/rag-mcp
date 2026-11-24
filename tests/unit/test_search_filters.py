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


@pytest.mark.parametrize("value", [
    "",
    "not-a-uuid",
    "123",
    "11111111-1111-1111-1111-11111111111z",
    "../etc/passwd",
    "' OR 1=1 --",
])
def test_search_filters_fuzz_corpus_rejects_bad_document_ids(value):
    with pytest.raises(ValueError, match="document_id inválido"):
        SearchFilters(document_ids=[value]).validate()


@pytest.mark.parametrize("page", [0, -1, -999])
def test_search_filters_boundary_rejects_non_positive_pages(page):
    with pytest.raises(ValueError, match="inteiros positivos"):
        SearchFilters(pages=[page]).validate()


def test_search_filters_corner_case_keeps_all_extreme_valid_filters_parameterized():
    filters = SearchFilters(
        document_ids=["11111111-1111-1111-1111-111111111111"],
        authors=["O'Reilly % _"],
        source_paths=["book' OR 1=1 --.epub"],
        chapters=["Chapter 999999"],
        pages=[2_147_483_647],
    )
    filters.validate()
    sql, params = filters.sql()
    assert "book' OR 1=1 --.epub" not in sql
    assert params[-1] == [2_147_483_647]
