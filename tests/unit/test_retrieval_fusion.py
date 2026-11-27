from app.search import fuse_rankings
from scripts.evaluate_retrieval import evaluate


def test_fusion_rewards_agreement_and_deduplicates():
    a,b,c = ({"chunk_id":x,"score":.5} for x in "abc")
    ranked=fuse_rankings([[a,b,a],[c,b]],60)
    assert ranked[0]["chunk_id"]=="b"
    assert len(ranked)==3
    assert ranked[0]["score"]==.5


def test_evaluation_counts_known_sources_not_exhaustive_relevance():
    results=[{"document":"Other","content":"Heading"},{"document":"Expected book","content":"A meaningful passage with more than five words."}]
    m=evaluate(results,["Expected"],5)
    assert m["known_source_recall"]==1
    assert m["reciprocal_rank"]==.5
    assert m["heading_only_count"]==1

from app.search import should_apply_lexical


def test_lexical_fallback_skips_when_dense_is_strong():
    cfg={"hybrid_enabled":True,"hybrid_min_dense_score":.55,"hybrid_min_dense_candidates":8}
    dense=[{"score":.72}]+[{"score":.4} for _ in range(7)]
    assert should_apply_lexical("ordinary semantic question",dense,8,cfg)==(False,"dense_sufficient")


def test_lexical_fallback_runs_when_dense_is_weak_or_short():
    cfg={"hybrid_enabled":True,"hybrid_min_dense_score":.55,"hybrid_min_dense_candidates":8}
    assert should_apply_lexical("ordinary semantic question",[{"score":.4} for _ in range(8)],8,cfg)==(True,None)
    assert should_apply_lexical("ordinary semantic question",[{"score":.9} for _ in range(3)],8,cfg)==(True,None)


def test_lexical_fallback_threshold_boundaries():
    cfg={"hybrid_enabled":True,"hybrid_min_dense_score":.55,"hybrid_min_dense_candidates":8}

    exact=[{"score":.55}]+[{"score":.1} for _ in range(7)]
    assert should_apply_lexical("ordinary semantic question",exact,8,cfg)==(False,"dense_sufficient")

    just_below=[{"score":.5499}]+[{"score":.1} for _ in range(7)]
    assert should_apply_lexical("ordinary semantic question",just_below,8,cfg)==(True,None)

    one_short=[{"score":.99} for _ in range(7)]
    assert should_apply_lexical("ordinary semantic question",one_short,8,cfg)==(True,None)



from app.search import annotate_selection, choose_adaptive_top_k, retrieval_usefulness, token_cost_estimate


def test_adaptive_top_k_respects_relevance_curve_and_explicit_override():
    cfg = {
        "adaptive_top_k_enabled": True,
        "adaptive_top_k_min": 2,
        "adaptive_top_k_max": 5,
        "adaptive_top_k_min_score": 0.55,
        "adaptive_top_k_gap": 0.18,
        "adaptive_top_k_allow_below_explicit": False,
    }
    rows = [
        {"score": .9, "rerank_score": .95},
        {"score": .8, "rerank_score": .88},
        {"score": .7, "rerank_score": .70},
        {"score": .6, "rerank_score": .30},
    ]
    chosen, meta = choose_adaptive_top_k(rows, 4, cfg, explicit=False)
    assert chosen == 3
    assert meta["enabled"] is True
    chosen, meta = choose_adaptive_top_k(rows, 4, cfg, explicit=True)
    assert chosen == 4
    assert meta["enabled"] is False


def test_retrieval_usefulness_is_labeled_as_heuristic():
    cfg = {"usefulness_min_score": .55, "usefulness_min_content_tokens": 5}
    rows = [
        {"rerank_score": .9, "content": "one two three four five six seven"},
        {"rerank_score": .8, "content": "one two three four five six seven"},
        {"rerank_score": .7, "content": "one two three four five six seven"},
    ]
    out = retrieval_usefulness(rows, cfg)
    assert out["score"] >= .75
    assert out["label"] == "high"
    assert "not answer correctness" in out["note"]


def test_token_cost_estimate_is_explicitly_local_and_estimated():
    out = token_cost_estimate(
        "hello world",
        [{"content": "x" * 400}, {"content": "y" * 400}],
        [{"content": "z" * 800}],
        {"enabled": True, "max_chars_per_candidate": 200},
    )
    assert out["provider"] == "local"
    assert out["currency_cost_usd"] == 0.0
    assert out["estimated"] is True
    assert out["total_model_input_tokens"] > 0
    assert out["returned_context_tokens"] > 0


def test_selection_explanation_preserves_retrieval_and_final_rank():
    rows = [{"score": .8, "rerank_score": .9, "retrieval_rank": 3}]
    annotate_selection(rows, hybrid=False, expanded=False)
    assert rows[0]["selection"]["final_rank"] == 1
    assert rows[0]["selection"]["retrieval_rank"] == 3
    assert rows[0]["selection"]["method"] == "dense"
    assert "reranked" in rows[0]["selection"]["reason"]

from app.search import analyze_query_intent, decompose_query


def test_query_intent_forces_hybrid_for_technical_tokens_and_book_titles():
    cfg = {"hybrid_query_aware_enabled": True, "hybrid_metadata_terms_max": 4}
    bm25 = analyze_query_intent(
        "Why is BM25 useful for IDs and domain-specific terms?", cfg
    )
    assert bm25["force_hybrid"] is True
    assert "technical_token" in bm25["reasons"]

    title = analyze_query_intent(
        "In RAG with Python Cookbook, what is reciprocal rank fusion?", cfg
    )
    assert title["force_hybrid"] is True
    assert any("RAG" in term for term in title["metadata_terms"])

    concept = analyze_query_intent(
        "What problem does the Strategy pattern solve?", cfg
    )
    assert concept["force_hybrid"] is True
    assert "named_concept" in concept["reasons"]


