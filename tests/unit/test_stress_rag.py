from argparse import Namespace

from scripts.stress_rag import apply_profile, percentile


def test_percentile_boundaries():
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 0.0) == 10
    assert percentile(values, 0.5) == 30
    assert percentile(values, 0.99) == 50


def test_percentile_empty():
    assert percentile([], 0.95) == 0.0


def test_load_profile_applies_defaults_without_overriding_explicit_values():
    args = Namespace(profile="load", requests=None, concurrency=None, duration_seconds=None)
    apply_profile(args)
    assert args.requests == 64
    assert args.concurrency == 8
    assert args.duration_seconds == 0

    explicit = Namespace(profile="stress", requests=10, concurrency=3, duration_seconds=1)
    apply_profile(explicit)
    assert explicit.requests == 10
    assert explicit.concurrency == 3
    assert explicit.duration_seconds == 1
