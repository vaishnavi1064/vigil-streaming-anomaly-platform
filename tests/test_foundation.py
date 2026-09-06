"""Tests for the zero-shot foundation-model detector.

The bucketing, context-slicing and degradation logic are tested without the model, because
they are ours and they are where the subtle bugs live. The tests that need real weights are
marked `slow` -- they still run by default, since chronos-bolt-tiny loads in under a second
and they are what actually verifies the model discriminates at all. Deselect them with
`-m 'not slow'` when there is no network to fetch weights.
"""

import numpy as np
import pytest

from vigil.detectors.foundation import ChronosResidualDetector, FoundationModelUnavailable
from vigil.windows import Window


def win(channel: str, start_ms: int, values, size_ms: int = 30_000) -> Window:
    n = len(values)
    return Window(
        channel=channel,
        start_ms=start_ms,
        end_ms=start_ms + size_ms,
        values=tuple(float(v) for v in values),
        event_ts_ms=tuple(start_ms + int(i * size_ms / n) for i in range(n)),
        injected=(None,) * n,
    )


# --------------------- bucketing and context, no model needed ---------------------


def test_readings_are_bucketed_to_a_fixed_cadence():
    # A window at 600 events/s holds thousands of points; the model wants an evenly spaced
    # series of a length it was trained on.
    d = ChronosResidualDetector(bucket_ms=1_000)
    buckets = d._bucketise(win("c", 0, [1.0] * 3000))
    assert len(buckets) == 30
    assert [idx for idx, _ in buckets] == list(range(30))


def test_a_bucket_is_the_mean_of_the_readings_inside_it():
    d = ChronosResidualDetector(bucket_ms=10_000)
    w = Window(
        channel="c",
        start_ms=0,
        end_ms=30_000,
        values=(1.0, 3.0, 100.0, 200.0),
        event_ts_ms=(0, 5_000, 10_000, 15_000),
        injected=(None,) * 4,
    )
    assert d._bucketise(w) == [(0, 2.0), (1, 150.0)]


def test_context_length_means_a_span_of_time_not_a_number_of_readings():
    # Otherwise the model's view would silently shrink as the ingest rate rose.
    d = ChronosResidualDetector(bucket_ms=1_000, min_context_buckets=4)
    slow = ChronosResidualDetector(bucket_ms=1_000, min_context_buckets=4)
    d._absorb("c", d._bucketise(win("c", 0, [5.0] * 3000)))
    slow._absorb("c", slow._bucketise(win("c", 0, [5.0] * 60)))
    assert len(d._history["c"].buckets) == len(slow._history["c"].buckets) == 30


def test_a_cold_channel_yields_no_context():
    d = ChronosResidualDetector(min_context_buckets=48)
    d._absorb("c", d._bucketise(win("c", 0, [1.0] * 100)))
    assert d._context_for(win("c", 30_000, [1.0] * 100)) is None


