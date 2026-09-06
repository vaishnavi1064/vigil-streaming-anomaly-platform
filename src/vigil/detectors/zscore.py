"""Rolling z-score baseline over sliding windows.

The permanent benchmark bar (FR-5, story C1) and the hot-path detector (ADR-017). It is
cheap, it is well understood, and every claim made about the foundation model is a claim
relative to this.

Two choices worth defending:

**Welford, not sum-of-squares.** The naive running variance subtracts two large, nearly
equal numbers and loses catastrophic precision on channels with a large mean and small
variance -- `bearing_temp_c` sits near 62 with a sigma near 1, and the solar feed's
`ac_voltage_v` near 500. Welford's update is numerically stable at any offset, and the cost
is a couple of extra floating-point operations per sample.

**A decayed reference, not an unbounded one.** The reference distribution is exponentially
weighted, so it tracks slow legitimate change (a plant warming through the morning) without
holding every sample ever seen. An unbounded accumulator on 336 live channels is both a
memory leak and a detector that becomes progressively deafer as its history grows.

The window under test is scored **before** it is folded into the reference. A window that
contributes to the distribution it is judged against partly hides itself, and with a long
level shift that self-masking compounds until the shift becomes the new normal.
"""

from __future__ import annotations

import math
import time

from vigil.detectors.base import DetectorScore, WindowDetector
from vigil.windows import Window


class Welford:
    """Numerically stable running mean and variance, optionally exponentially decayed.

    `decay = 1.0` is the plain cumulative estimator. Below 1.0, older observations lose
    weight geometrically, giving an effective memory of roughly `1 / (1 - decay)` samples.
    """

    __slots__ = ("count", "mean", "m2", "decay", "weight")

    def __init__(self, decay: float = 1.0) -> None:
        if not 0.0 < decay <= 1.0:
            raise ValueError(f"decay must be in (0, 1], got {decay}")
        self.decay = decay
        self.count = 0
        self.weight = 0.0
        self.mean = 0.0
        self.m2 = 0.0

    def update(self, value: float) -> None:
        self.count += 1
        if self.decay < 1.0:
            self.weight = self.weight * self.decay + 1.0
        else:
            self.weight += 1.0
        delta = value - self.mean
        self.mean += delta / self.weight
        self.m2 = self.m2 * (self.decay if self.decay < 1.0 else 1.0) + delta * (value - self.mean)

    def update_many(self, values: tuple[float, ...]) -> None:
        for v in values:
            self.update(v)

    @property
    def variance(self) -> float:
        if self.weight <= 1.0:
            return 0.0
        return max(self.m2 / (self.weight - 1.0), 0.0)

    @property
    def stddev(self) -> float:
        return math.sqrt(self.variance)


class RollingZScoreDetector(WindowDetector):
    """Scores a window by how far its mean sits from the channel's rolling distribution.

    The statistic is the absolute z-score of the window mean against the per-channel
    reference, scaled by the square root of the window's point count. A window of many
    samples has a mean whose standard error is smaller by sqrt(n), so without that scaling
    a long quiet window and a short noisy one would score the same displacement equally --
    and the detector would systematically under-react to sustained shifts, which are
    exactly the failure mode that matters operationally.

    A dispersion term is taken alongside it: a variance burst can leave the mean untouched
    while making the channel obviously unwell. The window's score is the larger of the two,
    because either is sufficient reason to look.
    """

    name = "zscore"

    def __init__(
        self,
        *,
        decay: float = 0.995,
        warmup_samples: int = 120,
        floor_sigma: float = 1e-9,
    ) -> None:
        self.decay = decay
        self.warmup_samples = warmup_samples
        self.floor_sigma = floor_sigma
        self._reference: dict[str, Welford] = {}
        self.windows_scored = 0
        self.windows_skipped_cold = 0

    def _reference_for(self, channel: str) -> Welford:
        ref = self._reference.get(channel)
        if ref is None:
            ref = Welford(decay=self.decay)
            self._reference[channel] = ref
        return ref

    def score(self, window: Window) -> DetectorScore | None:
        started = time.perf_counter()
        ref = self._reference_for(window.channel)

        if ref.count < self.warmup_samples:
            # No opinion yet. Reporting 0.0 here would let a cold start masquerade as a
            # stretch of verified-normal data in the benchmark.
            self.windows_skipped_cold += 1
            return None

        sigma = ref.stddev
        if sigma <= self.floor_sigma:
            # A channel that has not moved at all has no scale to measure against. Dividing
            # by it would turn floating-point dust into an enormous score.
            self.windows_skipped_cold += 1
            return None

        n = window.count
        window_mean = sum(window.values) / n
        mean_z = abs(window_mean - ref.mean) / sigma * math.sqrt(n)

        window_var = sum((v - window_mean) ** 2 for v in window.values) / max(n - 1, 1)
        dispersion_ratio = math.sqrt(max(window_var, 0.0)) / sigma
        # Expressed as a deviation from 1.0 so a calm window scores near zero and both an
        # unusually noisy and an unusually flat window are visible.
        dispersion_z = abs(dispersion_ratio - 1.0) * math.sqrt(n / 2.0)

        score = max(mean_z, dispersion_z)
        self.windows_scored += 1
        return DetectorScore(
            detector=self.name,
            channel=window.channel,
            window_start_ms=window.start_ms,
            window_end_ms=window.end_ms,
            score=score,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            detail={
                "mean_z": mean_z,
                "dispersion_z": dispersion_z,
                "window_mean": window_mean,
                "reference_mean": ref.mean,
                "reference_sigma": sigma,
                "points": n,
            },
        )

    def observe(self, window: Window) -> None:
        self._reference_for(window.channel).update_many(window.values)

    @property
    def channels_tracked(self) -> int:
        return len(self._reference)
