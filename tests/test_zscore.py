import math
import random
import statistics

import pytest

from vigil.detectors.zscore import RollingZScoreDetector, Welford
from vigil.windows import Window


def win(channel: str, values, start_ms: int = 0, size_ms: int = 10_000) -> Window:
    n = len(values)
    step = size_ms // max(n, 1)
    return Window(
        channel=channel,
        start_ms=start_ms,
        end_ms=start_ms + size_ms,
        values=tuple(float(v) for v in values),
        event_ts_ms=tuple(start_ms + i * step for i in range(n)),
        injected=(None,) * n,
    )


# --------------------------- Welford ---------------------------


def test_welford_matches_the_textbook_mean_and_variance():
    values = [4.0, 7.0, 13.0, 16.0, 2.5, 9.25]
    w = Welford()
    w.update_many(tuple(values))
    assert w.mean == pytest.approx(statistics.fmean(values))
    assert w.variance == pytest.approx(statistics.variance(values))


def test_welford_stays_accurate_at_a_large_offset_where_the_naive_formula_collapses():
    # The sum-of-squares estimator subtracts two nearly equal large numbers here and loses
    # most of its significant digits. Channels like bearing_temp_c (mean 62, sigma 1) and
    # the feed's ac_voltage_v (mean ~500) live in exactly this regime.
    rng = random.Random(4)
    offset = 1e9
    noise = [rng.gauss(0.0, 1.0) for _ in range(20_000)]
    w = Welford()
    w.update_many(tuple(offset + x for x in noise))
    assert w.variance == pytest.approx(statistics.variance(noise), rel=0.02)

    naive_mean = sum(offset + x for x in noise) / len(noise)
    naive_var = sum((offset + x) ** 2 for x in noise) / len(noise) - naive_mean**2
    assert abs(naive_var - statistics.variance(noise)) > abs(
        w.variance - statistics.variance(noise)
    )


def test_a_decayed_reference_tracks_a_moving_level():
    w = Welford(decay=0.99)
    w.update_many(tuple(10.0 for _ in range(2000)))
    w.update_many(tuple(50.0 for _ in range(2000)))
    assert w.mean == pytest.approx(50.0, abs=1.0)


def test_an_undecayed_reference_remembers_everything():
    w = Welford(decay=1.0)
    w.update_many(tuple(10.0 for _ in range(2000)))
    w.update_many(tuple(50.0 for _ in range(2000)))
    assert w.mean == pytest.approx(30.0, abs=0.1)


def test_variance_is_never_negative_under_decay():
    w = Welford(decay=0.9)
    for v in (5.0, 5.0, 5.0, 5.0):
        w.update(v)
        assert w.variance >= 0.0


def test_a_nonsense_decay_is_rejected_at_construction():
    for bad in (0.0, -0.5, 1.5):
        with pytest.raises(ValueError, match="decay"):
            Welford(decay=bad)


# --------------------------- detector ---------------------------


def warm(detector: RollingZScoreDetector, channel: str, sigma=1.0, mean=10.0, n=40, seed=1):
    rng = random.Random(seed)
    for i in range(n):
        detector.observe(
            win(channel, [rng.gauss(mean, sigma) for _ in range(20)], start_ms=i * 10_000)
        )


def test_a_cold_channel_gets_no_opinion_rather_than_a_zero():
    # Scoring 0.0 during warmup would let a cold start masquerade as verified-normal data.
    d = RollingZScoreDetector(warmup_samples=200)
    assert d.score(win("c", [1.0] * 20)) is None
    assert d.windows_skipped_cold == 1
    assert d.windows_scored == 0


def test_a_normal_window_scores_low_once_warm():
    d = RollingZScoreDetector(warmup_samples=100)
    warm(d, "c")
    rng = random.Random(99)
    scores = [d.score(win("c", [rng.gauss(10.0, 1.0) for _ in range(20)])).score for _ in range(30)]
    assert statistics.median(scores) < 3.0


def test_a_shifted_window_scores_far_above_a_normal_one():
    d = RollingZScoreDetector(warmup_samples=100)
    warm(d, "c")
    rng = random.Random(7)
    normal = d.score(win("c", [rng.gauss(10.0, 1.0) for _ in range(20)])).score
    shifted = d.score(win("c", [rng.gauss(15.0, 1.0) for _ in range(20)])).score
    assert shifted > 5 * normal


