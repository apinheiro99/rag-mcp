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
