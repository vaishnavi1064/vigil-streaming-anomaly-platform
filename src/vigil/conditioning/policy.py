"""The conditioning policy: deciding whether an episode is real or explained by context.

This is the core contribution, and the whole difficulty is in one place. The naive rule --
"an anomaly overlapping a deploy window is a deploy artifact" -- is blanket suppression
wearing a lab coat. It posts an excellent false-positive reduction and destroys recall,
because a real fault that happens during a deploy is still a real fault. The adversarial
generator (ADR-015) exists specifically to catch that policy, and it does; the measured
comparison is in `docs/EVALUATION.md`.

So the policy has to *discriminate*, and the discriminator has to be something a detector
can actually observe. Three pieces of evidence:

**1. Scope.** A deploy touched a named set of channels. An excursion on a channel the deploy
never touched cannot be explained by it. This is cheap and it removes a large class of
over-suppression outright.

**2. Corroboration across scope.** This is the one that does the work. A deploy that
perturbs telemetry perturbs *the channels it touched* -- a restart blips the whole batch, a
config change shifts the whole fleet it applied to. A real fault is a property of one
device: a bearing does not fail because a collector was redeployed. So an excursion that is
isolated -- alone among its in-scope siblings, which are calm -- is real, however
suspiciously well-timed. An excursion accompanied by its siblings is an artifact.

**3. Plausibility of the mechanism.** A pipeline disturbance can only explain an excursion
if it actually disturbed the data: a health record showing zero loss, zero duplication and
low lag cannot account for a value anomaly, whatever else it says. Attributing to a signal
that could not have caused the effect is superstition, not conditioning.

**Fail-open (ADR-007).** If the context source cannot answer, the episode is raised
unconditioned. Missing context must never hide a real anomaly. This is the one rule that is
not a heuristic -- it is a safety property, and it is asserted directly in the tests.

Every decision carries its reason. An operator dismissing an alert deserves to know why the
system thought it was explainable, and an evaluation that cannot see the reasoning cannot
tell a right answer from a lucky one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from vigil.conditioning.signals import ContextSignalSource, SignalWindow
from vigil.context import ContextEvent, ContextKind, Severity
from vigil.episodes import Episode, EpisodeStatus


class Verdict(StrEnum):
    """Why the policy decided what it did. Recorded on every episode."""

    NO_CONTEXT = "no_context"
    FAIL_OPEN = "fail_open"
    OUT_OF_SCOPE = "out_of_scope"
    ISOLATED = "isolated"
    IMPLAUSIBLE = "implausible"
    CORROBORATED = "corroborated"
    PIPELINE_DISTURBED = "pipeline_disturbed"


@dataclass(frozen=True)
class Attribution:
    """The policy's decision about one episode."""

    status: EpisodeStatus
    verdict: Verdict
    attributed_to: str | None
    reason: str
    corroborating_channels: int = 0
    scope_size: int = 0
    # The event itself, not just its id. An episode's `attributed_to` is a foreign key into
    # context_events, so whoever persists the episode has to be able to persist the event it
    # points at -- returning only the id left the caller with a reference it could not
    # satisfy, and the database correctly refused the write.
    event: ContextEvent | None = None

    @property
    def paged(self) -> bool:
        return self.status is EpisodeStatus.REAL

    def __repr__(self) -> str:
        return f"Attribution({self.status}, {self.verdict}, {self.attributed_to or '-'})"


