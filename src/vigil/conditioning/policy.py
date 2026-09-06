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

    @property
    def paged(self) -> bool:
        return self.status is EpisodeStatus.REAL

    def __repr__(self) -> str:
        return f"Attribution({self.status}, {self.verdict}, {self.attributed_to or '-'})"


class FlaggedWindowIndex:
    """Which channels were flagged in which window. The corroboration evidence.

    Deliberately kept as a plain index rather than derived on demand: the policy asks "were
    this channel's in-scope siblings also flagged at that moment", and answering that from
    storage per episode would put a query on the decision path.
    """

    def __init__(self, window_ms: int = 30_000) -> None:
        self.window_ms = window_ms
        self._by_window: dict[int, set[str]] = {}

    def record(self, channel: str, t_start_ms: int, t_end_ms: int) -> None:
        for start in self._windows_spanning(t_start_ms, t_end_ms):
            self._by_window.setdefault(start, set()).add(channel)

    def flagged_in(self, t_start_ms: int, t_end_ms: int) -> set[str]:
        out: set[str] = set()
        for start in self._windows_spanning(t_start_ms, t_end_ms):
            out |= self._by_window.get(start, set())
        return out

    def _windows_spanning(self, t_start_ms: int, t_end_ms: int) -> list[int]:
        first = (t_start_ms // self.window_ms) * self.window_ms
        last = (t_end_ms // self.window_ms) * self.window_ms
        return list(range(first, last + self.window_ms, self.window_ms))

    def evict_before(self, t_ms: int) -> None:
        for start in [s for s in self._by_window if s + self.window_ms < t_ms]:
            del self._by_window[start]

    def __len__(self) -> int:
        return len(self._by_window)


@dataclass
class ConditioningThresholds:
    """Where the policy draws its lines.

    `min_corroborating_channels` is 2 rather than 1 on purpose: one channel is the episode
    itself, and treating that as its own corroboration would collapse the policy straight
    back into blanket suppression.
    """

    min_corroborating_channels: int = 2
    min_scope_fraction: float = 0.25
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
        for event in sorted(lookup.events, key=lambda e: (e.kind is ContextKind.PIPELINE)):
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
        """The corroboration test, which is where the discrimination happens."""
        scope = set(event.scope)
        flagged = self.index.flagged_in(episode.t_start_ms, episode.t_end_ms)
        siblings = (flagged & scope) if scope else flagged
        siblings.discard(episode.channel)
        corroborating = len(siblings) + 1
        scope_size = len(scope) if scope else corroborating

        # A scope of one offers no corroboration at all: there are no siblings whose calm
        # could exonerate the channel, and none whose movement could implicate it. The
        # honest answer is "cannot tell", and the fail-open principle says that raises.
        # Attributing here would also hand anyone a trivial way to suppress everything --
        # declare every deploy single-channel.
        enough_channels = corroborating >= self.thresholds.min_corroborating_channels
        enough_fraction = corroborating / scope_size >= self.thresholds.min_scope_fraction

        if enough_channels and enough_fraction:
            return self._attribute(
                event,
                (
                    f"{corroborating} of {scope_size} channels {event.event_id} touched "
                    f"moved together in this window, which is what a change to them looks "
                    f"like"
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
                f"{episode.channel} moved alone while the other {scope_size - 1} channels "
                f"{event.event_id} touched stayed calm; a change affecting all of them "
                f"would not single one out"
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
        )

    def _record(self, attribution: Attribution) -> Attribution:
        self.verdicts[str(attribution.verdict)] = (
            self.verdicts.get(str(attribution.verdict), 0) + 1
        )
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
        return " | ".join(parts) + (f" | {breakdown}" if breakdown else "")


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
