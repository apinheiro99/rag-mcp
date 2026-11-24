from scripts.stress_rag import percentile


def test_percentile_boundaries():
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 0.0) == 10
    assert percentile(values, 0.5) == 30
    assert percentile(values, 0.99) == 50


def test_percentile_empty():
    assert percentile([], 0.95) == 0.0
