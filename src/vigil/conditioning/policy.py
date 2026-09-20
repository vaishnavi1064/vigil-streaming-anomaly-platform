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

**4. The second detector's opinion.** The three above all explain an episode by pointing
at something outside the data, so none of them can touch a false page with no external
cause -- and B-6 measured that population at 75 of 112 false pages. The platform already
runs a second detector on the same windows; an episode one detector raises that the other
cannot see at all is more likely a property of that detector than of the world. This is the
only signal here that needs no context event, and it is the only one that can suppress an
episode nothing in the context topic knows anything about.

**Fail-open (ADR-007).** If the context source cannot answer, the episode is raised
unconditioned. Missing context must never hide a real anomaly. This is the one rule that is
not a heuristic -- it is a safety property, and it is asserted directly in the tests. The
second detector is held to the same rule: no opinion is never read as disagreement.

Every decision carries its reason. An operator dismissing an alert deserves to know why the
system thought it was explainable, and an evaluation that cannot see the reasoning cannot
tell a right answer from a lucky one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from vigil.conditioning.persistence import EpisodePersistence, PersistenceIndex
from vigil.conditioning.second_opinion import SecondOpinion, SecondOpinionIndex
from vigil.conditioning.signals import ContextSignalSource, SignalWindow
from vigil.context import ContextEvent, ContextKind, Severity
from vigil.episodes import Episode, EpisodeStatus
from vigil.topology import FleetTopology

# The fraction of the detection threshold a preceding window must reach to be counted as
# a shoulder *for reporting*, independently of whether the rescue is switched on. Fixed
# here rather than configurable, because a counterfactual whose own threshold moves is not
# a counterfactual -- it is a second knob wearing a report's clothes.
SHOULDER_REPORTING_FRACTION = 0.5