def test_a_downward_shift_is_caught_as_readily_as_an_upward_one():
    # The synthetic generator emits both directions; a detector that only looks up would
    # silently miss half of them.
    d = RollingZScoreDetector(warmup_samples=100)
    warm(d, "c")
    rng = random.Random(8)
    up = d.score(win("c", [rng.gauss(15.0, 1.0) for _ in range(20)])).score
    down = d.score(win("c", [rng.gauss(5.0, 1.0) for _ in range(20)])).score
    assert down > 10.0
    assert down == pytest.approx(up, rel=0.4)


def test_a_variance_burst_is_caught_even_when_the_mean_does_not_move():
    # A mean-only statistic reports nothing here, yet the channel is obviously unwell.
    d = RollingZScoreDetector(warmup_samples=100)
    warm(d, "c")
    rng = random.Random(11)
    calm = d.score(win("c", [rng.gauss(10.0, 1.0) for _ in range(40)]))
    burst = d.score(win("c", [rng.gauss(10.0, 6.0) for _ in range(40)]))
    assert burst.detail["dispersion_z"] > 5 * calm.detail["dispersion_z"]
    assert burst.score > calm.score


def test_a_sustained_shift_scores_higher_than_a_brief_one_of_the_same_size():
    # sqrt(n) scaling: without it the detector under-reacts to sustained shifts, which are
    # the operationally important case.
    d = RollingZScoreDetector(warmup_samples=100)
    warm(d, "c")
    brief = d.score(win("c", [13.0] * 8)).score
    sustained = d.score(win("c", [13.0] * 64)).score
    assert sustained > brief


def test_the_window_is_scored_before_it_joins_the_reference():
    # Otherwise a window contributes to the distribution judging it and partly hides itself.
    d = RollingZScoreDetector(warmup_samples=100)
    warm(d, "c")
    anomalous = win("c", [40.0] * 20)
    first = d.score(anomalous).score
    d.observe(anomalous)
    for _ in range(20):
        d.observe(anomalous)
    later = d.score(anomalous).score
    assert first > later, "folding the window in first would let it mask itself"


def test_a_flat_channel_yields_no_opinion_rather_than_an_enormous_score():
    # Dividing by a zero scale turns floating-point dust into a huge z.
    d = RollingZScoreDetector(warmup_samples=10)
    for i in range(30):
        d.observe(win("flat", [7.0] * 20, start_ms=i * 10_000))
    assert d.score(win("flat", [7.0000001] * 20)) is None


def test_each_channel_keeps_its_own_reference():
    d = RollingZScoreDetector(warmup_samples=100)
    warm(d, "cool", mean=10.0, sigma=1.0)
    warm(d, "hot", mean=500.0, sigma=20.0, seed=2)
    assert d.channels_tracked == 2
    # 500 is catastrophic on the cool channel and entirely normal on the hot one.
    assert d.score(win("cool", [500.0] * 20)).score > 50.0
    assert d.score(win("hot", [500.0] * 20)).score < 10.0


def test_the_score_carries_the_evidence_behind_it():
    d = RollingZScoreDetector(warmup_samples=100)
    warm(d, "c")
    s = d.score(win("c", [14.0] * 20, start_ms=123_000))
    assert s.detector == "zscore"
    assert s.channel == "c"
    assert s.window_start_ms == 123_000
    assert s.window_end_ms == 133_000
    assert s.detail["points"] == 20
    assert s.detail["window_mean"] == pytest.approx(14.0)
    assert s.detail["reference_mean"] == pytest.approx(10.0, abs=0.6)
    assert set(s.detail) >= {"mean_z", "dispersion_z", "reference_sigma"}


def test_latency_is_recorded_per_window_not_averaged_away():
    d = RollingZScoreDetector(warmup_samples=100)
    warm(d, "c")
    s = d.score(win("c", [11.0] * 20))
    assert s.latency_ms >= 0.0
    assert s.latency_ms < 250.0, "the hot-path budget is 250 ms p99 for the whole window"


def test_scores_are_finite_for_every_plausible_window():
    d = RollingZScoreDetector(warmup_samples=100)
    warm(d, "c")
    for values in (
        [10.0] * 20,
        [1e6] * 20,
        [-1e6] * 20,
        [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
    ):
        s = d.score(win("c", values))
        assert s is not None
        assert math.isfinite(s.score)
        assert s.score >= 0.0
