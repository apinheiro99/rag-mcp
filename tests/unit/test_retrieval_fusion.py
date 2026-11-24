from app.search import canonical_clause, fuse_rankings
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
    assert should_apply_lexical(dense,8,cfg)==(False,"dense_sufficient")


def test_lexical_fallback_runs_when_dense_is_weak_or_short():
    cfg={"hybrid_enabled":True,"hybrid_min_dense_score":.55,"hybrid_min_dense_candidates":8}
    assert should_apply_lexical([{"score":.4} for _ in range(8)],8,cfg)==(True,None)
    assert should_apply_lexical([{"score":.9} for _ in range(3)],8,cfg)==(True,None)


def test_lexical_fallback_threshold_boundaries():
    cfg={"hybrid_enabled":True,"hybrid_min_dense_score":.55,"hybrid_min_dense_candidates":8}

    exact=[{"score":.55}]+[{"score":.1} for _ in range(7)]
    assert should_apply_lexical(exact,8,cfg)==(False,"dense_sufficient")

    just_below=[{"score":.5499}]+[{"score":.1} for _ in range(7)]
    assert should_apply_lexical(just_below,8,cfg)==(True,None)

    one_short=[{"score":.99} for _ in range(7)]
    assert should_apply_lexical(one_short,8,cfg)==(True,None)


def test_canonical_clause_hides_superseded_unless_document_is_explicit():
    from app.search_filters import SearchFilters

    assert canonical_clause(SearchFilters()) == " AND d.superseded_by IS NULL"
    assert canonical_clause(SearchFilters(document_ids=["11111111-1111-1111-1111-111111111111"])) == ""

from app.search import annotate_selection, choose_adaptive_top_k, retrieval_usefulness, token_cost_estimate


def test_adaptive_top_k_respects_relevance_curve_and_explicit_override():
    cfg = {
        "adaptive_top_k_enabled": True,
        "adaptive_top_k_min": 2,
        "adaptive_top_k_max": 5,
        "adaptive_top_k_min_score": 0.55,
        "adaptive_top_k_gap": 0.18,
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
