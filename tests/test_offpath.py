"""Tests for running a detector off the critical path.

A fake detector is used deliberately here: the subject is the worker's batching, bounded
queue and failure isolation, not any model's opinions. Driving it with real weights would
make the tests slow and would test the wrong thing.
"""

import threading
import time

from vigil.detectors.base import DetectorScore
from vigil.detectors.offpath import OffPathScorer
from vigil.windows import Window


def win(channel: str = "c", start_ms: int = 0) -> Window:
    return Window(
        channel=channel,
        start_ms=start_ms,
        end_ms=start_ms + 30_000,
        values=(1.0, 2.0, 3.0),
        event_ts_ms=(start_ms, start_ms + 1, start_ms + 2),
        injected=(None, None, None),
    )


class FakeDetector:
    """Records the batch sizes it was handed and scores everything."""

    name = "fake"

    def __init__(self, delay_s: float = 0.0, fail: bool = False):
        self.delay_s = delay_s
        self.fail = fail
        self.batch_sizes: list[int] = []
        self.lock = threading.Lock()

    def score_batch(self, windows):
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("model exploded")
        with self.lock:
            self.batch_sizes.append(len(windows))
        return [
            DetectorScore(
                detector=self.name,
                channel=w.channel,
                window_start_ms=w.start_ms,
                window_end_ms=w.end_ms,
                score=float(w.start_ms),
                latency_ms=1.0,
            )
            for w in windows
        ]


class Collector:
    def __init__(self):
        self.scores = []
        self.lock = threading.Lock()

    def __call__(self, scores):
        with self.lock:
            self.scores.extend(scores)

    def wait_for(self, n: int, timeout_s: float = 10.0) -> bool:
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            with self.lock:
                if len(self.scores) >= n:
                    return True
            time.sleep(0.01)
        return False


def test_submitted_windows_come_back_scored():
    detector, collector = FakeDetector(), Collector()
    scorer = OffPathScorer(detector, collector, max_batch=8, max_wait_s=0.05)
    scorer.start()
    for i in range(20):
        assert scorer.submit(win(start_ms=i * 10_000))
    assert collector.wait_for(20)
    scorer.stop()
    assert scorer.scored == 20
    assert scorer.dropped == 0


def test_windows_are_batched_rather_than_scored_one_at_a_time():
    # Batching is the entire economic argument for this path: measured on this CPU a single
    # window costs 5.5 ms and a batch of 32 costs 20.5 ms.
    detector, collector = FakeDetector(), Collector()
    scorer = OffPathScorer(detector, collector, max_batch=16, max_wait_s=0.5)
    scorer.start()
    for i in range(64):
        scorer.submit(win(start_ms=i * 10_000))
    assert collector.wait_for(64)
    scorer.stop()
    assert max(detector.batch_sizes) > 1


def test_a_batch_never_exceeds_the_configured_maximum():
    detector, collector = FakeDetector(), Collector()
    scorer = OffPathScorer(detector, collector, max_batch=4, max_wait_s=0.5)
    scorer.start()
    for i in range(40):
        scorer.submit(win(start_ms=i * 10_000))
    assert collector.wait_for(40)
    scorer.stop()
    assert max(detector.batch_sizes) <= 4


def test_a_lone_window_is_still_scored_after_the_wait_expires():
    # Otherwise a quiet stream would leave its last window unscored indefinitely.
    detector, collector = FakeDetector(), Collector()
    scorer = OffPathScorer(detector, collector, max_batch=32, max_wait_s=0.05)
    scorer.start()
    scorer.submit(win())
    assert collector.wait_for(1, timeout_s=5.0)
    scorer.stop()


def test_submitting_never_blocks_the_caller_even_when_the_worker_is_slow():
    # The hot path must not wait on the model. This is the property the whole design rests
    # on, so it is asserted on the clock rather than assumed.
    detector, collector = FakeDetector(delay_s=0.3), Collector()
    scorer = OffPathScorer(detector, collector, max_batch=1, max_wait_s=0.05, queue_size=4)
    scorer.start()
    started = time.perf_counter()
    for i in range(200):
        scorer.submit(win(start_ms=i * 10_000))
    elapsed = time.perf_counter() - started
    scorer.stop(timeout_s=2.0)
    assert elapsed < 1.0, f"submitting blocked for {elapsed:.2f}s"


def test_a_full_queue_drops_and_counts_rather_than_growing_without_limit():
    # An unbounded queue would trade a latency problem for an out-of-memory one. A dropped
    # window is a measurement the model did not make, and it is reported as exactly that.
    detector, collector = FakeDetector(delay_s=0.2), Collector()
    scorer = OffPathScorer(detector, collector, max_batch=1, max_wait_s=0.05, queue_size=3)
    scorer.start()
    for i in range(100):
        scorer.submit(win(start_ms=i * 10_000))
    scorer.stop(timeout_s=2.0)
    assert scorer.dropped > 0
    assert scorer.submitted + scorer.dropped == 100
    assert "DROPPED" in scorer.summary()


def test_a_dropped_window_is_never_counted_as_one_the_model_found_normal():
    detector, collector = FakeDetector(delay_s=0.2), Collector()
    scorer = OffPathScorer(detector, collector, max_batch=1, max_wait_s=0.05, queue_size=2)
    scorer.start()
    for i in range(60):
        scorer.submit(win(start_ms=i * 10_000))
    scorer.stop(timeout_s=2.0)
    assert scorer.scored <= scorer.submitted
    assert scorer.scored + scorer.dropped <= 60


def test_a_failing_model_is_isolated_and_reported_not_propagated():
    # If the model path could take the process down, it would not be off the critical path.
    detector, collector = FakeDetector(fail=True), Collector()
    scorer = OffPathScorer(detector, collector, max_batch=4, max_wait_s=0.05)
    scorer.start()
    for i in range(12):
        scorer.submit(win(start_ms=i * 10_000))
    time.sleep(0.5)
    scorer.stop()
    assert scorer.errors > 0
    assert "model exploded" in scorer.first_error
    assert collector.scores == []


def test_a_failing_result_handler_is_isolated_too():
    def explode(_scores):
        raise ValueError("handler exploded")

    scorer = OffPathScorer(FakeDetector(), explode, max_batch=4, max_wait_s=0.05)
    scorer.start()
    for i in range(8):
        scorer.submit(win(start_ms=i * 10_000))
    time.sleep(0.5)
    scorer.stop()
    assert scorer.errors > 0
    assert "handler exploded" in scorer.first_error


def test_stopping_drains_the_backlog_rather_than_discarding_it():
    # Otherwise the last windows of a run would be reported as never scored when they were
    # merely still queued.
    detector, collector = FakeDetector(), Collector()
    scorer = OffPathScorer(detector, collector, max_batch=4, max_wait_s=0.05)
    scorer.start()
    for i in range(50):
        scorer.submit(win(start_ms=i * 10_000))
    scorer.stop(timeout_s=10.0)
    assert scorer.scored == 50
    assert scorer.backlog == 0


def test_the_summary_stays_quiet_when_nothing_went_wrong():
    scorer = OffPathScorer(FakeDetector(), Collector(), max_batch=4)
    scorer.start()
    scorer.submit(win())
    scorer.stop()
    summary = scorer.summary()
    assert "DROPPED" not in summary
    assert "errors" not in summary
