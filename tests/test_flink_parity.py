"""Does the Flink job compute the same thing the Python detector does?

This is the test that makes the migration checkable rather than assertable. The Flink job
cannot be imported here -- it needs PyFlink, which has no wheel for this platform -- so the
scoring arithmetic is re-implemented from the job's source and compared against
`vigil.detectors.zscore` on identical input.

That re-implementation is a real weakness and is named as such: it verifies the *algorithm*
was transcribed faithfully, not that Flink executes it correctly. Flink's execution is
verified separately, by running the job and reconciling its output against the Python
detector's on the same readings (docs/CORRECTNESS.md). A drift between this file and
`flink/scoring_job.py` would make this test pass while the job diverged, so the parity test
below reads the job's source and fails if the constants it depends on have changed.
"""

import math
import random
import re
from pathlib import Path

import pytest

from vigil.detectors.zscore import RollingZScoreDetector
from vigil.windows import Window

JOB = Path(__file__).resolve().parents[1] / "flink" / "scoring_job.py"


def flink_score(values, state, warmup: int):
    """The scoring arithmetic transcribed from WindowedZScore.on_timer."""
    n = len(values)
    window_mean = sum(values) / n
    score = None
    if state["count"] >= warmup and state["weight"] > 1.0:
        variance = max(state["m2"] / (state["weight"] - 1.0), 0.0)
        sigma = math.sqrt(variance)
        if sigma > 1e-9:
            mean_z = abs(window_mean - state["mean"]) / sigma * math.sqrt(n)
            window_var = sum((v - window_mean) ** 2 for v in values) / max(n - 1, 1)
            dispersion_z = abs(math.sqrt(max(window_var, 0.0)) / sigma - 1.0) * math.sqrt(n / 2)
            score = max(mean_z, dispersion_z)
    return score


def flink_absorb(values, state, decay: float):
    for v in values:
        state["count"] += 1
        state["weight"] = state["weight"] * decay + 1.0
        delta = v - state["mean"]
        state["mean"] += delta / state["weight"]
        state["m2"] = state["m2"] * decay + delta * (v - state["mean"])
    return state


def window(values, start_ms=0, size_ms=30_000):
    n = len(values)
    return Window(
        channel="c",
        start_ms=start_ms,
        end_ms=start_ms + size_ms,
        values=tuple(float(v) for v in values),
        event_ts_ms=tuple(start_ms + int(i * size_ms / n) for i in range(n)),
        injected=(None,) * n,
    )


def test_the_job_source_still_uses_the_constants_this_parity_test_assumes():
    # If the job's defaults drift from the detector's, the two would diverge in production
    # while this file happily kept comparing the old arithmetic to itself.
    source = JOB.read_text(encoding="utf-8")
    assert re.search(r'"--decay", type=float, default=0\.995', source)
    assert re.search(r'"--warmup-samples", type=int, default=120', source)
    assert re.search(r'"--min-points", type=int, default=8', source)
    assert re.search(r'"--window-ms", type=int, default=30_000', source)
    assert re.search(r'"--slide-ms", type=int, default=10_000', source)


def test_the_job_scores_before_folding_the_window_into_its_reference():
    # Same ordering requirement as the Python detector: a window that contributes to the
    # distribution judging it partly hides itself.
    source = JOB.read_text(encoding="utf-8")
    score_at = source.index("score = max(mean_z, dispersion_z)")
    absorb_at = source.index('state["count"] += 1')
    assert score_at < absorb_at


def test_the_job_uses_the_same_defaults_as_the_python_detector():
    detector = RollingZScoreDetector()
    source = JOB.read_text(encoding="utf-8")
    assert f"default={detector.decay}" in source
    assert f"default={detector.warmup_samples}" in source