def test_context_stops_strictly_before_the_window_being_scored():
    # This is the leakage guard. Windows overlap by 20s at the default geometry, so the
    # history legitimately runs past the next window's start; feeding those buckets in
    # would ask the model to forecast data it had already been shown.
    d = ChronosResidualDetector(bucket_ms=1_000, min_context_buckets=4, context_buckets=128)
    for start in range(0, 200_000, 10_000):
        d._absorb("c", [(start // 1000 + i, float(start // 1000 + i)) for i in range(30)])
    context = d._context_for(win("c", 150_000, [0.0] * 30))
    assert context is not None
    assert max(context) < 150.0, "context leaked into the window under test"
    assert max(context) == 149.0


def test_overlapping_windows_do_not_duplicate_buckets():
    d = ChronosResidualDetector(bucket_ms=1_000, min_context_buckets=4)
    for start in (0, 10_000, 20_000, 30_000):
        d._absorb("c", d._bucketise(win("c", start, [1.0] * 300)))
    indices = [idx for idx, _ in d._history["c"].buckets]
    assert indices == sorted(set(indices))
    assert indices == list(range(60))


def test_history_is_bounded_so_per_channel_state_cannot_grow_without_limit():
    d = ChronosResidualDetector(bucket_ms=1_000, context_buckets=32)
    for start in range(0, 600_000, 10_000):
        d._absorb("c", d._bucketise(win("c", start, [1.0] * 300)))
    assert len(d._history["c"].buckets) == 64  # context_buckets * 2 headroom


def test_the_slice_still_yields_a_full_context_despite_the_bound():
    # The deque keeps headroom precisely so that slicing away the overlap does not starve
    # the context.
    d = ChronosResidualDetector(bucket_ms=1_000, context_buckets=32, min_context_buckets=32)
    for start in range(0, 600_000, 10_000):
        d._absorb("c", d._bucketise(win("c", start, [float(start)] * 300)))
    context = d._context_for(win("c", 600_000, [0.0] * 300))
    assert context is not None
    assert len(context) == 32


def test_each_channel_keeps_its_own_history():
    d = ChronosResidualDetector(bucket_ms=1_000, min_context_buckets=4)
    d._absorb("a", d._bucketise(win("a", 0, [1.0] * 300)))
    d._absorb("b", d._bucketise(win("b", 0, [2.0] * 300)))
    assert d.channels_tracked == 2
    assert d._history["a"].buckets[0][1] == 1.0
    assert d._history["b"].buckets[0][1] == 2.0


def test_an_unloadable_model_reports_a_degradation_rather_than_crashing():
    # ARCHITECTURE.md section 7 promises the platform falls back to the baseline when the
    # model path is unavailable. That has to be a handled condition, not a traceback.
    d = ChronosResidualDetector("amazon/definitely-not-a-real-checkpoint-xyz")
    with pytest.raises(FoundationModelUnavailable):
        d.load()
    assert not d.loaded


# --------------------- with real weights ---------------------


@pytest.fixture(scope="module")
def model():
    d = ChronosResidualDetector(bucket_ms=1_000, min_context_buckets=32)
    try:
        d.load()
    except FoundationModelUnavailable as exc:
        pytest.skip(f"foundation model unavailable: {exc}")
    return d


def warm(d, channel="c", seed=0, until_ms=200_000):
    rng = np.random.default_rng(seed)
    for start in range(0, until_ms, 10_000):
        d.score(win(channel, start, rng.normal(10, 1, 300)))
    return rng


@pytest.mark.slow
def test_a_normal_window_scores_low_and_a_shifted_one_scores_high(model):
    rng = warm(model, channel="discriminate")
    normal = model.score(win("discriminate", 200_000, rng.normal(10, 1, 300)))
    shifted = model.score(win("discriminate", 210_000, rng.normal(16, 1, 300)))
    assert normal is not None and shifted is not None
    assert shifted.score > 3 * normal.score


@pytest.mark.slow
def test_a_variance_burst_scores_above_a_calm_window(model):
    rng = warm(model, channel="burst", seed=3)
    calm = model.score(win("burst", 200_000, rng.normal(10, 1, 300)))
    burst = model.score(win("burst", 210_000, rng.normal(10, 6, 300)))
    assert burst.score > calm.score


@pytest.mark.slow
def test_the_score_carries_the_evidence_and_the_batch_it_came_from(model):
    warm(model, channel="evidence", seed=5)
    s = model.score(win("evidence", 200_000, [10.0] * 300))
    assert s.detector == "chronos-bolt-tiny"
    assert s.channel == "evidence"
    assert s.window_start_ms == 200_000
    assert set(s.detail) >= {
        "peak_residual_sigma",
        "mean_residual_sigma",
        "horizon_buckets",
        "context_buckets",
        "batch_size",
        "batch_ms",
    }
    assert s.detail["context_buckets"] <= model.context_buckets


@pytest.mark.slow
def test_batching_amortises_the_cost_per_window(model):
    # Batching is what makes this detector affordable at all; if a batch ever cost the same
    # per window as a single call, the off-path design would be pointless.
    warm(model, channel="batch-a", seed=7)
    warm(model, channel="batch-b", seed=8)
    single = model.score(win("batch-a", 200_000, [10.0] * 300))
    batch = model.score_batch(
        [win("batch-b", 200_000 + i * 10_000, [10.0] * 300) for i in range(16)]
    )
    scored = [s for s in batch if s is not None]
    assert scored
    assert scored[0].latency_ms < single.latency_ms


@pytest.mark.slow
def test_a_cold_channel_is_skipped_rather_than_scored(model):
    assert model.score(win("brand-new-channel", 0, [1.0] * 300)) is None


@pytest.mark.slow
def test_scoring_a_batch_returns_one_slot_per_input_window(model):
    warm(model, channel="slots", seed=9)
    windows = [win("slots", 200_000, [10.0] * 300), win("never-seen", 0, [1.0] * 300)]
    results = model.score_batch(windows)
    assert len(results) == 2
    assert results[0] is not None
    assert results[1] is None
