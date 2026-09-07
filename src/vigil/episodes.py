"""Episodes: consecutive flagged windows on a channel, merged into one incident.

A detector emits a score per window, and windows overlap. A single 90-second level shift
therefore produces a run of flagged windows, and treating each as a separate finding would
be wrong twice over: an operator is paged once per incident, and counting each window
separately lets a chatty detector inflate both its false-positive count and any apparent
reduction in it. Merging here is what makes the evaluation's incident-level counting
possible at all (ADR-016).

An episode stays open while flagged windows keep arriving on the channel, and closes after
a quiet gap. Closing on a gap rather than on a fixed duration matters: a real fault has no
maximum length, and a detector that chopped a twenty-minute outage into forty tidy
half-minute incidents would be describing its own window size, not the world.

The status field is where conditioning will land in Phase 3. Until then every episode is
`real`, which is the correct unconditioned default and exactly what the shadow baseline
needs to be.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from vigil.detectors.base import DetectorScore


class EpisodeStatus(StrEnum):
    """What the platform concluded about an episode.

    REAL is the unconditioned default. ATTRIBUTED means a context signal explains it, so it
    is recorded but not paged -- deliberately not called 'suppressed', because the episode
    still exists and is still queryable; only the paging decision changed. SUPPRESSED is
    reserved for an operator's explicit dismissal.
    """

    REAL = "real"
    ATTRIBUTED = "attributed"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class ScoreSample:
    """One detector's opinion of one window inside an episode."""

    detector: str
    window_start_ms: int
    window_end_ms: int
    score: float
    latency_ms: float
    # The detector's own working -- which term drove the score, what the reference was.
    # Carried so the agent's diagnoser can characterise an episode from evidence rather than
    # re-deriving it from raw values it cannot see. Not persisted: it is per-window detail
    # for in-process reasoning, and storing it would put detector internals in the schema.
    detail: dict = field(default_factory=dict)
    # Event time of the reading that drove the score. None when the detector cannot name one.
    # Last in the field order deliberately: several call sites construct a ScoreSample
    # positionally, and inserting a field ahead of `detail` silently rebinds their arguments.
    onset_ms: int | None = None


@dataclass
class Episode:
    """A merged run of flagged windows on one channel."""

    channel: str
    t_start_ms: int
    t_end_ms: int
    raised_by: str
    peak_score: float
    window_count: int
    threshold: float
    # When this channel actually departed, as opposed to which window noticed. `t_start_ms`
    # is a window boundary and is therefore quantised to the slide: at a 10 s slide the only
    # differences expressible between two episodes' starts are 0, 10, 20 ... seconds. Asking
    # whether two channels moved together needs finer resolution than the geometry provides,
    # which is exactly what the v1 and v2 conditioning measurements ran into (B-4).
    #
    # Taken from the window that *opened* the episode, not from the whole episode: later
    # windows of a sustained excursion are departed from their first sample, so their own
    # onsets sit on window boundaries and would drag this back to the quantised value.
    onset_ms: int | None = None
    scores: list[ScoreSample] = field(default_factory=list)
    status: EpisodeStatus = EpisodeStatus.REAL
    attributed_to: str | None = None
    explanation: str | None = None
    # Ground truth carried through from the synthetic source for the evaluation harness.
    # Never consulted by any detection or conditioning logic.
    injected_origins: tuple[str, ...] = ()

    @property
    def duration_ms(self) -> int:
        return self.t_end_ms - self.t_start_ms

    @property
    def began_ms(self) -> int:
        """The best available start time: the true onset, or the window start if there is none.

        Callers that compare episodes in time should use this rather than `t_start_ms`, so a
        detector that cannot report an onset degrades to the old quantised behaviour instead
        of losing the comparison entirely.
        """
        return self.onset_ms if self.onset_ms is not None else self.t_start_ms

    @property
    def mean_score(self) -> float:
        return sum(s.score for s in self.scores) / len(self.scores) if self.scores else 0.0

    def __repr__(self) -> str:
        return (
            f"Episode({self.channel} [{self.t_start_ms}, {self.t_end_ms}] "
            f"peak={self.peak_score:.1f} windows={self.window_count} {self.status})"
        )