class FlaggedWindowIndex:
    """When each channel was flagged. The corroboration evidence.

    Records episode **start times**, not just which window bucket they fell in. That
    distinction is the difference between a working discriminator and a broken one, and it
    was found by measurement rather than reasoning: the first version asked only "were this
    channel's in-scope siblings also flagged in this window", and at realistic anomaly
    density the answer was almost always yes by coincidence. Measured over 900 s with 12
    channels, that version returned `isolated` zero times out of 79 episodes and suppressed
    every real fault inside a quiet deploy window (docs/EVALUATION.md, v1).

    Synchrony is what actually separates the two cases. A deploy artifact hits the channels
    it touched at the same instant -- a collector restart blips them together. Two
    independent faults landing in the same 30-second bucket are not synchronised to the
    second. So corroboration asks whether siblings started *together*, within a tolerance
    far tighter than a window.
    """

    def __init__(self, window_ms: int = 30_000, synchrony_ms: int = 5_000) -> None:
        self.window_ms = window_ms
        self.synchrony_ms = synchrony_ms
        # channel -> sorted start times. Bounded by eviction, not by count, because the
        # useful horizon is "as long as a context event can last", not a fixed depth.
        self._starts: dict[str, list[int]] = {}

    def record(self, channel: str, t_start_ms: int, t_end_ms: int) -> None:
        """Record when a channel was flagged.

        `t_start_ms` should be the episode's **onset** (`Episode.began_ms`), not its window
        start. Passing a window start still works and is what the loose criterion uses, but
        it caps the resolution of every synchrony question at the window slide: with a 10 s
        slide, two episodes are either in the same bucket or at least 10 s apart, so a
        tolerance below the slide can only ever mean "identical bucket". That is what v1 and
        v2 of the conditioning measurement actually asked (B-4).
        """
        starts = self._starts.setdefault(channel, [])
        starts.append(t_start_ms)
        if len(starts) > 1 and starts[-1] < starts[-2]:
            starts.sort()

    def synchronous_with(self, t_start_ms: int, tolerance_ms: int | None = None) -> set[str]:
        """Channels whose episode began within `tolerance_ms` of this one."""
        tolerance = self.synchrony_ms if tolerance_ms is None else tolerance_ms
        out: set[str] = set()
        for channel, starts in self._starts.items():
            if any(abs(start - t_start_ms) <= tolerance for start in starts):
                out.add(channel)
        return out

    def flagged_in(self, t_start_ms: int, t_end_ms: int) -> set[str]:
        """Channels flagged anywhere in this span. Retained for the loose comparison.

        Kept so the strict and loose criteria can be measured against each other rather
        than one silently replacing the other.
        """
        out: set[str] = set()
        for channel, starts in self._starts.items():
            if any(t_start_ms <= start <= t_end_ms for start in starts):
                out.add(channel)
        return out

    def evict_before(self, t_ms: int) -> None:
        for channel in list(self._starts):
            kept = [s for s in self._starts[channel] if s >= t_ms]
            if kept:
                self._starts[channel] = kept
            else:
                del self._starts[channel]

    def __len__(self) -> int:
        """Channels tracked."""
        return len(self._starts)

    @property
    def total_recorded(self) -> int:
        """Episode starts held. This is what eviction actually bounds."""
        return sum(len(v) for v in self._starts.values())


@dataclass
class ConditioningThresholds:
    """Where the policy draws its lines.

    `min_corroborating_channels` is 2 rather than 1 on purpose: one channel is the episode
    itself, and treating that as its own corroboration would collapse the policy straight
    back into blanket suppression.
    """

    min_corroborating_channels: int = 2
    min_scope_fraction: float = 0.25
    # How close together two channels must move to count as moving *together*. Far tighter
    # than a window on purpose: at a 30 s window the criterion was satisfied by coincidence
    # and stopped discriminating entirely (docs/EVALUATION.md, v1).
    synchrony_ms: int = 5_000
    # A pipeline event with no loss, no duplication and only mild lag has no mechanism by
    # which it could have distorted a value, so it explains nothing.
    pipeline_requires_disturbance: bool = True
    pipeline_min_severity: Severity = Severity.WARNING