class Verdict(StrEnum):
    """Why the policy decided what it did. Recorded on every episode."""

    NO_CONTEXT = "no_context"
    FAIL_OPEN = "fail_open"
    OUT_OF_SCOPE = "out_of_scope"
    ISOLATED = "isolated"
    IMPLAUSIBLE = "implausible"
    CORROBORATED = "corroborated"
    PIPELINE_DISTURBED = "pipeline_disturbed"
    # The excursion is confined to one physical failure domain -- one machine, or one
    # cabinet -- which is what a fault looks like and what a ring rollout does not.
    FAULT_DOMAIN = "fault_domain"
    # The deploy's own blast radius is confined to one failure domain, so its footprint and
    # a fault's footprint are the same shape and nothing can tell them apart.
    NARROW_BLAST_RADIUS = "narrow_blast_radius"
    # The second detector scored the same windows and found nothing. No context event is
    # involved, which is the point: this is the only verdict that can answer a false page
    # with no external cause (B-6).
    SECOND_OPINION_DISSENTS = "second_opinion_dissents"
    # Both detectors saw it, so it is raised whatever the context says.
    SECOND_OPINION_AGREES = "second_opinion_agrees"
    # The excursion occupied one window and never came back. Like the two above it needs
    # no context event; unlike them it needs no second detector either (ADR-053).
    NO_PERSISTENCE = "no_persistence"
    # It lasted, so a context event is refused. Only reachable with the persistence veto
    # switched on, which is not the default.
    PERSISTENT = "persistent"


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

    def recurrences_near(self, channel: str, t_start_ms: int, within_ms: int) -> int:
        """Other departures on **this** channel within `within_ms` either side of this one.

        The episode asking the question is itself in the index, so an exact tie is its own
        entry and is excluded. Two distinct episodes on one channel cannot share an onset:
        the builder merges anything closer than its merge gap into one episode, so a second
        entry at the same instant would be the same episode counted twice.

        Symmetric in time, which is affordable only because ADR-037's barrier already holds
        a verdict until the fleet watermark has passed the onset by its buffer -- so at a
        recurrence width no larger than that buffer, the forward half is evidence the policy
        is guaranteed to have rather than evidence it happens to have.
        """
        starts = self._starts.get(channel)
        if not starts:
            return 0
        return sum(
            1 for start in starts if start != t_start_ms and abs(start - t_start_ms) <= within_ms
        )

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
    # Whether the blast-radius test runs at all. Off reproduces the timing-only policy that
    # v1-v3 measured, which is the only way those results stay repeatable.
    require_blast_radius: bool = True
    # How many distinct machines the perturbed channels must span before the excursion can
    # be a rollout rather than a machine failing. Two, because one is a fault domain; there
    # is no fraction here on purpose -- a proportion would need a threshold, and "confined
    # to one failure domain" is a fact about the graph rather than a matter of degree.
    min_blast_nodes: int = 2
    # Applied only where the fleet actually has more than one cabinet. A rack fault moves
    # several machines at once and would pass the node test; it cannot pass this one.
    require_rack_spread: bool = True

    # -- the second detector (ADR-050) --
    # Whether cross-detector agreement is allowed to change a decision at all. Off
    # reproduces the v1-v4 policy exactly, which is what keeps those results repeatable
    # and what the ablation pass runs.
    use_second_opinion: bool = False
    # The corroborating detector's score above which it counts as having seen the same
    # thing. Chronos-Bolt residuals are divided by the model's own predicted spread into
    # sigma-like units, so this is on roughly the same footing as the baseline's z-score --
    # and it is set deliberately *below* the second detector's own alarm threshold (6.0),
    # because the question here is corroboration, not independent detection. Setting it
    # low makes agreement easier, which protects recall and costs false-positive
    # reduction: the generous direction is the one that does not flatter the headline.
    second_opinion_agrees_at: float = 3.0
    # Whether agreement also overrides a context attribution. On, an episode both
    # detectors saw is raised even when a deploy could explain it -- "do not suppress
    # anything both detectors agree on", taken literally. Off, agreement only blocks
    # suppression by this signal and the context half decides alone.
    second_opinion_protects_attributed: bool = True

    # -- temporal persistence (ADR-053) --
    # Whether an excursion's duration is allowed to change a decision. Off reproduces
    # every policy before v6b, which is what the ablation pass runs.
    use_persistence: bool = False
    # Flagged windows an episode must occupy before it counts as a state the channel was
    # in rather than one window's statistics. Two, from the window geometry: consecutive
    # windows overlap by 20 of their 30 seconds, so anything present for one full slide is
    # inside two of them.
    min_persistence_windows: int = 2
    # A second departure on the same channel this close rescues a one-window episode: two
    # flickers in quick succession are a pattern. One window width, which is also the
    # verdict barrier's buffer, so the forward half of the comparison is guaranteed
    # evidence. Wider is not safer -- at this density minutes-wide recurrence is satisfied
    # by coincidence and would protect everything, which is the v1 failure. 0 disables.
    persistence_recurrence_ms: int = 30_000
    # Fraction of the detection threshold a neighbouring window must reach to count as a
    # shoulder -- an excursion that was already building. **Off by default and measured
    # anyway**: the report prints what it would have rescued at several fractions, so it
    # can be switched on from evidence rather than from disappointment.
    persistence_shoulder_fraction: float = 0.0
    # Whether persistence also overrides a context attribution. **Off**, unlike the second
    # detector's veto, and the asymmetry is deliberate: most episodes are persistent, so a
    # persistence veto would refuse nearly every context attribution and leave a policy
    # that suppresses one-window blips and does nothing else. That would replace the
    # context half rather than leave it unchanged, and it would make the ablation measure
    # the removal of v4 rather than the addition of v6b.
    persistence_protects_attributed: bool = False


