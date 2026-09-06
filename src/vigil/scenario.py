"""Scheduled scenarios: deploys, the artifacts they cause, and real faults.

This module exists to make the core claim falsifiable. The easy way to post a large
false-positive reduction is to mute everything during a deploy window, and a naive
evaluation cannot tell that apart from targeted attribution. So the generator is built to
catch it (ADR-015). Four populations are scheduled deliberately:

  1. Deploys that DO perturb telemetry.  The artifact excursion they cause is what
     conditioning should attribute to the deploy instead of paging.
  2. Deploys that do NOT perturb anything.  Real deploys often change nothing observable.
     A system that suppresses on the marker alone still looks correct on population 1;
     it is only caught here, in combination with 4.
  3. Real faults OUTSIDE every deploy window.  The control: detection must be unaffected.
  4. Real faults INSIDE deploy windows, in both perturbing and quiet ones.  These must
     still be detected. A blanket suppressor loses exactly this population, which is why
     recall is reported next to false-positive reduction and never on its own (ADR-016).

Scope matters as much as timing. A deploy touches a subset of channels, and an artifact is
only ever placed on a channel the deploy actually touched. A policy that ignores scope and
suppresses fleet-wide during any deploy is caught by population 4 landing on channels the
deploy never touched.

Everything is scheduled up front from a seed, so a run is exactly reproducible and the
ground truth is known before a single reading is produced.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from vigil.context import ContextEvent, ContextKind, Severity
from vigil.synthetic import AnomalyKind, AnomalyOrigin, InjectedEpisode

# Real deploys are mostly boring. Roughly a third of them changing nothing observable is
# both realistic and enough of a population to make blanket suppression obvious.
DEFAULT_QUIET_DEPLOY_FRACTION = 0.35

# Fraction of genuine faults deliberately placed inside a deploy window. High enough that
# losing them is unmissable in the recall number.
DEFAULT_FAULT_IN_WINDOW_FRACTION = 0.40

_DEPLOY_DETAILS = (
    "rollout of inverter-telemetry-collector {version} to the {fleet} fleet",
    "config change: scrape interval lowered on the {fleet} fleet ({version})",
    "restart of the site-rollup aggregator after {version} deploy",
    "canary of string-normaliser {version} on the {fleet} fleet",
)


@dataclass(frozen=True)
class ScheduledDeploy:
    event: ContextEvent
    # Artifact episodes this deploy causes, keyed by channel. Empty for a quiet deploy.
    artifacts: dict[str, list[InjectedEpisode]] = field(default_factory=dict)

    @property
    def quiet(self) -> bool:
        return not self.artifacts


@dataclass
class ScenarioPlan:
    """A complete, seeded schedule of deploys, artifacts and real faults for one run.

    Times are stream-relative seconds from the start of the run. The producer converts
    them to event-time milliseconds; nothing here needs a wall clock.
    """

    duration_s: float
    channels: tuple[str, ...]
    deploys: list[ScheduledDeploy] = field(default_factory=list)
    faults: dict[str, list[InjectedEpisode]] = field(default_factory=dict)

    def episodes_for(self, channel: str) -> list[InjectedEpisode]:
        """Every scheduled episode on a channel, real and artifact, in time order."""
        out = list(self.faults.get(channel, ()))
        for deploy in self.deploys:
            out.extend(deploy.artifacts.get(channel, ()))
        out.sort(key=lambda e: e.start_s)
        return out

    def context_events(self) -> list[ContextEvent]:
        return sorted((d.event for d in self.deploys), key=lambda e: e.t_start_ms)

    # -- populations, named so the evaluation harness reads like the design --

    @property
    def perturbing_deploys(self) -> list[ScheduledDeploy]:
        return [d for d in self.deploys if not d.quiet]

    @property
    def quiet_deploys(self) -> list[ScheduledDeploy]:
        return [d for d in self.deploys if d.quiet]

    def faults_inside_deploy_windows(self) -> list[tuple[str, InjectedEpisode]]:
        out = []
        for channel, episodes in self.faults.items():
            for ep in episodes:
                if self._covering_deploy(channel, ep) is not None:
                    out.append((channel, ep))
        return out

    def faults_outside_deploy_windows(self) -> list[tuple[str, InjectedEpisode]]:
        out = []
        for channel, episodes in self.faults.items():
            for ep in episodes:
                if self._covering_deploy(channel, ep) is None:
                    out.append((channel, ep))
        return out

    def _covering_deploy(self, channel: str, ep: InjectedEpisode) -> ScheduledDeploy | None:
        for deploy in self.deploys:
            e = deploy.event
            start_ms, end_ms = int(ep.start_s * 1000), int(ep.end_s * 1000)
            if e.overlaps(start_ms, end_ms) and e.applies_to(channel):
                return deploy
        return None

    def summary(self) -> str:
        inside = self.faults_inside_deploy_windows()
        outside = self.faults_outside_deploy_windows()
        return (
            f"scenario: {len(self.deploys)} deploys "
            f"({len(self.perturbing_deploys)} perturbing, {len(self.quiet_deploys)} quiet) | "
            f"real faults {len(inside) + len(outside)} "
            f"({len(inside)} inside a deploy window, {len(outside)} outside)"
        )


def _artifact_episode(rng: random.Random, start_s: float) -> InjectedEpisode:
    """The telemetry excursion a deploy causes: a restart blip or a settling period."""
    kind = rng.choice((AnomalyKind.LEVEL_SHIFT, AnomalyKind.VARIANCE_BURST, AnomalyKind.SPIKE))
    duration = 0.0 if kind is AnomalyKind.SPIKE else rng.uniform(15.0, 70.0)
    magnitude = rng.uniform(3.5, 8.0) * rng.choice((-1.0, 1.0))
    return InjectedEpisode(
        kind=kind,
        start_s=start_s,
        end_s=start_s + duration,
        magnitude=magnitude,
        origin=AnomalyOrigin.DEPLOY,
    )


def _fault_episode(rng: random.Random, start_s: float) -> InjectedEpisode:
    kind = rng.choice(list(AnomalyKind))
    duration = 0.0 if kind is AnomalyKind.SPIKE else rng.uniform(20.0, 90.0)
    magnitude = rng.uniform(4.0, 9.0) * rng.choice((-1.0, 1.0))
    return InjectedEpisode(
        kind=kind,
        start_s=start_s,
        end_s=start_s + duration,
        magnitude=magnitude,
        origin=AnomalyOrigin.FAULT,
    )


def build_scenario(
    channels: tuple[str, ...],
    duration_s: float,
    *,
    seed: int = 1729,
    deploys_per_hour: float = 30.0,
    faults_per_hour: float = 40.0,
    quiet_deploy_fraction: float = DEFAULT_QUIET_DEPLOY_FRACTION,
    fault_in_window_fraction: float = DEFAULT_FAULT_IN_WINDOW_FRACTION,
    deploy_duration_s: tuple[float, float] = (60.0, 180.0),
) -> ScenarioPlan:
    """Schedule one reproducible run.

    Rates are per hour across the whole fleet, not per channel, because a deploy is a
    fleet-level event.
    """
    if not channels:
        raise ValueError("a scenario needs at least one channel")
    rng = random.Random(seed)
    plan = ScenarioPlan(duration_s=duration_s, channels=tuple(channels))

    # --- deploys, and the artifacts the perturbing ones cause ---
    t = 0.0
    n = 0
    while True:
        t += rng.expovariate(deploys_per_hour / 3600.0) if deploys_per_hour > 0 else float("inf")
        if t >= duration_s:
            break
        n += 1
        end = min(t + rng.uniform(*deploy_duration_s), duration_s)
        touched = _touched_channels(rng, channels)
        perturbs = rng.random() >= quiet_deploy_fraction

        artifacts: dict[str, list[InjectedEpisode]] = {}
        if perturbs:
            for channel in touched:
                # The excursion starts shortly after the rollout begins, not at the exact
                # instant -- a restart takes a moment to show up in telemetry.
                onset = t + rng.uniform(0.0, max(1.0, (end - t) * 0.4))
                artifacts.setdefault(channel, []).append(_artifact_episode(rng, onset))

        event = ContextEvent(
            event_id=f"deploy-{n:04d}",
            kind=ContextKind.DEPLOY,
            t_start_ms=int(t * 1000),
            t_end_ms=int(end * 1000),
            severity=Severity.INFO if not perturbs else Severity.WARNING,
            detail=rng.choice(_DEPLOY_DETAILS).format(
                version=f"v1.{rng.randint(2, 40)}.{rng.randint(0, 9)}",
                fleet=rng.choice(("california", "nevada", "arizona")),
            ),
            scope=touched,
            perturbed_telemetry=perturbs,
        )
        plan.deploys.append(ScheduledDeploy(event=event, artifacts=artifacts))

    # --- real faults, deliberately split inside and outside deploy windows ---
    total_faults = max(1, int(round(faults_per_hour * duration_s / 3600.0)))
    want_inside = int(round(total_faults * fault_in_window_fraction))

    placed_inside = 0
    if plan.deploys:
        # Draw from both perturbing and quiet deploys. Faults during a quiet deploy are the
        # sharpest test: there is no artifact to attribute them to, so suppressing them
        # could only ever be blanket muting.
        for _ in range(want_inside * 4):
            if placed_inside >= want_inside:
                break
            deploy = rng.choice(plan.deploys)
            e = deploy.event
            span_s = (e.t_end_ms - e.t_start_ms) / 1000.0
            if span_s <= 0 or not e.scope:
                continue
            channel = rng.choice(e.scope)
            start_s = e.t_start_ms / 1000.0 + rng.uniform(0.0, span_s)
            plan.faults.setdefault(channel, []).append(_fault_episode(rng, start_s))
            placed_inside += 1

    deploy_spans = [(d.event.t_start_ms / 1000.0, d.event.t_end_ms / 1000.0) for d in plan.deploys]
    placed_outside = 0
    want_outside = total_faults - placed_inside
    for _ in range(want_outside * 20):
        if placed_outside >= want_outside:
            break
        start_s = rng.uniform(0.0, duration_s)
        if any(lo <= start_s <= hi for lo, hi in deploy_spans):
            continue
        channel = rng.choice(channels)
        plan.faults.setdefault(channel, []).append(_fault_episode(rng, start_s))
        placed_outside += 1

    for episodes in plan.faults.values():
        episodes.sort(key=lambda e: e.start_s)

    return plan


def _touched_channels(rng: random.Random, channels: tuple[str, ...]) -> tuple[str, ...]:
    """A deploy touches a subset, never reliably the whole fleet.

    If every deploy were fleet-wide, scope would carry no information and a policy that
    ignored it would score identically to one that respected it.
    """
    if len(channels) == 1:
        return channels
    share = rng.choice((0.2, 0.35, 0.5, 1.0))
    k = max(1, int(round(len(channels) * share)))
    return tuple(sorted(rng.sample(list(channels), k)))
