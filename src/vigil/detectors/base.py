"""The detector contract.

One interface for every detector -- the cheap hot-path baseline, the foundation model that
runs batched off the critical path, and anything the benchmark harness compares them
against. Uniformity is what makes the comparison in docs/EVALUATION.md fair: the same
driver, the same windows, the same timing instrumentation.

A detector returns a **score**, not a verdict. Thresholding is a separate decision, and
conditioning is a later one still (Phase 3). Keeping scoring free of both is what lets the
benchmark compute threshold-independent measures at all -- a detector that only emitted
booleans could not produce a PR curve.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from vigil.windows import Window


@dataclass(frozen=True, slots=True)
class DetectorScore:
    """One detector's opinion of one window.

    `score` is unbounded and non-negative, higher meaning more anomalous. Its scale is the
    detector's own; never compare raw scores across detectors, compare them at a matched
    alarm budget.
    """

    detector: str
    channel: str
    window_start_ms: int
    window_end_ms: int
    score: float
    # Wall time spent scoring this window. Recorded per score rather than aggregated, so
    # the latency budget in NFR-1 can be reported as a percentile over real work rather
    # than as an average that hides the tail.
    latency_ms: float = 0.0
    # Event time of the reading that actually drove this score, not the window boundary.
    # The boundary is quantised to the slide, which makes it useless for asking whether two
    # channels moved *together*: at a 10 s slide the only gaps expressible between two
    # window starts are 0, 10, 20 ... seconds, so any tolerance finer than a slide collapses
    # to "same bucket". Detectors that cannot identify a driving sample leave it None.
    onset_ms: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class WindowDetector(ABC):
    """Scores closed windows. Stateful across windows of the same channel is allowed."""

    name: str

    @abstractmethod
    def score(self, window: Window) -> DetectorScore | None:
        """Score one window, or return None if there is not enough history yet.

        Returning None is meaningfully different from returning zero: "I have no opinion"
        must not be recorded as "I looked and found nothing wrong", or a cold start would
        silently count as a stretch of verified-normal data in the benchmark.
        """

    def observe(self, window: Window) -> None:
        """Fold a window into the detector's reference state.

        Separate from `score` because order matters: a window must be scored against the
        history *before* it, otherwise the window contributes to the distribution it is
        being judged against and every anomaly partly hides itself.
        """
        return None
