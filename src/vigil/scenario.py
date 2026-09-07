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

**Where the excursions land is now as deliberate as when.** The four measurements in
`docs/EVALUATION.md` section 3.7 established that timing alone cannot separate a deploy
artifact from a real fault. The distinction that remains is structural: a rollout reaches a
deploy ring, which is built to cut across physical failure domains, while a physical unit
failing moves its own metrics and nothing else. So the generator places excursions on the
inventory in `vigil.topology`:

  - a **deploy** perturbs channels across the nodes of one ring, usually the same metric
    family on each -- which is what redeploying a collector actually does;
  - a **node fault** moves several metrics on one machine, near-simultaneously, which is
    the trap for a timing-only discriminator: those channels *are* synchronous, they *are*
    in scope, and attributing them to the deploy costs a real fault;
  - a **rack fault** moves channels across two machines in one cabinet. It is the trap for
    a naive "spread across nodes" rule, which would call it a deploy;
  - a minority of faults stay **single-channel**, one sensor going bad, because a fleet
    where every fault is multi-channel would make corroboration easier than it is.

Everything is scheduled up front from a seed, so a run is exactly reproducible and the
ground truth is known before a single reading is produced.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from vigil.context import ContextEvent, ContextKind, Severity
from vigil.synthetic import AnomalyKind, AnomalyOrigin, InjectedEpisode
from vigil.topology import FleetTopology

# Real deploys are mostly boring. Roughly a third of them changing nothing observable is
# both realistic and enough of a population to make blanket suppression obvious.
DEFAULT_QUIET_DEPLOY_FRACTION = 0.35

# Fraction of genuine faults deliberately placed inside a deploy window. High enough that
# losing them is unmissable in the recall number.
DEFAULT_FAULT_IN_WINDOW_FRACTION = 0.40

# A rollout confined to one machine has the same blast radius as that machine failing, so
# no topology test can separate them and the policy must decline. Kept as a real minority
# rather than excluded: a generator that only produced deploys the discriminator can win
# on would be measuring the generator.
DEFAULT_CANARY_DEPLOY_FRACTION = 0.20

# Which fault domain a real fault occupies. Node faults dominate because a physical unit is
# the usual thing that breaks; the rack slice is the population that punishes a policy which
# only checks node spread; the channel slice keeps single-sensor faults in the mix, which
# are the hardest case for any corroboration test and the one v1-v3 measured.
DEFAULT_FAULT_DOMAIN_WEIGHTS: dict[str, float] = {"node": 0.55, "rack": 0.15, "channel": 0.30}

# A pump seizing does not move its vibration and its bearing temperature at the same
# millisecond, but it moves them within seconds. Deliberately inside the policy's synchrony
# tolerance: if a real fault were never synchronous, the timing test would already have
# worked and there would be nothing here to fix.
FAULT_ONSET_SPREAD_S = 4.0

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
    # The deploy ring this rollout targeted, and the nodes inside it that it reached.
    # Ground truth for the blast-radius scoring; no policy reads this, it reads the
    # inventory and the scope.
    ring: str = ""
    nodes: tuple[str, ...] = ()

    @property
    def quiet(self) -> bool:
        return not self.artifacts

    @property
    def canary(self) -> bool:
        """A rollout to one machine. Its footprint is a fault's footprint."""
        return len(self.nodes) <= 1


@dataclass
class ScenarioPlan:
    """A complete, seeded schedule of deploys, artifacts and real faults for one run.

    Times are stream-relative seconds from the start of the run. The producer converts
    them to event-time milliseconds; nothing here needs a wall clock.
    """

    duration_s: float
    channels: tuple[str, ...]
    topology: FleetTopology = field(default_factory=FleetTopology.empty)
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

    @property
    def canary_deploys(self) -> list[ScheduledDeploy]:
        return [d for d in self.deploys if d.canary]

    def fault_incidents(self) -> dict[str, list[tuple[str, InjectedEpisode]]]:
        """Faults grouped by the incident they belong to.

        A pump failing is one thing that happened, however many of its metrics moved. An
        operator is paged once for it (ADR-016), so it is counted once.
        """
        out: dict[str, list[tuple[str, InjectedEpisode]]] = {}
        for channel, episodes in self.faults.items():
            for ep in episodes:
                out.setdefault(ep.incident or f"{channel}@{ep.start_s:.3f}", []).append(
                    (channel, ep)
                )
        return out

    def faults_by_domain(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for members in self.fault_incidents().values():
            domain = members[0][1].domain or "channel"
            counts[domain] = counts.get(domain, 0) + 1
        return counts

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
        domains = ", ".join(f"{k} {v}" for k, v in sorted(self.faults_by_domain().items()))
        return (
            f"scenario: {len(self.deploys)} deploys "
            f"({len(self.perturbing_deploys)} perturbing, {len(self.quiet_deploys)} quiet, "
            f"{len(self.canary_deploys)} single-node) | "
            f"real faults {len(self.fault_incidents())} incidents by domain [{domains}] "
            f"over {len(inside) + len(outside)} channel-episodes "
            f"({len(inside)} inside a deploy window, {len(outside)} outside)"
        )


def _artifact_episode(rng: random.Random, start_s: float, incident: str) -> InjectedEpisode:
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
        incident=incident,
        domain="ring",
    )


