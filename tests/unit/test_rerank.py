import math
import pytest

from app.rerank import (
    RerankError,
    parse_rerank_response,
    qwen_yes_probability,
    rerank_results,
    select_rerank_strategy,
)


class FakeClient:
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    async def chat(self, messages, model, **kwargs):
        self.calls.append({"kind": "chat", "messages": messages, "model": model, **kwargs})
        return self.response


class FakeQwenClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat_completion(self, messages, model, **kwargs):
        self.calls.append({"kind": "chat_completion", "messages": messages, "model": model, **kwargs})
        return self.responses.pop(0)


def _results():
    return [
        {"chunk_id": "a", "content": "alpha", "score": 0.90},
        {"chunk_id": "b", "content": "beta", "score": 0.80},
        {"chunk_id": "c", "content": "gamma", "score": 0.70},
    ]


def _choice(token, token_logprob, top):
    return {
        "choices": [{
            "message": {"content": token, "reasoning_content": ""},
            "logprobs": {
                "content": [{
                    "token": token,
                    "logprob": token_logprob,
                    "top_logprobs": [
                        {"token": t, "logprob": lp} for t, lp in top
                    ],
                }]
            },
            "finish_reason": "length",
        }]
    }


def test_parse_rerank_response_accepts_json_and_fenced_json():
    raw = """```json
    [{"chunk_id":"b","score":0.95},{"chunk_id":"a","score":0.4}]
    ```"""
    parsed = parse_rerank_response(raw)
    assert parsed == {"b": 0.95, "a": 0.4}


def test_parse_rerank_response_rejects_invalid_scores():
    with pytest.raises(RerankError):
        parse_rerank_response('[{"chunk_id":"a","score":4.2}]')


@pytest.mark.parametrize(
    "model",
    [
        "qwen3-reranker-0.6b",
        "qwen3-reranker-4b",
        "publisher/Qwen3-Reranker-4B-Q4_K_M",
    ],
)
def test_select_rerank_strategy_uses_qwen_binary_for_qwen3_rerankers(model):
    assert select_rerank_strategy(model) == "qwen_binary"


def test_select_rerank_strategy_keeps_generic_json_for_gemma():
    assert select_rerank_strategy("generic-reranker-model") == "generic_json"


def test_qwen_yes_probability_handles_yes_variants():
    payload = _choice(
        "Yes",
        -0.71,
        [("Yes", -0.71), ("yes", -0.70), ("NO", -5.4)],
    )
    score = qwen_yes_probability(payload)
    assert 0.98 <= score <= 1.0


def test_qwen_yes_probability_handles_no_when_yes_is_not_in_top_logprobs():
    payload = _choice(
        "No",
        -0.2172,
        [("No", -0.2172), ("NO", -5.5872), ("no", -6.2766), ("Other", -3.0)],
    )
    score = qwen_yes_probability(payload)
    assert 0.0 <= score < 0.25


@pytest.mark.asyncio
async def test_rerank_results_reorders_and_preserves_vector_score():
    client = FakeClient('[{"chunk_id":"b","score":0.99},{"chunk_id":"a","score":0.5},{"chunk_id":"c","score":0.1}]')
    config = {
        "model": "small-reranker",
        "temperature": 0.0,
        "max_tokens": 500,
        "timeout": 30,
        "max_chars_per_candidate": 100,
    }

    out = await rerank_results("which is best?", _results(), config, client)

    assert [r["chunk_id"] for r in out] == ["b", "a", "c"]
    assert out[0]["score"] == 0.80
    assert out[0]["rerank_score"] == 0.99
    assert client.calls[0]["model"] == "small-reranker"


@pytest.mark.asyncio
async def test_qwen_reranker_uses_official_binary_protocol_and_logprobs():
    client = FakeQwenClient([
        _choice("No", -0.1, [("No", -0.1), ("no", -4.0)]),
        _choice("Yes", -0.05, [("Yes", -0.05), ("yes", -4.0)]),
        _choice("No", -0.2, [("No", -0.2), ("no", -5.0)]),
    ])
    config = {
        "model": "qwen3-reranker-0.6b",
        "timeout": 30,
        "max_chars_per_candidate": 100,
        "qwen_parallelism": 1,
        "qwen_instruction": "Retrieve passages that directly answer the query.",
    }

    out = await rerank_results("which is best?", _results(), config, client)

    assert [r["chunk_id"] for r in out] == ["b", "a", "c"]
    assert out[0]["rerank_score"] > out[1]["rerank_score"] == out[2]["rerank_score"] == 0.0
    assert len(client.calls) == 3
    call = client.calls[0]
    assert call["model"] == "qwen3-reranker-0.6b"
    assert call["max_tokens"] == 1
    assert call["logprobs"] is True
    assert call["top_logprobs"] == 20
    assert call["reasoning_effort"] == "none"
    assert call["messages"][0]["role"] == "system"
    assert "only be \"yes\" or \"no\"" in call["messages"][0]["content"]
    assert "<Query>:" in call["messages"][1]["content"]
    assert "<Document>:" in call["messages"][1]["content"]


@pytest.mark.asyncio
async def test_rerank_results_requires_model():
    with pytest.raises(RerankError, match="modelo"):
        await rerank_results("q", _results(), {"model": None}, FakeClient("[]"))



def test_select_rerank_strategy_requires_rank_backend_for_qwen3_8b():
    assert select_rerank_strategy("qwen3-reranker-8b") == "qwen_rank_backend_required"


@pytest.mark.asyncio
async def test_qwen_8b_fails_fast_when_rank_backend_is_unavailable():
    with pytest.raises(RerankError, match="RANK pooling"):
        await rerank_results(
            "query",
            [{"chunk_id": "a", "content": "relevant", "score": 0.5}],
            {"model": "qwen3-reranker-8b"},
            FakeClient("unused"),
        )


def test_qwen_no_only_top_logprobs_do_not_turn_residual_mass_into_yes():
    payload = _choice(
        "No",
        -1.2,
        [("No", -1.2), ("Other", -1.4)],
    )
    assert qwen_yes_probability(payload) == 0.0
