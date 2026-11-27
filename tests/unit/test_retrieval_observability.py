import pytest

import app.retrieval_observability as ro


@pytest.mark.asyncio
async def test_record_retrieval_returns_trace_id(monkeypatch):
    class Cursor:
        async def fetchone(self):
            return {"id": 42}

    class Conn:
        async def execute(self, *args, **kwargs):
            return Cursor()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    class Pool:
        def connection(self):
            return Conn()

    class DB:
        pool = Pool()

    monkeypatch.setattr(ro, "db", DB())
    trace_id = await ro.record_retrieval({"query": "q", "top_k": 5, "min_score": 0})
    assert trace_id == 42


@pytest.mark.asyncio
async def test_record_retrieval_fault_injection_is_fail_open(monkeypatch):
    class Conn:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("injected telemetry database failure")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    class Pool:
        def connection(self):
            return Conn()

    class DB:
        pool = Pool()

    monkeypatch.setattr(ro, "db", DB())
    trace_id = await ro.record_retrieval({"query": "q", "top_k": 5, "min_score": 0})
    assert trace_id is None


def test_metrics_query_exposes_p95_and_p99_without_threshold_gate():
    source = __import__("pathlib").Path("app/retrieval_observability.py").read_text()
    assert "percentile_cont(0.95)" in source
    assert "AS p95_total_ms" in source
    assert "percentile_cont(0.99)" in source
    assert "AS p99_total_ms" in source
    assert "threshold" not in source.lower()
    assert "slo" not in source.lower()