def _fault_episode(
    rng: random.Random, start_s: float, incident: str, domain: str
) -> InjectedEpisode:
    kind = rng.choice(list(AnomalyKind))
    duration = 0.0 if kind is AnomalyKind.SPIKE else rng.uniform(20.0, 90.0)
    magnitude = rng.uniform(4.0, 9.0) * rng.choice((-1.0, 1.0))
    return InjectedEpisode(
        kind=kind,
        start_s=start_s,
        end_s=start_s + duration,
        magnitude=magnitude,
        origin=AnomalyOrigin.FAULT,
        incident=incident,
        domain=domain,
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
    canary_deploy_fraction: float = DEFAULT_CANARY_DEPLOY_FRACTION,
    fault_domain_weights: dict[str, float] | None = None,
    deploy_duration_s: tuple[float, float] = (60.0, 180.0),
    topology: FleetTopology | None = None,
) -> ScenarioPlan:
    """Schedule one reproducible run.

    Rates are per hour across the whole fleet, not per channel, because a deploy is a
    fleet-level event. `topology` is the inventory the excursions are placed on; when it is
    not given it is derived from the channel names, which is what the loadgen does.
    """
    if not channels:
        raise ValueError("a scenario needs at least one channel")
    rng = random.Random(seed)
    fleet = topology if topology is not None else FleetTopology.from_channels(channels)
    weights = dict(fault_domain_weights or DEFAULT_FAULT_DOMAIN_WEIGHTS)
    plan = ScenarioPlan(duration_s=duration_s, channels=tuple(channels), topology=fleet)

    # --- deploys, and the artifacts the perturbing ones cause ---
    t = 0.0
    n = 0
    while True:
        t += rng.expovariate(deploys_per_hour / 3600.0) if deploys_per_hour > 0 else float("inf")
        if t >= duration_s:
            break
        n += 1
        end = min(t + rng.uniform(*deploy_duration_s), duration_s)
        touched, ring, nodes = _rollout_blast_radius(rng, fleet, channels, canary_deploy_fraction)
        perturbs = rng.random() >= quiet_deploy_fraction

        artifacts: dict[str, list[InjectedEpisode]] = {}
        if perturbs:
            for channel in touched:
                # The excursion starts shortly after the rollout begins, not at the exact
                # instant -- a restart takes a moment to show up in telemetry.
                onset = t + rng.uniform(0.0, max(1.0, (end - t) * 0.4))
                artifacts.setdefault(channel, []).append(
                    _artifact_episode(rng, onset, f"deploy-{n:04d}")
                )

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
        plan.deploys.append(
            ScheduledDeploy(event=event, artifacts=artifacts, ring=ring, nodes=nodes)
        )

    # --- real faults, deliberately split inside and outside deploy windows ---
    total_faults = max(1, int(round(faults_per_hour * duration_s / 3600.0)))
    want_inside = int(round(total_faults * fault_in_window_fraction))
    incidents = 0

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
            start_s = e.t_start_ms / 1000.0 + rng.uniform(0.0, span_s)
            incidents += 1
            members = _fault_incident(
                rng, fleet, start_s, f"fault-{incidents:04d}", weights, allowed=tuple(e.scope)
            )
            for channel, episode in members:
                plan.faults.setdefault(channel, []).append(episode)
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
        incidents += 1
        members = _fault_incident(rng, fleet, start_s, f"fault-{incidents:04d}", weights)
        for channel, episode in members:
            plan.faults.setdefault(channel, []).append(episode)
        placed_outside += 1

    for episodes in plan.faults.values():
        episodes.sort(key=lambda e: e.start_s)

    return plan


def _metric_of(channel: str) -> str:
    """The measurement a channel carries, as opposed to the machine carrying it."""
    _, _, metric = channel.partition(".")
    return metric or channel


