"""Whether an excursion lasted, or whether the detector blinked.

The second detector (ADR-050) answers "did something else see this too". This asks a
question that needs no second opinion and no context event either: **did it last?**

A real fault is a state the channel is in. A bearing that starts running hot is hot in the
next window too, and the one after that. A z-score false positive on AR(1) noise is a
sample that dominated one window's statistics and nothing more -- the neighbouring windows
share 20 of their 30 seconds with it and do not flag. So an episode that occupies exactly
one window is a different kind of object from one that occupies four, and until now nothing
in the policy looked at the difference.

**Where the default comes from, so it is not a tuned number.** Windows are 30 s wide and
slide by 10 s, so consecutive windows overlap by 20 s. Any excursion present in the data for
one full slide is inside at least two consecutive window views. An episode flagged in
exactly one window therefore crossed the threshold in one 30 s view and failed to cross it
in the two neighbouring views built from mostly the same samples. Two windows is the line
the geometry draws, and it is where `min_persistence_windows` sits.

**Recurrence, and why it is not free.** A channel that flickers twice in quick succession is
showing a pattern rather than a coincidence, so a second departure close in time rescues the
first. "Close" has to be small: at this run's density each channel opens roughly six episodes
in 900 s, so a recurrence window of minutes is satisfied by chance and would protect
everything -- which is the v1 lesson (a coincidence test at the wrong width stops
discriminating). The default is one window width, 30 s, which is also exactly the event-time
buffer ADR-037's barrier already waits, so the forward half of the comparison is evidence
the policy is guaranteed to have.

**Shoulders are measured and not acted on.** A real excursion is often building before the
window that catches it, leaving neighbouring windows elevated but under threshold. That is a
plausible third test and it is *not* wired into the verdict; the index records it and the
report prints what it would have rescued, so turning it on is a decision someone makes from
numbers rather than a knob that appeared because the first result was disappointing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vigil.detectors.base import DetectorScore


@dataclass(frozen=True, slots=True)
class EpisodePersistence:
    """How long one episode lasted, and what stood next to it.

    `covered` is false when the index cannot answer -- the channel has no scored history at
    all. As everywhere else here, no evidence raises the episode rather than suppressing it.
    """

    covered: bool
    persistent: bool
    flagged_windows: int
    recurrences: int
    shoulder_windows: int
    reason: str

    @property
    def is_flicker(self) -> bool:
        return self.covered and not self.persistent


@dataclass
class PersistenceIndex:
    """Every window the baseline scored, per channel, flagged or not.

    Holding the unflagged windows too is the point: the question "did the neighbouring
    windows see anything" cannot be asked of a record that only keeps the ones that fired.
    Raw scores rather than booleans, for the same reason `SecondOpinionIndex` keeps them --
    a shoulder threshold can then be re-read off a recorded run instead of costing another.
    """

    detector: str = ""
    # channel -> [(window_start_ms, window_end_ms, score)] in event-time order, which is the
    # order the assigner emits them in per channel.
    _scored: dict[str, list[tuple[int, int, float]]] = field(default_factory=dict, repr=False)
    recorded: int = field(default=0, init=False)

    def record(self, score: DetectorScore) -> None:
        self.detector = self.detector or score.detector
        self._scored.setdefault(score.channel, []).append(
            (score.window_start_ms, score.window_end_ms, score.score)
        )
        self.recorded += 1

    def shoulder_before(
        self, channel: str, t_start_ms: int, threshold: float, fraction: float
    ) -> int:
        """Consecutive windows immediately before this one that were already elevated.

        Backwards only, and deliberately. The forward side would need windows that close
        after the episode does, which is data the verdict barrier does not promise for a
        one-window episode -- and a test that is available for long episodes and not for
        short ones would be measuring episode length twice.
        """
        if fraction <= 0.0:
            return 0
        scored = self._scored.get(channel)
        if not scored:
            return 0
        bar = threshold * fraction
        before = [row for row in scored if row[1] <= t_start_ms]
        count = 0
        for _start, _end, score in reversed(before):
            if score < bar:
                break
            count += 1
        return count

    def scored_windows(self, channel: str) -> int:
        return len(self._scored.get(channel, ()))

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