def test_decompose_query_only_splits_explicit_compound_comparisons():
    cfg = {"decomposition_enabled": True, "decomposition_max_subqueries": 2}
    out = decompose_query(
        "Compare the Strategy pattern with an Aggregate Root: what problem does each solve?",
        cfg,
    )
    assert out["enabled"] is True
    assert out["reason"] == "explicit_comparison"
    assert len(out["subqueries"]) == 2
    assert "Strategy pattern" in out["subqueries"][0]
    assert "Aggregate Root" in out["subqueries"][1]

    simple = decompose_query("What is an Aggregate Root?", cfg)
    assert simple == {"enabled": False, "reason": "simple_query", "subqueries": []}


def test_query_aware_lexical_runs_even_when_dense_is_strong():
    cfg = {
        "hybrid_enabled": True,
        "hybrid_query_aware_enabled": True,
        "hybrid_metadata_terms_max": 4,
        "hybrid_min_dense_score": .55,
        "hybrid_min_dense_candidates": 8,
    }
    dense = [{"score": .9} for _ in range(8)]
    intent = analyze_query_intent("What does RRF do in hybrid retrieval?", cfg)
    assert should_apply_lexical("What does RRF do in hybrid retrieval?", dense, 8, cfg, intent=intent) == (
        True, "query_lexical_intent"
    )


def test_adaptive_top_k_can_treat_explicit_top_k_as_ceiling():
    cfg = {
        "adaptive_top_k_enabled": True,
        "adaptive_top_k_allow_below_explicit": True,
        "adaptive_top_k_min": 2,
        "adaptive_top_k_max": 8,
        "adaptive_top_k_min_score": .55,
        "adaptive_top_k_gap": .18,
    }
    rows = [
        {"rerank_score": .97},
        {"rerank_score": .91},
        {"rerank_score": .70},
        {"rerank_score": .30},
        {"rerank_score": .20},
    ]
    chosen, meta = choose_adaptive_top_k(rows, 20, cfg, explicit=True)
    assert chosen == 2
    assert meta["enabled"] is True

import pytest
import app.search as search_module


@pytest.mark.asyncio
async def test_semantic_search_decomposition_recovers_missing_side_of_comparison(monkeypatch):
    class FakeSettings:
        async def effective(self):
            return {
                "search": {
                    "top_k": 3,
                    "max_top_k": 20,
                    "min_score": 0.0,
                    "hybrid_enabled": False,
                    "decomposition_enabled": True,
                    "decomposition_max_subqueries": 2,
                    "decomposition_candidates_per_query": 3,
                    "adaptive_enabled": False,
                    "adaptive_top_k_enabled": False,
                    "expand_heading_context": False,
                    "fusion_rank_constant": 60,
                },
                "rerank": {"enabled": False},
            }

        async def lmstudio_client(self):
            return FakeClient()

        async def get(self, key, default=None):
            return default

    class FakeClient:
        async def list_models(self):
            return [{"id": "embed", "state": "loaded"}]

        async def unload_model(self, model):
            return {"status": "ok"}

        async def ensure_model_loaded(self, model, *, context_length=None):
            return {"instance_id": model}

    def row(chunk_id, document, score):
        return {
            "chunk_id": chunk_id,
            "document_id": f"doc-{chunk_id}",
            "filename": document,
            "source_path": document,
            "meta": {},
            "collection": "Computer Books",
            "score": score,
            "content": f"evidence from {document}",
            "loc": {},
            "seq": 0,
        }

    async def fake_resolve(collections, filters):
        return [{
            "id": "collection-id",
            "name": "Computer Books",
            "embedding_model": "embed",
            "embedding_dim": 3,
            "default_search": True,
        }]

    async def fake_search_group(*, query, **kwargs):
        if query.startswith("Compare "):
            rows = [
                row("strategy", "Head First Design Patterns.epub", .92),
                row("noise", "Other Book.epub", .85),
            ]
        elif "Strategy pattern" in query:
            rows = [row("strategy", "Head First Design Patterns.epub", .95)]
        elif "Aggregate Root" in query:
            rows = [row("aggregate", "Domain-Driven Design.epub", .96)]
        else:
            rows = []
        return rows, 1.0, 1.0

    async def fake_record(event):
        return 123

    monkeypatch.setattr(search_module, "settings", FakeSettings())
    monkeypatch.setattr(search_module, "_resolve_collections", fake_resolve)
    monkeypatch.setattr(search_module, "_search_group", fake_search_group)
    monkeypatch.setattr(search_module, "record_retrieval", fake_record)

    out = await search_module.semantic_search(
        "Compare the Strategy pattern with an Aggregate Root: what problem does each solve?",
        top_k=3,
    )

    docs = {item["document"] for item in out["results"]}
    assert "Head First Design Patterns.epub" in docs
    assert "Domain-Driven Design.epub" in docs
    assert out["retrieval"]["decomposition"]["enabled"] is True
    aggregate = next(item for item in out["results"] if item["document"] == "Domain-Driven Design.epub")
    assert aggregate["selection"]["method"] == "decomposition"
    assert out["trace_id"] == 123