def _rollout_blast_radius(
    rng: random.Random,
    fleet: FleetTopology,
    channels: tuple[str, ...],
    canary_fraction: float,
) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    """Which channels a rollout reaches: a metric family, across the nodes of one ring.

    Two things make this different from sampling channels at random, and both matter. A
    ring spans nodes, so the footprint is spread across the physical hierarchy rather than
    concentrated in it -- that is the signal the discriminator reads. And a rollout
    redeploys the *same collector* everywhere it lands, so it touches the same metrics on
    each node rather than an arbitrary handful, which is what makes its footprint regular
    enough to be recognised at all.
    """
    if len(channels) == 1 or not fleet.known:
        return tuple(sorted(channels)), "", ()

    ring = rng.choice(fleet.rings)
    ring_nodes = list(fleet.nodes_in_ring(ring))
    if len(ring_nodes) >= 2 and rng.random() >= canary_fraction:
        nodes = rng.sample(ring_nodes, rng.randint(2, len(ring_nodes)))
    else:
        nodes = [rng.choice(ring_nodes)]

    metrics = sorted({_metric_of(c) for node in nodes for c in fleet.channels_on(node)})
    share = rng.choice((0.34, 0.5, 0.75, 1.0))
    k = max(1, round(len(metrics) * share))
    rolled = set(rng.sample(metrics, min(k, len(metrics))))

    scope = tuple(
        sorted(c for node in nodes for c in fleet.channels_on(node) if _metric_of(c) in rolled)
    )
    return scope or tuple(sorted(fleet.channels_on(nodes[0]))), ring, tuple(sorted(nodes))


def _weighted_domain(rng: random.Random, weights: dict[str, float]) -> str:
    total = sum(max(w, 0.0) for w in weights.values()) or 1.0
    draw = rng.random() * total
    running = 0.0
    for domain, weight in weights.items():
        running += max(weight, 0.0)
        if draw < running:
            return domain
    return "channel"


def _fault_incident(
    rng: random.Random,
    fleet: FleetTopology,
    start_s: float,
    incident: str,
    weights: dict[str, float],
    allowed: tuple[str, ...] = (),
) -> list[tuple[str, InjectedEpisode]]:
    """One real fault, as the set of channels it actually moves.

    `allowed` restricts the incident to a deploy's scope, which is how a fault is placed
    *inside* a deploy window in a way that actually tests anything: the channels it moves
    are channels the deploy touched, they move within seconds of each other, and a policy
    that reads only timing and scope will attribute them to the deploy and lose a real
    fault. The topology is the only thing left that can tell the two apart.
    """
    pool = tuple(allowed) if allowed else fleet.placements and tuple(fleet.placements) or ()
    if not pool:
        return []
    domain = _weighted_domain(rng, weights) if fleet.known else "channel"

    nodes: dict[str, list[str]] = {}
    for channel in pool:
        node = fleet.node_of(channel) or channel
        nodes.setdefault(node, []).append(channel)

    members: list[str] = []
    if domain == "node":
        candidates = [n for n, cs in nodes.items() if len(cs) >= 2] or list(nodes)
        chosen = rng.choice(candidates)
        available = sorted(nodes[chosen])
        members = rng.sample(available, min(len(available), rng.randint(2, 4)))
    elif domain == "rack":
        racks: dict[str, list[str]] = {}
        for node in nodes:
            rack = next((p.rack for p in fleet.placements.values() if p.node == node), node)
            racks.setdefault(rack, []).append(node)
        candidates = [r for r, ns in racks.items() if len(ns) >= 2]
        if candidates:
            chosen_rack = rng.choice(candidates)
            for node in rng.sample(racks[chosen_rack], 2):
                available = sorted(nodes[node])
                members.extend(rng.sample(available, min(len(available), rng.randint(1, 2))))
        else:
            # No cabinet in this fleet holds two machines, so a rack fault is a node fault.
            # Recorded as what it is rather than as what was asked for.
            domain = "node"
            chosen = rng.choice(list(nodes))
            available = sorted(nodes[chosen])
            members = rng.sample(available, min(len(available), rng.randint(2, 4)))
    if not members:
        domain = "channel"
        members = [rng.choice(pool)]
    if len(members) == 1:
        domain = "channel"

    return [
        (
            channel,
            _fault_episode(rng, start_s + rng.uniform(0.0, FAULT_ONSET_SPREAD_S), incident, domain),
        )
        for channel in sorted(members)
    ]
