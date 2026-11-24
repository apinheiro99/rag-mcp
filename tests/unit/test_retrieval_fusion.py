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
    assert should_apply_lexical(dense,8,cfg)==(False,"dense_sufficient")


def test_lexical_fallback_runs_when_dense_is_weak_or_short():
    cfg={"hybrid_enabled":True,"hybrid_min_dense_score":.55,"hybrid_min_dense_candidates":8}
    assert should_apply_lexical([{"score":.4} for _ in range(8)],8,cfg)==(True,None)
    assert should_apply_lexical([{"score":.9} for _ in range(3)],8,cfg)==(True,None)
