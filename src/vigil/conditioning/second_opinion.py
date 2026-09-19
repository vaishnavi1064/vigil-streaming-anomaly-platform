"""The second detector's opinion of an episode the first one raised.

Every conditioning signal built so far -- timing, synchrony, scope, blast radius -- can
only explain an episode by pointing at something *outside* the data: a deploy, a pipeline
disturbance. That puts a hard ceiling on what conditioning can do, and B-6 computed it: on
the v4w run, 75 of 112 false pages overlap no injected excursion at all, so no context
signal can attribute them however good the discriminator is. The detector fired on its own
noise, and nothing in the context topic knows anything about that.

This signal needs no external cause. The platform already runs two detectors on the same
windows -- a rolling z-score on the hot path, Chronos-Bolt forecast residuals off it
(ADR-017) -- and they fail differently: the z-score trips on an AR(1) excursion that stays
inside what the forecaster expected, and the forecaster misses slow drift the z-score
catches. An episode one of them raises that the other cannot see at all is more likely a
property of that detector than of the world. An episode both of them see is not.

**What this is not.** It is not an ensemble detector. The second model does not raise
episodes here and does not change which windows flag; the z-score baseline remains the only
thing that pages anyone, so every run stays comparable to v1-v4 on the same shadow pass.
What changes is whether an episode the baseline already raised survives conditioning.

**Abstention is the default.** The model is cold for its first 48 buckets on a channel, it
can be unavailable entirely, and off-path scoring may drop windows when it falls behind. In
all three cases the index has no opinion, and no opinion must never read as disagreement --
that is the same rule as ADR-007, applied to the second detector rather than to the context
source. Coverage is therefore checked explicitly and reported, so "the model disagreed" and
"the model never looked" stay distinguishable in the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vigil.detectors.base import DetectorScore


@dataclass(frozen=True, slots=True)
class SecondOpinion:
    """What the corroborating detector made of one episode's span.

    `covered` is the honest part. False means the model had no usable view of this span --
    cold, dropped or not yet caught up -- and the policy must abstain rather than read
    silence as dissent.
    """

    covered: bool
    agrees: bool
    peak_score: float
    windows: int
    reason: str

    @property
    def dissents(self) -> bool:
        return self.covered and not self.agrees


@dataclass
class SecondOpinionIndex:
    """What the corroborating detector scored, per channel, and how far it has got.

    Holds raw scores rather than a boolean per window, because the line between agreement
    and dissent is a policy threshold and belongs in `ConditioningThresholds` -- keeping
    the scores lets the same recorded run be re-read at another threshold instead of
    needing another run to answer a sensitivity question.
    """

    detector: str = ""
    # channel -> [(window_start_ms, window_end_ms, score)], in arrival order, which for a
    # detector fed by the window assigner is also event-time order per channel.
    _scored: dict[str, list[tuple[int, int, float]]] = field(default_factory=dict, repr=False)
    _progress_ms: dict[str, int] = field(default_factory=dict, repr=False)
    recorded: int = field(default=0, init=False)

    def record(self, score: DetectorScore) -> None:
        self.detector = self.detector or score.detector
        self._scored.setdefault(score.channel, []).append(
            (score.window_start_ms, score.window_end_ms, score.score)
        )
        previous = self._progress_ms.get(score.channel)
        if previous is None or score.window_end_ms > previous:
            self._progress_ms[score.channel] = score.window_end_ms
        self.recorded += 1

    def opinion(
        self, channel: str, t_start_ms: int, t_end_ms: int, agrees_at: float
    ) -> SecondOpinion:
        """Did the second detector also find this span anomalous?

        Agreement is concluded from a single window over the line, because an episode is a
        run of flagged windows and the baseline's own peak is what raised it -- requiring
        the second detector to clear the bar on *every* window would be asking it to agree
        about the episode's shape rather than about its existence.

        Dissent needs more than the absence of agreement: the model must have scored a
        window overlapping the span **and** have passed the span's end on this channel, so
        a low score means "looked and saw nothing" rather than "has not got there yet".
        """
        scored = self._scored.get(channel)
        if not scored:
            return SecondOpinion(
                covered=False,
                agrees=False,
                peak_score=0.0,
                windows=0,
                reason=f"{self.detector or 'the second detector'} has not scored {channel}",
            )
        overlapping = [s for start, end, s in scored if start <= t_end_ms and t_start_ms <= end]
        peak = max(overlapping, default=0.0)
        if overlapping and peak >= agrees_at:
            return SecondOpinion(
                covered=True,
                agrees=True,
                peak_score=peak,
                windows=len(overlapping),
                reason=(
                    f"{self.detector} independently scored this span {peak:.1f} "
                    f"(agreement at {agrees_at:g}), so both detectors saw it"
                ),
            )
        progress = self._progress_ms.get(channel)
        if not overlapping or progress is None or progress < t_end_ms:
            return SecondOpinion(
                covered=False,
                agrees=False,
                peak_score=peak,
                windows=len(overlapping),
                reason=(
                    f"{self.detector} has no complete view of this span on {channel} "
                    f"({len(overlapping)} windows scored, progress "
                    f"{'none' if progress is None else progress})"
                ),
            )
        return SecondOpinion(
            covered=True,
            agrees=False,
            peak_score=peak,
            windows=len(overlapping),
            reason=(
                f"{self.detector} scored the same {len(overlapping)} windows and peaked at "
                f"{peak:.1f}, below the {agrees_at:g} agreement line"
            ),
        )

    def progress_ms(self, idle_ms: int = 0) -> int | None:
        """Event time through which every channel has a second opinion.

        The minimum across channels, for the same reason the fleet watermark is
        (`SlidingWindowAssigner.fleet_watermark_ms`): a decision that consults the second
        detector is racing it unless the slowest channel has passed the span in question.
        `idle_ms` excludes a channel that has fallen further than that behind the fastest,
        so one channel the model cannot score does not hold every verdict still.
        """
        marks = list(self._progress_ms.values())
        if not marks:
            return None
        if idle_ms > 0:
            cutoff = max(marks) - idle_ms
            live = [m for m in marks if m >= cutoff]
            if live:
                marks = live
        return min(marks)

    def evict_before(self, t_ms: int) -> None:
        for channel in list(self._scored):
            kept = [row for row in self._scored[channel] if row[1] >= t_ms]
            if kept:
                self._scored[channel] = kept
            else:
                del self._scored[channel]

    @property
    def channels(self) -> int:
        return len(self._scored)
