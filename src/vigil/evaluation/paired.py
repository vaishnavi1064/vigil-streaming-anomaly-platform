"""Scoring a run against its ground-truth plan, as a pair (ADR-016).

False-positive reduction on its own is maximised by suppressing everything, so it is never
reported alone. Every result here is a pair: how much noise conditioning removed, **and**
what it cost in recall on real faults -- broken out for faults inside context windows and
outside them, because the inside population is exactly what a blanket suppressor loses.

Counting is **incident-level**. A run of consecutive alarms on one channel counts once,
because an operator is paged once per incident and counting each window separately lets a
chatty detector inflate both its false-positive count and its apparent reduction. Episodes
are already merged incidents, so the unit of counting is the episode.

Matching is by **temporal overlap on the same channel**, not by exact boundaries. A windowed
detector cannot reproduce an episode's exact start, and demanding it would measure window
alignment rather than detection. There is no point-adjustment here: an episode overlapping a
fault counts as one detection of that fault, not as credit for every point in it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from vigil.episodes import EpisodeStatus


@dataclass(frozen=True)
class TruthEpisode:
    """One injected episode from the plan, on one channel."""

    channel: str
    kind: str
    origin: str
    t_start_ms: int
    t_end_ms: int
    # Which real-world event this belongs to, and the topology domain that event occupies.
    # A pump seizing moves several of its metrics and is one incident; older plans have no
    # incident field and each episode is then its own, which is what they were.
    incident: str = ""
    domain: str = ""

    @property
    def is_real(self) -> bool:
        return self.origin == "fault"

    def overlaps(self, start_ms: int, end_ms: int, slack_ms: int = 0) -> bool:
        return self.t_start_ms - slack_ms <= end_ms and start_ms <= self.t_end_ms + slack_ms


@dataclass(frozen=True)
class FaultIncident:
    """One real fault, as the set of channels it moved.

    The `domain` says which level of the topology it occupies -- one machine, one cabinet,
    or a single sensor -- so recall can be broken out by fault shape. A discriminator built
    on blast radius has to be scored against the shapes it claims to separate, or the claim
    is untested.
    """

    incident: str
    domain: str
    episodes: tuple[TruthEpisode, ...]
    window: TruthWindow | None

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(sorted({e.channel for e in self.episodes}))

    @property
    def t_start_ms(self) -> int:
        return min(e.t_start_ms for e in self.episodes)

    @property
    def inside_window(self) -> bool:
        return self.window is not None

    @property
    def inside_quiet_window(self) -> bool:
        return self.window is not None and not self.window.perturbed


@dataclass(frozen=True)
class TruthWindow:
    """One context window from the plan, with whether it actually perturbed anything."""

    event_id: str
    t_start_ms: int
    t_end_ms: int
    scope: tuple[str, ...]
    perturbed: bool

    def covers(self, channel: str, start_ms: int, end_ms: int) -> bool:
        in_time = self.t_start_ms <= end_ms and start_ms <= self.t_end_ms
        in_scope = not self.scope or channel in self.scope
        return in_time and in_scope


@dataclass
class GroundTruth:
    """The plan the generator wrote before any detector saw the data."""

    faults: list[TruthEpisode] = field(default_factory=list)
    artifacts: list[TruthEpisode] = field(default_factory=list)
    windows: list[TruthWindow] = field(default_factory=list)

    @classmethod
    def from_plan(cls, path: Path) -> GroundTruth:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        truth = cls()
        for channel, episodes in data.get("faults", {}).items():
            for e in episodes:
                truth.faults.append(
                    TruthEpisode(
                        channel,
                        e["kind"],
                        e["origin"],
                        e["t_start_ms"],
                        e["t_end_ms"],
                        incident=e.get("incident") or f"{channel}@{e['t_start_ms']}",
                        domain=e.get("domain") or "channel",
                    )
                )
        for deploy in data.get("deploys", []):
            event = deploy["event"]
            truth.windows.append(
                TruthWindow(
                    event_id=event["event_id"],
                    t_start_ms=event["t_start_ms"],
                    t_end_ms=event["t_end_ms"],
                    scope=tuple(event.get("scope", ())),
                    perturbed=bool(event.get("perturbed_telemetry", False)),
                )
            )
            for channel, episodes in deploy.get("artifacts", {}).items():
                for e in episodes:
                    truth.artifacts.append(
                        TruthEpisode(
                            channel,
                            e["kind"],
                            e["origin"],
                            e["t_start_ms"],
                            e["t_end_ms"],
                            incident=e.get("incident") or event["event_id"],
                            domain=e.get("domain") or "ring",
                        )
                    )
        return truth

    def faults_inside_windows(self) -> list[TruthEpisode]:
        return [f for f in self.faults if self._covering(f) is not None]

    def faults_outside_windows(self) -> list[TruthEpisode]:
        return [f for f in self.faults if self._covering(f) is None]

    def faults_inside_quiet_windows(self) -> list[TruthEpisode]:
        out = []
        for fault in self.faults:
            window = self._covering(fault)
            if window is not None and not window.perturbed:
                out.append(fault)
        return out

    def _covering(self, episode: TruthEpisode) -> TruthWindow | None:
        for window in self.windows:
            if window.covers(episode.channel, episode.t_start_ms, episode.t_end_ms):
                return window
        return None

    # -- incidents: the unit an operator is actually paged for --

    def fault_incidents(self) -> list[FaultIncident]:
        """Faults grouped by the thing that happened, not by the channel that showed it.

        A pump seizing moves several of its own metrics. Counting that as four faults would
        quadruple its weight in recall against a single-sensor fault next to it, and would
        make the recall denominator a function of how many metrics each machine happens to
        report. ADR-016 already fixed the unit of counting as the incident; this extends the
        same rule from the observed side to the truth side.

        In every plan written before topology-aware faults existed each episode is its own
        incident, so this is a strict generalisation rather than a redefinition.
        """
        grouped: dict[str, list[TruthEpisode]] = {}
        for fault in self.faults:
            grouped.setdefault(fault.incident or f"{fault.channel}@{fault.t_start_ms}", []).append(
                fault
            )
        out = []
        for incident_id, members in grouped.items():
            windows = [self._covering(m) for m in members]
            covering = next((w for w in windows if w is not None), None)
            out.append(
                FaultIncident(
                    incident=incident_id,
                    domain=members[0].domain or "channel",
                    episodes=tuple(sorted(members, key=lambda m: m.t_start_ms)),
                    window=covering,
                )
            )
        out.sort(key=lambda i: i.t_start_ms)
        return out

    def incidents_by_domain(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for incident in self.fault_incidents():
            counts[incident.domain] = counts.get(incident.domain, 0) + 1
        return dict(sorted(counts.items()))


@dataclass(frozen=True)
class ObservedEpisode:
    """One episode the platform produced."""

    channel: str
    t_start_ms: int
    t_end_ms: int
    status: str
    raised_by: str
    peak_score: float
    attributed_to: str | None = None
    # Which conditioning test decided this episode. Null on the unconditioned pass.
    verdict: str | None = None

    @property
    def paged(self) -> bool:
        """Would an operator have been woken up by this?"""
        return self.status == str(EpisodeStatus.REAL)


@dataclass
class PairedResult:
    """One pass, scored. Never reported without its counterpart."""

    label: str
    episodes: int
    paged: int
    attributed: int

    # Counted per incident: the thing that happened, not the channels that showed it.
    faults_total: int
    faults_detected: int
    faults_inside_total: int
    faults_inside_detected: int
    faults_outside_total: int
    faults_outside_detected: int
    faults_quiet_window_total: int
    faults_quiet_window_detected: int

    artifact_pages: int
    unexplained_pages: int

    # Recall broken out by the shape of the fault, which is what a blast-radius
    # discriminator has to be scored against. Keys are topology domains.
    faults_by_domain: dict[str, tuple[int, int]] = field(default_factory=dict)
    # The finer view, one entry per channel-episode. Reported beside the incident numbers
    # rather than instead of them: a policy that loses one metric of a four-metric fault has
    # done something different from one that loses the whole fault, and only this shows it.
    fault_channels_total: int = 0
    fault_channels_detected: int = 0
    # Which test suppressed what, against what the plan says the episode really was. A
    # reduction number says how many pages went away; this says whether they should have.
    suppressed_by_verdict: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def recall(self) -> float:
        return self.faults_detected / self.faults_total if self.faults_total else 0.0

    @property
    def recall_inside(self) -> float:
        return (
            self.faults_inside_detected / self.faults_inside_total
            if self.faults_inside_total
            else 0.0
        )

    @property
    def recall_outside(self) -> float:
        return (
            self.faults_outside_detected / self.faults_outside_total
            if self.faults_outside_total
            else 0.0
        )

    @property
    def recall_quiet_window(self) -> float:
        return (
            self.faults_quiet_window_detected / self.faults_quiet_window_total
            if self.faults_quiet_window_total
            else 0.0
        )

    @property
    def false_pages(self) -> int:
        """Pages an operator should not have received: artifacts plus the unexplained."""
        return self.artifact_pages + self.unexplained_pages

    @property
    def precision(self) -> float:
        return (self.paged - self.false_pages) / self.paged if self.paged else 0.0

    @property
    def recall_channels(self) -> float:
        return (
            self.fault_channels_detected / self.fault_channels_total
            if self.fault_channels_total
            else 0.0
        )

    def domain_line(self) -> str:
        if not self.faults_by_domain:
            return "no faults"
        return " | ".join(
            f"{domain} {found}/{total}"
            for domain, (found, total) in sorted(self.faults_by_domain.items())
        )


def score_pass(
    label: str,
    observed: list[ObservedEpisode],
    truth: GroundTruth,
    *,
    slack_ms: int = 30_000,
) -> PairedResult:
    """Score one pass. `slack_ms` allows for window alignment, not for point-adjustment."""
    paged = [o for o in observed if o.paged]

    def detected(fault: TruthEpisode) -> bool:
        # Detected means an operator would have been paged for it. An episode that exists
        # but was attributed away did not wake anyone, so it is not a detection.
        return any(
            o.channel == fault.channel
            and fault.overlaps(o.t_start_ms, o.t_end_ms, slack_ms=slack_ms)
            for o in paged
        )

    def incident_detected(incident: FaultIncident) -> bool:
        # One page is enough. An operator woken for the bearing temperature of a seizing
        # pump has been told about the pump.
        return any(detected(e) for e in incident.episodes)

    incidents = truth.fault_incidents()
    inside = [i for i in incidents if i.inside_window]
    outside = [i for i in incidents if not i.inside_window]
    quiet = [i for i in incidents if i.inside_quiet_window]

    by_domain: dict[str, tuple[int, int]] = {}
    for incident in incidents:
        found, total = by_domain.get(incident.domain, (0, 0))
        by_domain[incident.domain] = (found + int(incident_detected(incident)), total + 1)

    artifact_pages = 0
    unexplained_pages = 0
    for o in paged:
        if any(
            a.channel == o.channel and a.overlaps(o.t_start_ms, o.t_end_ms, slack_ms=slack_ms)
            for a in truth.artifacts
        ):
            artifact_pages += 1
        elif not any(
            f.channel == o.channel and f.overlaps(o.t_start_ms, o.t_end_ms, slack_ms=slack_ms)
            for f in truth.faults
        ):
            unexplained_pages += 1
    suppressed_by_verdict: dict[str, dict[str, int]] = {}
    for o in observed:
        if o.paged:
            continue
        row = suppressed_by_verdict.setdefault(
            o.verdict or "unconditioned", {"fault": 0, "artifact": 0, "unexplained": 0}
        )
        row[classify(o, truth, slack_ms)] += 1

    return PairedResult(
        label=label,
        episodes=len(observed),
        paged=len(paged),
        attributed=len(observed) - len(paged),
        faults_total=len(incidents),
        faults_detected=sum(1 for i in incidents if incident_detected(i)),
        faults_inside_total=len(inside),
        faults_inside_detected=sum(1 for i in inside if incident_detected(i)),
        faults_outside_total=len(outside),
        faults_outside_detected=sum(1 for i in outside if incident_detected(i)),
        faults_quiet_window_total=len(quiet),
        faults_quiet_window_detected=sum(1 for i in quiet if incident_detected(i)),
        artifact_pages=artifact_pages,
        unexplained_pages=unexplained_pages,
        faults_by_domain=by_domain,
        fault_channels_total=len(truth.faults),
        fault_channels_detected=sum(1 for f in truth.faults if detected(f)),
        suppressed_by_verdict=suppressed_by_verdict,
    )


def classify(observed: ObservedEpisode, truth: GroundTruth, slack_ms: int = 30_000) -> str:
    """What this episode actually was, by the plan written before anything scored it."""
    if any(
        f.channel == observed.channel
        and f.overlaps(observed.t_start_ms, observed.t_end_ms, slack_ms=slack_ms)
        for f in truth.faults
    ):
        return "fault"
    if any(
        a.channel == observed.channel
        and a.overlaps(observed.t_start_ms, observed.t_end_ms, slack_ms=slack_ms)
        for a in truth.artifacts
    ):
        return "artifact"
    return "unexplained"


def verdict_breakdown(
    observed: list[ObservedEpisode], truth: GroundTruth, slack_ms: int = 30_000
) -> dict[str, dict[str, int]]:
    """Every verdict against what the episode really was.

    This is what makes a suppression claim checkable. A policy that removes false pages and
    a policy that removes pages is the same number until the verdicts are read against
    ground truth, and until now the verdict existed only in the console.
    """
    out: dict[str, dict[str, int]] = {}
    for o in observed:
        row = out.setdefault(
            o.verdict or "unconditioned", {"fault": 0, "artifact": 0, "unexplained": 0}
        )
        row[classify(o, truth, slack_ms)] += 1
    return dict(sorted(out.items()))


@dataclass
class PairedComparison:
    """Shadow against conditioned. The only form a result is reported in."""

    shadow: PairedResult
    conditioned: PairedResult
    fp_reduction_target: float = 0.40
    recall_loss_tolerance: float = 0.05

    @property
    def fp_reduction(self) -> float:
        if not self.shadow.false_pages:
            return 0.0
        removed = self.shadow.false_pages - self.conditioned.false_pages
        return removed / self.shadow.false_pages

    @property
    def recall_loss(self) -> float:
        return self.shadow.recall - self.conditioned.recall

    @property
    def recall_loss_inside(self) -> float:
        return self.shadow.recall_inside - self.conditioned.recall_inside

    @property
    def meets_target(self) -> bool:
        """Both halves, or neither. This is NFR-8."""
        return (
            self.fp_reduction >= self.fp_reduction_target
            and self.recall_loss <= self.recall_loss_tolerance
        )

    def table(self) -> str:
        s, c = self.shadow, self.conditioned
        rows = [
            ("Episodes recorded", f"{s.episodes}", f"{c.episodes}", ""),
            ("Pages raised", f"{s.paged}", f"{c.paged}", f"{c.paged - s.paged:+d}"),
            ("Attributed (not paged)", f"{s.attributed}", f"{c.attributed}", ""),
            (
                "False pages (artifact + unexplained)",
                f"{s.false_pages}",
                f"{c.false_pages}",
                f"{c.false_pages - s.false_pages:+d}",
            ),
            (
                "  of which artifact-driven",
                f"{s.artifact_pages}",
                f"{c.artifact_pages}",
                f"{c.artifact_pages - s.artifact_pages:+d}",
            ),
            (
                "Recall, all real faults",
                f"{s.recall:.1%} ({s.faults_detected}/{s.faults_total})",
                f"{c.recall:.1%} ({c.faults_detected}/{c.faults_total})",
                f"{c.recall - s.recall:+.1%}",
            ),
            (
                "Recall, faults OUTSIDE windows",
                f"{s.recall_outside:.1%} ({s.faults_outside_detected}/{s.faults_outside_total})",
                f"{c.recall_outside:.1%} ({c.faults_outside_detected}/{c.faults_outside_total})",
                f"{c.recall_outside - s.recall_outside:+.1%}",
            ),
            (
                "Recall, faults INSIDE windows",
                f"{s.recall_inside:.1%} ({s.faults_inside_detected}/{s.faults_inside_total})",
                f"{c.recall_inside:.1%} ({c.faults_inside_detected}/{c.faults_inside_total})",
                f"{c.recall_inside - s.recall_inside:+.1%}",
            ),
            (
                "Recall, faults in QUIET windows",
                f"{s.recall_quiet_window:.1%} "
                f"({s.faults_quiet_window_detected}/{s.faults_quiet_window_total})",
                f"{c.recall_quiet_window:.1%} "
                f"({c.faults_quiet_window_detected}/{c.faults_quiet_window_total})",
                f"{c.recall_quiet_window - s.recall_quiet_window:+.1%}",
            ),
            (
                "Recall, per fault channel-episode",
                f"{s.recall_channels:.1%} ({s.fault_channels_detected}/{s.fault_channels_total})",
                f"{c.recall_channels:.1%} ({c.fault_channels_detected}/{c.fault_channels_total})",
                f"{c.recall_channels - s.recall_channels:+.1%}",
            ),
            (
                "Precision (incident-level)",
                f"{s.precision:.1%}",
                f"{c.precision:.1%}",
                f"{c.precision - s.precision:+.1%}",
            ),
        ]
        width = max(len(r[0]) for r in rows)
        lines = [f"{'Measure'.ljust(width)}  {'Shadow':>22}  {'Conditioned':>22}  {'Delta':>9}"]
        lines.append("-" * (width + 60))
        for name, a, b, delta in rows:
            lines.append(f"{name.ljust(width)}  {a:>22}  {b:>22}  {delta:>9}")
        if s.faults_by_domain:
            lines.append("")
            lines.append("Recall by fault domain -- the shapes a blast-radius test separates")
            lines.append(f"  shadow      {s.domain_line()}")
            lines.append(f"  conditioned {c.domain_line()}")
        return "\n".join(lines)

    def verdict(self) -> str:
        met = "MET" if self.meets_target else "NOT MET"
        return (
            f"NFR-8 {met}: false-positive reduction {self.fp_reduction:+.1%} "
            f"(target >= {self.fp_reduction_target:.0%}), "
            f"recall loss {self.recall_loss:+.1%} "
            f"(tolerance <= {self.recall_loss_tolerance:.0%}). "
            f"Recall loss inside context windows {self.recall_loss_inside:+.1%}."
        )


@dataclass
class FailOpenCheck:
    """Did conditioning stay out of the way when it had no signal to condition on?

    ADR-007 makes this a correctness property rather than a preference: a policy that
    suppresses while its context source says nothing has turned silence in the signal path
    into silence in the alerting path, which is the failure mode conditioning is most likely
    to introduce and the hardest to notice. So the pass has to reproduce the unconditioned
    baseline exactly -- not approximately, and not "close enough".

    The pass this scores runs against a context topic that exists and is empty, so the
    source is available and silent. A source that cannot be reached at all is the other half
    of ADR-007 and is covered by unit tests on the policy.
    """

    shadow_episodes: int
    fail_open_episodes: int
    identical: int
    missing_from_fail_open: int
    extra_in_fail_open: int
    suppressed: int
    attributed: int

    @property
    def held(self) -> bool:
        return (
            self.missing_from_fail_open == 0
            and self.extra_in_fail_open == 0
            and self.suppressed == 0
            and self.attributed == 0
        )

    def line(self) -> str:
        state = "HELD" if self.held else "BROKEN"
        return (
            f"fail-open (ADR-007) {state}: {self.identical:,} of {self.shadow_episodes:,} "
            f"episodes identical to the unconditioned pass | "
            f"{self.missing_from_fail_open} missing, {self.extra_in_fail_open} extra, "
            f"{self.suppressed} suppressed, {self.attributed} attributed"
        )


def compare_fail_open(
    shadow: list[ObservedEpisode], fail_open: list[ObservedEpisode]
) -> FailOpenCheck:
    """Compare a conditioned-but-signal-less pass against the unconditioned one.

    Identity is on (channel, start, end, detector), not on status: status is precisely what
    conditioning would have changed, so including it in the key would hide a changed verdict
    as a missing episode plus an extra one.
    """

    def key(e: ObservedEpisode) -> tuple[str, int, int, str]:
        return (e.channel, e.t_start_ms, e.t_end_ms, e.raised_by)

    shadow_by_key = {key(e): e for e in shadow}
    fail_open_by_key = {key(e): e for e in fail_open}
    shared = shadow_by_key.keys() & fail_open_by_key.keys()

    return FailOpenCheck(
        shadow_episodes=len(shadow),
        fail_open_episodes=len(fail_open),
        identical=sum(1 for k in shared if shadow_by_key[k].status == fail_open_by_key[k].status),
        missing_from_fail_open=len(shadow_by_key.keys() - fail_open_by_key.keys()),
        extra_in_fail_open=len(fail_open_by_key.keys() - shadow_by_key.keys()),
        suppressed=sum(
            1 for k in shared if shadow_by_key[k].paged and not fail_open_by_key[k].paged
        ),
        attributed=sum(1 for e in fail_open if e.attributed_to is not None),
    )