@dataclass
class _OpenEpisode:
    episode: Episode
    last_window_end_ms: int


@dataclass
class EpisodeBuilder:
    """Merges flagged windows into episodes, one open episode per channel.

    `threshold` is the score above which a window is flagged. `merge_gap_ms` is how long a
    channel must stay quiet before an open episode is considered over; it defaults to two
    window slides, so a single unflagged window in the middle of a genuine event does not
    split it in two.

    `on_open` fires the moment a channel's first flagged window arrives, before the episode
    is complete. Conditioning uses it to record *when* a channel departed as soon as that is
    known, rather than when the episode eventually closes -- an episode that runs for two
    minutes is evidence about its first second, and withholding it until the end is what put
    the corroboration index behind the question it was being asked (G-7).
    """

    threshold: float = 8.0
    merge_gap_ms: int = 20_000
    on_open: Callable[[Episode], None] | None = None

    _open: dict[str, _OpenEpisode] = field(default_factory=dict, init=False, repr=False)
    windows_flagged: int = field(default=0, init=False)
    episodes_opened: int = field(default=0, init=False)
    episodes_emitted: int = field(default=0, init=False)

    def add(
        self, score: DetectorScore, injected_origins: tuple[str | None, ...] = ()
    ) -> Episode | None:
        """Feed one window score. Returns an episode if this closed one.

        A below-threshold window is not ignored: it is what advances the quiet gap that
        eventually closes an open episode.
        """
        channel = score.channel
        open_ep = self._open.get(channel)

        if score.score < self.threshold:
            if open_ep and score.window_start_ms - open_ep.last_window_end_ms >= self.merge_gap_ms:
                return self._close(channel)
            return None

        self.windows_flagged += 1
        sample = ScoreSample(
            detector=score.detector,
            window_start_ms=score.window_start_ms,
            window_end_ms=score.window_end_ms,
            score=score.score,
            latency_ms=score.latency_ms,
            onset_ms=score.onset_ms,
            detail=dict(score.detail),
        )
        origins = tuple(o for o in injected_origins if o is not None)

        if (
            open_ep is None
            or score.window_start_ms - open_ep.last_window_end_ms >= self.merge_gap_ms
        ):
            closed = self._close(channel) if open_ep else None
            opened = Episode(
                channel=channel,
                t_start_ms=score.window_start_ms,
                t_end_ms=score.window_end_ms,
                raised_by=score.detector,
                peak_score=score.score,
                window_count=1,
                threshold=self.threshold,
                onset_ms=score.onset_ms,
                scores=[sample],
                injected_origins=tuple(dict.fromkeys(origins)),
            )
            self._open[channel] = _OpenEpisode(
                episode=opened, last_window_end_ms=score.window_end_ms
            )
            self.episodes_opened += 1
            if self.on_open is not None:
                self.on_open(opened)
            return closed

        ep = open_ep.episode
        ep.t_end_ms = max(ep.t_end_ms, score.window_end_ms)
        ep.window_count += 1
        ep.scores.append(sample)
        if score.score > ep.peak_score:
            ep.peak_score = score.score
            ep.raised_by = score.detector
        if origins:
            ep.injected_origins = tuple(dict.fromkeys(ep.injected_origins + origins))
        open_ep.last_window_end_ms = max(open_ep.last_window_end_ms, score.window_end_ms)
        return None

    def _close(self, channel: str) -> Episode | None:
        open_ep = self._open.pop(channel, None)
        if open_ep is None:
            return None
        self.episodes_emitted += 1
        return open_ep.episode

    def close_all(self) -> Iterator[Episode]:
        """Flush every open episode. For shutdown and bounded runs."""
        for channel in list(self._open):
            episode = self._close(channel)
            if episode is not None:
                yield episode

    @property
    def open_count(self) -> int:
        return len(self._open)