@dataclass
class ConditioningPolicy:
    """Decides `real` or `attributed` for an episode, and records why."""

    source: ContextSignalSource
    index: FlaggedWindowIndex = field(default_factory=FlaggedWindowIndex)
    thresholds: ConditioningThresholds = field(default_factory=ConditioningThresholds)

    decided: int = field(default=0, init=False)
    attributed: int = field(default=0, init=False)
    failed_open: int = field(default=0, init=False)
    verdicts: dict[str, int] = field(default_factory=dict, init=False)
    # How much evidence each scoped decision actually had. G-7 is the claim that these were
    # near-zero not because channels moved alone but because the siblings had not arrived,
    # so the two populations have to be counted separately: siblings that moved *with* the
    # episode (synchronous) and siblings that moved anywhere in its span (present at all).
    synchronous_siblings: list[int] = field(default_factory=list, init=False)
    siblings_in_span: list[int] = field(default_factory=list, init=False)

    def decide(self, episode: Episode) -> Attribution:
        self.decided += 1
        lookup = self.source.signals_for(
            SignalWindow(episode.channel, episode.t_start_ms, episode.t_end_ms)
        )

        if not lookup.available:
            # ADR-007. Not a heuristic: a signal outage must never hide a real anomaly.
            self.failed_open += 1
            return self._record(
                Attribution(
                    status=EpisodeStatus.REAL,
                    verdict=Verdict.FAIL_OPEN,
                    attributed_to=None,
                    reason=(
                        f"context unavailable ({lookup.source}: {lookup.reason}); "
                        f"raised unconditioned"
                    ),
                )
            )

        if not lookup.events:
            return self._record(
                Attribution(
                    status=EpisodeStatus.REAL,
                    verdict=Verdict.NO_CONTEXT,
                    attributed_to=None,
                    reason="no operational context overlaps this episode",
                )
            )

        # More than one event can overlap. Take the strongest explanation available, and
        # keep the best rejection so the reason is informative when nothing explains it.
        best_rejection: Attribution | None = None
        for event in sorted(lookup.events, key=lambda e: e.kind is ContextKind.PIPELINE):
            attribution = self._consider(episode, event)
            if attribution.status is EpisodeStatus.ATTRIBUTED:
                return self._record(attribution)
            if best_rejection is None or attribution.verdict is not Verdict.OUT_OF_SCOPE:
                best_rejection = attribution

        return self._record(best_rejection)

    def _consider(self, episode: Episode, event: ContextEvent) -> Attribution:
        if not event.applies_to(episode.channel):
            return Attribution(
                status=EpisodeStatus.REAL,
                verdict=Verdict.OUT_OF_SCOPE,
                attributed_to=None,
                reason=(
                    f"{event.event_id} did not touch {episode.channel}, so it cannot "
                    f"explain an excursion there"
                ),
                scope_size=len(event.scope),
            )

        if event.kind is ContextKind.PIPELINE:
            return self._consider_pipeline(episode, event)
        return self._consider_scoped(episode, event)

    def _consider_pipeline(self, episode: Episode, event: ContextEvent) -> Attribution:
        """A pipeline disturbance is fleet-wide, so scope carries no information here.

        What matters instead is whether it actually disturbed anything. The harness puts the
        counts in the event's detail, and a record showing no loss and no duplication has no
        mechanism by which it could have distorted a reading.
        """
        if not self.thresholds.pipeline_requires_disturbance:
            return self._attribute(event, "pipeline event overlaps", Verdict.PIPELINE_DISTURBED)

        disturbed = _pipeline_actually_disturbed(event, self.thresholds.pipeline_min_severity)
        if disturbed:
            return self._attribute(
                event,
                f"pipeline disturbance overlapping this window ({event.detail})",
                Verdict.PIPELINE_DISTURBED,
            )
        return Attribution(
            status=EpisodeStatus.REAL,
            verdict=Verdict.IMPLAUSIBLE,
            attributed_to=None,
            reason=(
                f"{event.event_id} overlaps but shows no loss, duplication or serious lag, "
                f"so it offers no mechanism for this excursion"
            ),
        )

    def _consider_scoped(self, episode: Episode, event: ContextEvent) -> Attribution:
        """The corroboration test, which is where the discrimination happens.

        Asks whether the channel's in-scope siblings moved *at the same moment*, not merely
        somewhere in the same window. The looser version was measured and did not
        discriminate at all.
        """
        scope = set(event.scope)
        # Compared on the onset, not the window start: a tolerance finer than the window
        # slide is meaningless against quantised boundaries (B-4).
        synchronous = self.index.synchronous_with(episode.began_ms, self.thresholds.synchrony_ms)
        siblings = (synchronous & scope) if scope else synchronous
        siblings.discard(episode.channel)
        corroborating = len(siblings) + 1
        scope_size = len(scope) if scope else corroborating

        # Recorded whatever the verdict: "no sibling moved with it" and "no sibling had
        # arrived yet" are different findings and v1-v3 could not tell them apart.
        in_span = self.index.flagged_in(episode.t_start_ms, episode.t_end_ms)
        in_span = (in_span & scope) if scope else in_span
        in_span.discard(episode.channel)
        self.synchronous_siblings.append(len(siblings))
        self.siblings_in_span.append(len(in_span))

        enough_channels = corroborating >= self.thresholds.min_corroborating_channels
        enough_fraction = corroborating / scope_size >= self.thresholds.min_scope_fraction

        if enough_channels and enough_fraction:
            return self._attribute(
                event,
                (
                    f"{corroborating} of the {scope_size} channels {event.event_id} touched "
                    f"moved within {self.thresholds.synchrony_ms / 1000:g}s of each other, "
                    f"which is what a change to all of them looks like"
                ),
                Verdict.CORROBORATED,
                corroborating=corroborating,
                scope_size=scope_size,
            )

        if scope_size <= 1:
            reason = (
                f"{event.event_id} touched only {episode.channel}, so nothing can corroborate "
                f"or exonerate it; with no evidence either way the episode is raised"
            )
        else:
            reason = (
                f"{episode.channel} moved alone -- none of the other {scope_size - 1} "
                f"channels {event.event_id} touched moved within "
                f"{self.thresholds.synchrony_ms / 1000:g}s of it; a change affecting all of "
                f"them would not single one out"
            )
        return Attribution(
            status=EpisodeStatus.REAL,
            verdict=Verdict.ISOLATED,
            attributed_to=None,
            reason=reason,
            corroborating_channels=corroborating,
            scope_size=scope_size,
        )

    def _attribute(
        self,
        event: ContextEvent,
        reason: str,
        verdict: Verdict,
        corroborating: int = 0,
        scope_size: int = 0,
    ) -> Attribution:
        return Attribution(
            status=EpisodeStatus.ATTRIBUTED,
            verdict=verdict,
            attributed_to=event.event_id,
            reason=reason,
            corroborating_channels=corroborating,
            scope_size=scope_size,
            event=event,
        )

    def _record(self, attribution: Attribution) -> Attribution:
        self.verdicts[str(attribution.verdict)] = self.verdicts.get(str(attribution.verdict), 0) + 1
        if attribution.status is EpisodeStatus.ATTRIBUTED:
            self.attributed += 1
        return attribution

    def apply(self, episode: Episode) -> Attribution:
        """Decide, and write the decision onto the episode."""
        attribution = self.decide(episode)
        episode.status = attribution.status
        episode.attributed_to = attribution.attributed_to
        return attribution

    def summary(self) -> str:
        parts = [
            f"decided {self.decided:,}",
            f"attributed {self.attributed:,}",
            f"raised {self.decided - self.attributed:,}",
        ]
        if self.failed_open:
            parts.append(f"failed open {self.failed_open:,}")
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(self.verdicts.items()))
        line = " | ".join(parts) + (f" | {breakdown}" if breakdown else "")
        if not self.synchronous_siblings:
            return line
        return f"{line}\n  {self.evidence_report()}"

    def evidence_report(self) -> str:
        """How much corroboration evidence the scoped decisions actually had.

        This is the G-7 measurement. A corroboration test that never fires is ambiguous
        between "channels really do move alone here" and "the siblings had not arrived
        yet", and only the second is a bug. Reporting both populations makes the two
        distinguishable in every future run rather than reconstructible from a diagnosis.
        """
        n = len(self.synchronous_siblings)
        if not n:
            return "no scoped decisions"
        sync_any = sum(1 for c in self.synchronous_siblings if c)
        span_any = sum(1 for c in self.siblings_in_span if c)
        return (
            f"corroboration evidence over {n} scoped decisions: "
            f"in-scope siblings synchronous mean {sum(self.synchronous_siblings) / n:.2f} "
            f"({sync_any} decisions with >=1) | "
            f"present anywhere in the episode span mean "
            f"{sum(self.siblings_in_span) / n:.2f} ({span_any} decisions with >=1)"
        )


_SEVERITY_ORDER = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.CRITICAL: 2,
}


def _pipeline_actually_disturbed(event: ContextEvent, min_severity: Severity) -> bool:
    """Did this health record describe a disturbance that could distort a value?

    Parsed from the detail string the harness writes. Parsing beats trusting severity alone:
    a window can be graded on lag while having lost nothing, and lag delays a reading rather
    than changing it.
    """
    fields = _parse_detail(event.detail)
    if fields.get("missing", 0) > 0 or fields.get("duplicates", 0) > 0:
        return True
    if fields.get("reordered", 0) > 0:
        return True
    return _SEVERITY_ORDER.get(event.severity, 0) >= _SEVERITY_ORDER.get(min_severity, 1) and (
        # Severity alone only counts when the detail did not say otherwise: an event from a
        # source that publishes no counts is taken at its word.
        "missing" not in fields
    )


def _parse_detail(detail: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for token in detail.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        try:
            out[key] = int(value)
        except ValueError:
            continue
    return out
