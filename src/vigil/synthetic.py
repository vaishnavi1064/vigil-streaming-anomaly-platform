"""Synthetic multi-channel sensor signal with injected, labelled anomalies.

The generator is deliberately not trivial. Noise is AR(1) rather than white, because a
white-noise series makes a rolling z-score look far better than it is: consecutive samples
are independent, so the running standard deviation is a well-behaved estimate and almost
any excursion is caught. Real sensor noise is autocorrelated, which inflates the running
sigma and is what actually makes cheap detectors miss things. Benchmarking against an easy
signal would be the dishonest kind of win.

Every value carries the label of the episode that produced it, if any. No detector reads
that field -- it exists so the evaluation harness has ground truth on a stream that is
otherwise unlabelled by construction.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class AnomalyKind(StrEnum):
    """The shape of an excursion."""

    SPIKE = "spike"
    LEVEL_SHIFT = "level_shift"
    VARIANCE_BURST = "variance_burst"


class AnomalyOrigin(StrEnum):
    """What caused an excursion -- the distinction the whole evaluation turns on.

    A FAULT is a genuine problem and must be detected whatever else is happening. DEPLOY
    and PIPELINE excursions are artifacts of an operational event; attributing those to
    their cause instead of paging is the point of conditioning. Because both kinds are
    generated and labelled separately, an evaluation can tell targeted attribution apart
    from suppressing everything inside a window (ADR-015).
    """

    FAULT = "fault"
    DEPLOY = "deploy"
    PIPELINE = "pipeline"


@dataclass(frozen=True)
class ChannelSpec:
    """The normal behaviour of one channel."""

    name: str
    base: float
    seasonal_amplitude: float
    seasonal_period_s: float
    noise_sigma: float
    # AR(1) coefficient. 0 is white noise; approaching 1 is a slow random walk.
    noise_persistence: float = 0.85


@dataclass
class InjectedEpisode:
    kind: AnomalyKind
    start_s: float
    end_s: float
    magnitude: float  # in units of the channel's noise sigma
    origin: AnomalyOrigin = AnomalyOrigin.FAULT

    def covers(self, t_s: float) -> bool:
        return self.start_s <= t_s <= self.end_s

    @property
    def is_real(self) -> bool:
        """True when this must be detected regardless of surrounding context."""
        return self.origin is AnomalyOrigin.FAULT


@dataclass(frozen=True)
class Sample:
    value: float
    injected: AnomalyKind | None
    origin: AnomalyOrigin | None = None


# Duration ranges per kind, in seconds. A spike is one sample by definition; the others
# persist long enough to span several detection windows, which is what makes them
# interesting to a windowed detector.
_DURATION_S: dict[AnomalyKind, tuple[float, float]] = {
    AnomalyKind.SPIKE: (0.0, 0.0),
    AnomalyKind.LEVEL_SHIFT: (20.0, 90.0),
    AnomalyKind.VARIANCE_BURST: (15.0, 60.0),
}

_MAGNITUDE_SIGMA: dict[AnomalyKind, tuple[float, float]] = {
    AnomalyKind.SPIKE: (6.0, 12.0),
    AnomalyKind.LEVEL_SHIFT: (3.0, 6.0),
    AnomalyKind.VARIANCE_BURST: (3.0, 6.0),
}


class ChannelSimulator:
    """Stateful sampler for one channel.

    Deterministic given the seed and the sequence of timestamps it is asked for, so tests
    and the labelled benchmark can be replayed exactly.
    """

    def __init__(
        self,
        spec: ChannelSpec,
        seed: int,
        anomalies_per_hour: float = 12.0,
        scheduled: Sequence[InjectedEpisode] = (),
    ) -> None:
        self.spec = spec
        self._rng = random.Random(seed)
        self._noise = 0.0
        self._anomalies_per_hour = anomalies_per_hour
        self._episode: InjectedEpisode | None = None
        self._next_episode_at_s: float | None = None
        # Episodes planned ahead of the run by a scenario (deploy artifacts, and faults
        # placed deliberately inside or outside deploy windows). Kept in a separate queue
        # from the Poisson stream so a scenario run is exactly reproducible from its plan.
        self._scheduled = sorted(scheduled, key=lambda e: e.start_s)
        self._scheduled_at = 0
        self.episodes: list[InjectedEpisode] = []

    def _schedule_next(self, t_s: float) -> None:
        # Poisson arrivals: exponential gaps at the configured hourly rate.
        if self._anomalies_per_hour <= 0:
            self._next_episode_at_s = math.inf
            return
        gap_s = self._rng.expovariate(self._anomalies_per_hour / 3600.0)
        self._next_episode_at_s = t_s + gap_s

    def _maybe_start_scheduled(self, t_s: float) -> InjectedEpisode | None:
        """Activate the next scheduled episode once the stream reaches its start.

        Fires on the first sample at or after `start_s`, and does not require the sample to
        fall inside the span. A spike has zero duration, so an exact-instant match would
        essentially never occur against a discrete sample grid and every scheduled spike
        would vanish -- while the plan still listed it, leaving the evaluation expecting a
        detection the data never contained. An episode present in the ground truth must be
        present in the signal.
        """
        while self._scheduled_at < len(self._scheduled):
            candidate = self._scheduled[self._scheduled_at]
            if candidate.start_s > t_s:
                return None
            self._scheduled_at += 1
            self.episodes.append(candidate)
            return candidate
        return None

    def _maybe_start_episode(self, t_s: float) -> None:
        if self._next_episode_at_s is None:
            self._schedule_next(t_s)
            return
        if t_s < self._next_episode_at_s:
            return
        kind = self._rng.choice(list(AnomalyKind))
        lo, hi = _DURATION_S[kind]
        duration = self._rng.uniform(lo, hi)
        mag_lo, mag_hi = _MAGNITUDE_SIGMA[kind]
        magnitude = self._rng.uniform(mag_lo, mag_hi)
        # Sign is random so a level shift is as likely to be a drop as a rise; a detector
        # that only looks for increases should fail here, and should be seen to fail.
        if self._rng.random() < 0.5:
            magnitude = -magnitude
        episode = InjectedEpisode(kind=kind, start_s=t_s, end_s=t_s + duration, magnitude=magnitude)
        self._episode = episode
        self.episodes.append(episode)
        self._schedule_next(t_s + duration)

    def sample(self, t_s: float) -> Sample:
        spec = self.spec
        # A scheduled episode outranks a Poisson one: the scenario's ground truth is what
        # the evaluation scores against, so it must never be displaced by chance.
        scheduled = self._maybe_start_scheduled(t_s)
        if scheduled is not None:
            # In force for this sample by construction. The expiry check below must not
            # run on it: an instantaneous spike does not cover the grid point it lands on,
            # so checking would cancel it the moment it was activated.
            self._episode = scheduled
        else:
            self._maybe_start_episode(t_s)
            if self._episode is not None and not self._episode.covers(t_s):
                self._episode = None

        active = self._episode
        sigma_scale = 1.0
        if active is not None and active.kind is AnomalyKind.VARIANCE_BURST:
            sigma_scale = abs(active.magnitude)

        # AR(1): scale the innovation so the stationary variance stays noise_sigma^2
        # regardless of the persistence, otherwise persistence silently changes the SNR.
        phi = spec.noise_persistence
        innovation = self._rng.gauss(0.0, spec.noise_sigma * math.sqrt(1.0 - phi * phi))
        self._noise = phi * self._noise + innovation

        seasonal = spec.seasonal_amplitude * math.sin(2.0 * math.pi * t_s / spec.seasonal_period_s)
        value = spec.base + seasonal + self._noise * sigma_scale

        injected: AnomalyKind | None = None
        origin: AnomalyOrigin | None = None
        if active is not None:
            injected = active.kind
            origin = active.origin
            if active.kind is AnomalyKind.SPIKE:
                value += active.magnitude * spec.noise_sigma
                self._episode = None  # a spike is exactly one sample
            elif active.kind is AnomalyKind.LEVEL_SHIFT:
                value += active.magnitude * spec.noise_sigma

        return Sample(value=value, injected=injected, origin=origin)


def default_fleet(channel_count: int, seed: int = 1729) -> list[ChannelSpec]:
    """A fleet of pump-monitoring channels with deliberately dissimilar characters.

    Channels differ in base level, seasonal depth and noise so that a single global
    threshold cannot work and per-channel state is genuinely required.
    """
    rng = random.Random(seed)
    metrics = ("vibration_mm_s", "bearing_temp_c", "discharge_pressure_bar", "flow_m3_h")
    specs: list[ChannelSpec] = []
    for i in range(channel_count):
        metric = metrics[i % len(metrics)]
        unit = i // len(metrics)
        base, amplitude, sigma = {
            "vibration_mm_s": (4.5, 0.8, 0.35),
            "bearing_temp_c": (62.0, 6.0, 0.9),
            "discharge_pressure_bar": (7.2, 0.4, 0.12),
            "flow_m3_h": (145.0, 18.0, 3.1),
        }[metric]
        specs.append(
            ChannelSpec(
                name=f"pump-{unit:02d}.{metric}",
                base=base * rng.uniform(0.92, 1.08),
                seasonal_amplitude=amplitude * rng.uniform(0.8, 1.2),
                # Compressed "daily" cycle: a few minutes, so a demo shows a full period.
                seasonal_period_s=300.0 * rng.uniform(0.85, 1.15),
                noise_sigma=sigma * rng.uniform(0.85, 1.15),
                noise_persistence=rng.uniform(0.75, 0.92),
            )
        )
    return specs