@pytest.mark.parametrize("seed", [1, 7, 13])
def test_scores_agree_with_the_python_detector_on_identical_input(seed):
    rng = random.Random(seed)
    detector = RollingZScoreDetector(decay=0.995, warmup_samples=120)
    state = {"count": 0, "weight": 0.0, "mean": 0.0, "m2": 0.0}

    for step in range(60):
        values = [rng.gauss(10.0, 1.0) for _ in range(20)]
        w = window(values, start_ms=step * 10_000)

        theirs = flink_score(values, state, warmup=120)
        flink_absorb(values, state, decay=0.995)

        ours = detector.score(w)
        detector.observe(w)

        if ours is None:
            assert theirs is None, "the job scored a window the detector considered cold"
        else:
            assert theirs is not None
            assert theirs == pytest.approx(ours.score, rel=1e-9)


def test_both_agree_that_a_shifted_window_is_anomalous():
    rng = random.Random(3)
    detector = RollingZScoreDetector(decay=0.995, warmup_samples=120)
    state = {"count": 0, "weight": 0.0, "mean": 0.0, "m2": 0.0}
    for step in range(40):
        values = [rng.gauss(10.0, 1.0) for _ in range(20)]
        w = window(values, start_ms=step * 10_000)
        flink_absorb(values, state, decay=0.995)
        detector.observe(w)

    shifted = [rng.gauss(16.0, 1.0) for _ in range(20)]
    theirs = flink_score(shifted, state, warmup=120)
    ours = detector.score(window(shifted, start_ms=999_000))
    assert theirs > 10.0
    assert ours.score > 10.0
    assert theirs == pytest.approx(ours.score, rel=1e-9)


def test_both_withhold_a_score_during_warmup():
    state = {"count": 0, "weight": 0.0, "mean": 0.0, "m2": 0.0}
    detector = RollingZScoreDetector(warmup_samples=120)
    values = [1.0] * 20
    assert flink_score(values, state, warmup=120) is None
    assert detector.score(window(values)) is None


def test_both_withhold_a_score_on_a_channel_with_no_scale():
    state = {"count": 0, "weight": 0.0, "mean": 0.0, "m2": 0.0}
    flat = [7.0] * 20
    for _ in range(20):
        flink_absorb(flat, state, decay=0.995)
    assert flink_score(flat, state, warmup=120) is None

    detector = RollingZScoreDetector(warmup_samples=120)
    for i in range(20):
        detector.observe(window(flat, start_ms=i * 10_000))
    assert detector.score(window(flat)) is None


def test_the_job_declares_exactly_once_end_to_end():
    # Every link has to hold or the guarantee is not one: the source must not read
    # uncommitted records, the sink must write transactionally, and checkpointing must be in
    # exactly-once mode.
    source = JOB.read_text(encoding="utf-8")
    assert 'set_property("isolation.level", "read_committed")' in source
    assert "DeliveryGuarantee.EXACTLY_ONCE" in source
    assert "CheckpointingMode.EXACTLY_ONCE" in source
    assert "set_transactional_id_prefix" in source


def test_the_transaction_timeout_comfortably_exceeds_the_checkpoint_interval():
    # A transaction that times out before the checkpoint that would have committed it
    # completes is silent data loss, not an error.
    source = JOB.read_text(encoding="utf-8")
    interval = int(
        re.search(r'"--checkpoint-interval-ms", type=int, default=([0-9_]+)', source)
        .group(1)
        .replace("_", "")
    )
    timeout = int(
        re.search(r'"--transaction-timeout-ms", type=int, default=([0-9_]+)', source)
        .group(1)
        .replace("_", "")
    )
    assert timeout > interval * 10


def test_the_job_uses_rocksdb_and_incremental_checkpoints():
    # Per-channel state is one entry per channel forever; heap state would make the job's
    # memory a function of fleet size.
    source = JOB.read_text(encoding="utf-8")
    assert 'set_string("state.backend.type", "rocksdb")' in source
    assert 'set_string("state.backend.incremental", "true")' in source


def test_the_job_times_out_idle_partitions():
    # Same failure the reconciliation harness hit: one quiet partition must not hold the
    # watermark back for every other partition.
    source = JOB.read_text(encoding="utf-8")
    assert "with_idleness" in source


def test_the_job_takes_event_time_from_the_reading_not_from_arrival():
    source = JOB.read_text(encoding="utf-8")
    assert "for_bounded_out_of_orderness" in source
    assert 'json.loads(value)["ts"]' in source