@dataclass
class ConditioningPolicy:
    """Decides `real` or `attributed` for an episode, and records why."""

    source: ContextSignalSource
    index: FlaggedWindowIndex = field(default_factory=FlaggedWindowIndex)
    thresholds: ConditioningThresholds = field(default_factory=ConditioningThresholds)
    # The inventory: which machine and cabinet each channel is collected from, and which
    # deploy ring reaches it. Operational fact from the CMDB, carrying nothing about what is
    # wrong. Empty means no inventory, and then every blast-radius question answers "cannot
    # tell" -- which raises the episode, like every other absence of evidence here.
    topology: FleetTopology = field(default_factory=FleetTopology.empty)
    # What the corroborating detector scored. Present whenever the second detector is
    # running, including on the ablation pass where its verdict is recorded and ignored --
    # collecting it either way is what makes the two passes differ in the decision alone
    # and not also in when the verdict was taken.
    second_opinion: SecondOpinionIndex | None = None
    # Every window the baseline scored, flagged or not. Needed only for the shoulder
    # measurement; the duration test reads the episode's own window count, and the
    # recurrence test reads the flagged-window index that already exists.
    persistence: PersistenceIndex | None = None

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
    # Every rejection reached, not only the one that ended up on the episode. An episode
    # overlapped by both a deploy and a pipeline health record produces two conclusions and
    # can carry one, and for four published runs the one it carried was always the pipeline
    # event's -- which is why every result reports `isolated=0` (G-16).
    rejections: dict[str, int] = field(default_factory=dict, init=False)
    # What the second detector said, counted whether or not it was allowed to act. The
    # ablation pass records these and changes nothing, so the counterfactual is in the
    # run rather than reconstructed from it.
    second_opinions: dict[str, int] = field(default_factory=dict, init=False)
    # Peak second-detector score per decision, kept so the agreement line can be moved
    # after the fact and the result read off the same run instead of needing another one.
    second_opinion_peaks: list[float] = field(default_factory=list, init=False)
    second_opinion_suppressed: int = field(default=0, init=False)
    second_opinion_vetoed: int = field(default=0, init=False)
    # What the duration test saw, counted whether or not it was allowed to act.
    persistence_outcomes: dict[str, int] = field(default_factory=dict, init=False)
    persistence_windows_seen: list[int] = field(default_factory=list, init=False)
    persistence_suppressed: int = field(default=0, init=False)
    persistence_vetoed: int = field(default=0, init=False)
    # One-window episodes, and what each rescue would have saved. Recorded for every run
    # including the ablation, so a rescue can be argued for from this run's numbers.
    _flicker_recurrences: list[int] = field(default_factory=list, init=False, repr=False)
    _flicker_shoulders: list[int] = field(default_factory=list, init=False, repr=False)

    def decide(self, episode: Episode) -> Attribution:
        self.decided += 1
        attribution = self._decide_on_context(episode)
        attribution = self._reconsider_with_second_detector(episode, attribution)
        return self._record(self._reconsider_with_persistence(episode, attribution))

    def _decide_on_context(self, episode: Episode) -> Attribution:
        """Everything that explains an episode by pointing at an operational event.

        Unchanged from v4. Split out so the second detector's opinion is applied to the
        conclusion rather than woven through it -- the two signals answer different
        questions and an ablation that cannot separate them measures nothing.
        """
        lookup = self.source.signals_for(
            SignalWindow(episode.channel, episode.t_start_ms, episode.t_end_ms)
        )

        if not lookup.available:
            # ADR-007. Not a heuristic: a signal outage must never hide a real anomaly.
            self.failed_open += 1
            return Attribution(
                status=EpisodeStatus.REAL,
                verdict=Verdict.FAIL_OPEN,
                attributed_to=None,
                reason=(
                    f"context unavailable ({lookup.source}: {lookup.reason}); raised unconditioned"
                ),
            )

        if not lookup.events:
            return Attribution(
                status=EpisodeStatus.REAL,
                verdict=Verdict.NO_CONTEXT,
                attributed_to=None,
                reason="no operational context overlaps this episode",
            )

        # More than one event can overlap. Take the strongest explanation available, and
        # keep the most informative rejection so the reason is useful when nothing explains
        # it. Every rejection considered is counted, whether or not it is the one recorded:
        # keeping only the winner is what hid the corroboration test's conclusions for four
        # published runs (G-16).
        best_rejection: Attribution | None = None
        for event in sorted(lookup.events, key=lambda e: e.kind is ContextKind.PIPELINE):
            attribution = self._consider(episode, event)
            if attribution.status is EpisodeStatus.ATTRIBUTED:
                return attribution
            self.rejections[str(attribution.verdict)] = (
                self.rejections.get(str(attribution.verdict), 0) + 1
            )
            if best_rejection is None or _more_informative(attribution, best_rejection):
                best_rejection = attribution

        assert best_rejection is not None
        return best_rejection

    def _reconsider_with_second_detector(
        self, episode: Episode, attribution: Attribution
    ) -> Attribution:
        """Let the corroborating detector confirm or contradict the conclusion (ADR-050).

        Two directions, and they are not symmetric on purpose:

        - **Agreement protects.** Both detectors saw the same span move, so the episode is
          raised whatever the context half concluded. This can only add pages, never
          remove them, so it cannot flatter the false-positive number.
        - **Dissent suppresses**, and only where nothing else already did. This is the one
          rule here that needs no context event, which is the whole reason it exists -- it
          is the only thing that can answer the `unexplained` false pages B-6 counted.

        `fail_open` is exempt from suppression. ADR-007 says an episode decided without a
        context signal is raised unconditioned, and that promise is worth more than the
        pages this would remove.
        """
        opinion = self._second_opinion(episode)
        if opinion is None:
            return attribution

        self.second_opinion_peaks.append(opinion.peak_score)
        outcome = (
            "abstained" if not opinion.covered else ("agrees" if opinion.agrees else "dissents")
        )
        self.second_opinions[outcome] = self.second_opinions.get(outcome, 0) + 1

        if not self.thresholds.use_second_opinion or not opinion.covered:
            return attribution

        if opinion.agrees:
            if attribution.status is not EpisodeStatus.ATTRIBUTED:
                return attribution
            if not self.thresholds.second_opinion_protects_attributed:
                return attribution
            self.second_opinion_vetoed += 1
            return Attribution(
                status=EpisodeStatus.REAL,
                verdict=Verdict.SECOND_OPINION_AGREES,
                attributed_to=None,
                reason=(
                    f"{attribution.attributed_to} could explain this, but {opinion.reason}; "
                    f"two detectors agreeing outranks an explanation"
                ),
                corroborating_channels=attribution.corroborating_channels,
                scope_size=attribution.scope_size,
            )

        if attribution.status is EpisodeStatus.ATTRIBUTED:
            return attribution
        if attribution.verdict is Verdict.FAIL_OPEN:
            return attribution
        self.second_opinion_suppressed += 1
        return Attribution(
            status=EpisodeStatus.ATTRIBUTED,
            verdict=Verdict.SECOND_OPINION_DISSENTS,
            attributed_to=None,
            reason=(
                f"{opinion.reason}; one detector alone on a span the other watched and "
                f"found ordinary is more likely that detector than the world"
            ),
        )

    def _second_opinion(self, episode: Episode) -> SecondOpinion | None:
        if self.second_opinion is None:
            return None
        return self.second_opinion.opinion(
            episode.channel,
            episode.t_start_ms,
            episode.t_end_ms,
            self.thresholds.second_opinion_agrees_at,
        )

    def _reconsider_with_persistence(
        self, episode: Episode, attribution: Attribution
    ) -> Attribution:
        """Did the excursion last, or did the detector blink once (ADR-053)?

        Independent of the second detector on purpose. Agreement asks whether something
        else saw the same thing; this asks whether the thing was there in the next window,
        which is answerable from the baseline's own output and is therefore available on a
        run with no model at all.

        The two directions are not symmetric, and differently from the second detector's:

        - **Persistence protects only from this test**, not from the context half. Most
          episodes are persistent, so letting duration veto a deploy attribution would
          refuse nearly every attribution the context half makes -- replacing it rather
          than leaving it unchanged. Configurable, and the reasoning is in the threshold's
          comment.
        - **A flicker suppresses**, and only where nothing else already did.

        `fail_open` is exempt, as everywhere: an episode decided without a context signal
        is raised unconditioned (ADR-007).
        """
        persistence = self._persistence_of(episode)
        if persistence is None:
            return attribution

        self.persistence_windows_seen.append(persistence.flagged_windows)
        outcome = (
            "abstained"
            if not persistence.covered
            else ("persistent" if persistence.persistent else "flicker")
        )
        self.persistence_outcomes[outcome] = self.persistence_outcomes.get(outcome, 0) + 1
        if persistence.flagged_windows < self.thresholds.min_persistence_windows:
            # Counted for every one-window episode, acted on or not, so what a rescue would
            # have bought is a number from this run rather than an argument about it.
            self._flicker_recurrences.append(persistence.recurrences)
            self._flicker_shoulders.append(persistence.shoulder_windows)

        if not self.thresholds.use_persistence or not persistence.covered:
            return attribution

        if persistence.persistent:
            if attribution.status is not EpisodeStatus.ATTRIBUTED:
                return attribution
            if not self.thresholds.persistence_protects_attributed:
                return attribution
            self.persistence_vetoed += 1
            return Attribution(
                status=EpisodeStatus.REAL,
                verdict=Verdict.PERSISTENT,
                attributed_to=None,
                reason=(
                    f"{attribution.attributed_to} could explain this, but {persistence.reason}; "
                    f"an excursion that lasted is not a rollout blip"
                ),
                corroborating_channels=attribution.corroborating_channels,
                scope_size=attribution.scope_size,
            )

        if attribution.status is EpisodeStatus.ATTRIBUTED:
            return attribution
        if attribution.verdict is Verdict.FAIL_OPEN:
            return attribution
        self.persistence_suppressed += 1
        return Attribution(
            status=EpisodeStatus.ATTRIBUTED,
            verdict=Verdict.NO_PERSISTENCE,
            attributed_to=None,
            reason=(
                f"{persistence.reason}; a state the channel was in would still be there a "
                f"slide later, and this was not"
            ),
        )

    def _persistence_of(self, episode: Episode) -> EpisodePersistence | None:
        """What the duration test saw. None when the test is not configured at all.

        The duration itself comes from the episode, not from an index: `window_count` is
        the number of windows the builder merged, which is exactly the question. The index
        is consulted only for the shoulder measurement, so a run with the shoulder fraction
        at zero needs no index and still gets a verdict.
        """
        if not self.thresholds.use_persistence and self.persistence is None:
            return None

        flagged = episode.window_count
        recurrence_ms = self.thresholds.persistence_recurrence_ms
        recurrences = (
            self.index.recurrences_near(episode.channel, episode.began_ms, recurrence_ms)
            if recurrence_ms > 0
            else 0
        )
        # Measured at the reporting fraction whatever the acting fraction is, so a run with
        # the rescue switched off still reports how many shoulders were *there*. Reading
        # the acting fraction here would make "the test was off" and "no shoulder existed"
        # the same zero in the report, which is the distinction the whole counterfactual
        # exists to draw.
        shoulder = (
            self.persistence.shoulder_before(
                episode.channel,
                episode.t_start_ms,
                episode.threshold,
                self.thresholds.persistence_shoulder_fraction or SHOULDER_REPORTING_FRACTION,
            )
            if self.persistence is not None
            else 0
        )

        needed = self.thresholds.min_persistence_windows
        if flagged >= needed:
            return EpisodePersistence(
                covered=True,
                persistent=True,
                flagged_windows=flagged,
                recurrences=recurrences,
                shoulder_windows=shoulder,
                reason=(
                    f"the excursion held the threshold for {flagged} consecutive windows "
                    f"(persistence needs {needed})"
                ),
            )
        if recurrences:
            return EpisodePersistence(
                covered=True,
                persistent=True,
                flagged_windows=flagged,
                recurrences=recurrences,
                shoulder_windows=shoulder,
                reason=(
                    f"one window, but {episode.channel} departed {recurrences} other "
                    f"time(s) within {recurrence_ms / 1000:g}s -- a recurring signature "
                    f"rather than a single blip"
                ),
            )
        # Gated on the *acting* fraction, not on whether a shoulder was measured: the
        # measurement runs on every episode so the counterfactual is real, and the rescue
        # only fires where someone asked for it.
        if self.thresholds.persistence_shoulder_fraction > 0 and flagged + shoulder >= needed:
            return EpisodePersistence(
                covered=True,
                persistent=True,
                flagged_windows=flagged,
                recurrences=recurrences,
                shoulder_windows=shoulder,
                reason=(
                    f"one window over the threshold, with {shoulder} window(s) before it "
                    f"already at {self.thresholds.persistence_shoulder_fraction:.0%} of it "
                    f"-- the excursion was building"
                ),
            )
        return EpisodePersistence(
            covered=True,
            persistent=False,
            flagged_windows=flagged,
            recurrences=recurrences,
            shoulder_windows=shoulder,
            reason=(
                f"{episode.channel} crossed the threshold in exactly {flagged} window and "
                f"in neither neighbouring view built from the same 20 seconds of samples"
            ),
        )

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
            perturbed = siblings | {episode.channel}
            shape = self._blast_radius_verdict(event, perturbed)
            if shape is not None:
                return shape
            return self._attribute(
                event,
                (
                    f"{corroborating} of the {scope_size} channels {event.event_id} touched "
                    f"moved within {self.thresholds.synchrony_ms / 1000:g}s of each other, "
                    f"across {self._footprint_phrase(perturbed)}, which is the shape of the "
                    f"rollout rather than of a machine failing"
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

    def _footprint_phrase(self, channels: set[str]) -> str:
        if not self.topology.known:
            return f"{len(channels)} channels"
        return self.topology.footprint(channels).describe()

    def _blast_radius_verdict(self, event: ContextEvent, perturbed: set[str]) -> Attribution | None:
        """Does the perturbation have the shape of this rollout, or of a failure domain?

        Returns `None` when the topology raises no objection and the attribution may stand.
        Otherwise returns the rejection, with the reason an operator needs.

        Timing says these channels moved together. It cannot say *why*, because a machine
        failing moves several of its own metrics within seconds too -- and when that machine
        is inside a deploy's scope, a timing-only policy attributes a real fault to a deploy
        and stops paging anyone. What separates the two is where the channels sit. A deploy
        ring is laid out across physical failure domains deliberately, so that a cabinet
        losing power and a rollout going bad do not look alike downstream; a fault is
        confined to the thing that broke. So the test is structural: spread across domains
        is a rollout, concentration in one is a fault.

        Two ways it declines, and both raise the episode:

        - the **observed** perturbation is confined to one machine or one cabinet, which is
          a fault however well-timed it was;
        - the **declared** blast radius is itself confined to one machine, so the rollout
          and a fault would leave the same footprint and no evidence could separate them
          (the same reasoning as ADR-025, applied to shape rather than to count).
        """
        if not self.thresholds.require_blast_radius or not self.topology.known:
            return None

        observed = self.topology.footprint(perturbed)
        if observed.unplaced:
            # A channel the inventory does not know about. Absence of evidence, so the
            # topology declines to object rather than guessing where it lives.
            return None

        # A fleet-wide event declares no radius, so there is nothing to compare the
        # observation against and only the observation itself can be judged.
        declared = self.topology.footprint(event.scope) if event.scope else None

        if declared is not None and declared.node_count < self.thresholds.min_blast_nodes:
            return Attribution(
                status=EpisodeStatus.REAL,
                verdict=Verdict.NARROW_BLAST_RADIUS,
                attributed_to=None,
                reason=(
                    f"{event.event_id} reached only {declared.nodes[0] if declared.nodes else '?'}"
                    f", so its blast radius is one machine -- the same footprint a fault on "
                    f"that machine would leave, and nothing can tell the two apart"
                ),
                scope_size=len(event.scope),
            )

        if observed.node_count < self.thresholds.min_blast_nodes:
            return Attribution(
                status=EpisodeStatus.REAL,
                verdict=Verdict.FAULT_DOMAIN,
                attributed_to=None,
                reason=(
                    f"every channel that moved sits on {observed.nodes[0]}, while "
                    f"{event.event_id} reached "
                    f"{declared.node_count if declared else 'the whole fleet'} machines; one "
                    f"machine moving is that machine failing, not the rollout arriving"
                ),
                scope_size=len(event.scope),
            )

        if (
            self.thresholds.require_rack_spread
            and self.topology.distinguishes_racks
            and observed.confined_to_one_rack
            and declared is not None
            and not declared.confined_to_one_rack
        ):
            return Attribution(
                status=EpisodeStatus.REAL,
                verdict=Verdict.FAULT_DOMAIN,
                attributed_to=None,
                reason=(
                    f"the {observed.node_count} machines that moved are all in "
                    f"{observed.racks[0]}, while {event.event_id} reached "
                    f"{declared.rack_count} racks; a rollout does not stop at a cabinet "
                    f"boundary and a cabinet losing power does"
                ),
                scope_size=len(event.scope),
            )
        return None

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
        # Persisted from here on. G-16 was a reporting defect about which verdict an
        # episode carried, and it was only findable because the console printed one; a
        # verdict that never reaches the store cannot be cross-checked against ground
        # truth at all, which is exactly what reading the second detector's effect needs.
        episode.verdict = str(attribution.verdict)
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
        if self.rejections:
            reached = ", ".join(f"{k}={v}" for k, v in sorted(self.rejections.items()))
            line += f"\n  rejections reached (an episode carries one): {reached}"
        if self.second_opinions:
            line += f"\n  {self.second_opinion_report()}"
        if self.persistence_outcomes:
            line += f"\n  {self.persistence_report()}"
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

    def second_opinion_report(self) -> str:
        """What the corroborating detector said, and what was done about it.

        Reported on the ablation pass too, where the policy ignores it. A signal whose
        effect is only visible in the pass that uses it cannot be told apart from a signal
        that was never there.
        """
        if self.second_opinion is None or not self.second_opinions:
            return "second detector: not running"
        counts = ", ".join(f"{k}={v}" for k, v in sorted(self.second_opinions.items()))
        acted = (
            f"suppressed {self.second_opinion_suppressed}, "
            f"vetoed an attribution {self.second_opinion_vetoed}"
            if self.thresholds.use_second_opinion
            else "advisory only, changed nothing"
        )
        line = (
            f"second detector ({self.second_opinion.detector or 'unnamed'}) over "
            f"{len(self.second_opinion_peaks)} decisions at agreement "
            f"{self.thresholds.second_opinion_agrees_at:g}: {counts} | {acted}"
        )
        return f"{line}\n  {self.agreement_sensitivity()}"

    def agreement_sensitivity(self) -> str:
        """How many decisions would have counted as agreement at other thresholds.

        Published because one threshold's result is unfalsifiable: a reader cannot tell a
        line chosen on principle from one chosen because it flattered the run. The default
        is fixed before the run and this curve is reported beside it.
        """
        peaks = [p for p in self.second_opinion_peaks if p > 0.0]
        if not peaks:
            return "agreement sensitivity: no scored spans"
        points = [f"{bar:g}:{sum(1 for p in peaks if p >= bar)}" for bar in (1, 2, 3, 4, 6, 8)]
        ordered = sorted(peaks)
        return (
            f"agreement sensitivity over {len(peaks)} scored spans "
            f"(bar:agreeing) {' '.join(points)} | peak p50 "
            f"{ordered[len(ordered) // 2]:.1f} p90 "
            f"{ordered[min(int(len(ordered) * 0.9), len(ordered) - 1)]:.1f} "
            f"max {ordered[-1]:.1f}"
        )

    def persistence_report(self) -> str:
        """What the duration test saw, and what was done about it.

        Printed on the ablation pass too, where the policy ignores it, for the same reason
        the second detector's is: a signal whose effect is only visible in the pass that
        uses it cannot be told apart from a signal that was never there.
        """
        if not self.persistence_outcomes:
            return "persistence: not running"
        counts = ", ".join(f"{k}={v}" for k, v in sorted(self.persistence_outcomes.items()))
        acted = (
            f"suppressed {self.persistence_suppressed}, "
            f"vetoed an attribution {self.persistence_vetoed}"
            if self.thresholds.use_persistence
            else "advisory only, changed nothing"
        )
        line = (
            f"persistence over {len(self.persistence_windows_seen)} decisions at "
            f"{self.thresholds.min_persistence_windows} window(s), recurrence "
            f"{self.thresholds.persistence_recurrence_ms / 1000:g}s, shoulder "
            f"{self.thresholds.persistence_shoulder_fraction:g}: {counts} | {acted}"
        )
        return f"{line}\n  {self.persistence_sensitivity()}"

    def persistence_sensitivity(self) -> str:
        """The duration distribution, and what each rescue would have bought.

        Published for the same reason the agreement curve is: one threshold's result is
        unfalsifiable, and a reader cannot otherwise tell a line drawn from the window
        geometry from one drawn around the answer. The rescue columns report what the two
        tests that are *not* wired into the verdict would have saved, so switching either
        on is a decision made from this run's numbers.
        """
        seen = self.persistence_windows_seen
        if not seen:
            return "persistence sensitivity: no decisions"
        points = " ".join(f"{n}:{sum(1 for w in seen if w >= n)}" for n in (1, 2, 3, 4, 6))
        ordered = sorted(seen)
        rescue = ""
        if self._flicker_recurrences:
            recurring = sum(1 for r in self._flicker_recurrences if r)
            shouldered = sum(1 for sh in self._flicker_shoulders if sh)
            rescue = (
                f" | of {len(self._flicker_recurrences)} one-window episodes, "
                f"{recurring} had a recurrence and {shouldered} a shoulder"
            )
        return (
            f"persistence sensitivity over {len(seen)} decisions (windows:episodes at least "
            f"that long) {points} | window count p50 "
            f"{ordered[len(ordered) // 2]} p90 "
            f"{ordered[min(int(len(ordered) * 0.9), len(ordered) - 1)]} "
            f"max {ordered[-1]}{rescue}"
        )


# How informative a rejection is, most first. The episode is raised either way; what this
# decides is which of several overlapping events gets to explain why on the record. A
# conclusion the topology reached about the channels that moved says more than "a pipeline
# event overlapped and had no mechanism", which says more than "that event never touched
# this channel at all". Before this ordering existed the *last* rejection won, pipeline
# events sorted last, and the deploy test's conclusion was overwritten every time (G-16).
_VERDICT_INFORMATIVENESS = (
    Verdict.FAULT_DOMAIN,
    Verdict.NARROW_BLAST_RADIUS,
    Verdict.ISOLATED,
    Verdict.IMPLAUSIBLE,
    Verdict.OUT_OF_SCOPE,
)


def _more_informative(candidate: Attribution, incumbent: Attribution) -> bool:
    def rank(attribution: Attribution) -> int:
        try:
            return _VERDICT_INFORMATIVENESS.index(attribution.verdict)
        except ValueError:
            return len(_VERDICT_INFORMATIVENESS)

    return rank(candidate) < rank(incumbent)


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
