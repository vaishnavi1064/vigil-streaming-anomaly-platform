"""The blast-radius discriminator, and the populations it has to get right.

Timing says these channels moved together. It cannot say why: a pump seizing moves its own
vibration, bearing temperature and flow within seconds of each other, and if that pump is
inside a deploy's scope then a timing-only policy attributes a real fault to the deploy and
nobody is paged. That is not a hypothetical -- it is what v1 to v3 measured, and it is why
two of seven attributions in v3 cost a real fault.

The tests below are organised by what the policy must not do:

  - attribute a machine failing to a rollout, however synchronous and however in-scope;
  - attribute a **cabinet** failing to a rollout, which is the case a naive "spread across
    machines" rule gets wrong;
  - attribute anything to a rollout whose own blast radius is one machine, because then the
    two footprints are the same and no evidence can separate them;
  - decline to attribute a genuine ring rollout, which is where the false-positive
    reduction has to come from.
"""

from __future__ import annotations

from vigil.conditioning.policy import (
    ConditioningPolicy,
    ConditioningThresholds,
    FlaggedWindowIndex,
    Verdict,
)
from vigil.conditioning.signals import StaticContextSource
from vigil.context import ContextEvent, ContextKind, Severity
from vigil.episodes import Episode, EpisodeStatus
from vigil.synthetic import default_fleet
from vigil.topology import FleetTopology

FLEET = tuple(spec.name for spec in default_fleet(24))
TOPOLOGY = FleetTopology.from_channels(FLEET)

ONSET_MS = 120_000


def channels_on(node: str) -> tuple[str, ...]:
    return TOPOLOGY.channels_on(node)


def ring_scope(ring: str, metrics: int = 1) -> tuple[str, ...]:
    """A rollout's scope: the same metric family, on every machine in the ring.

    Not every channel on those machines. Redeploying the vibration collector touches the
    vibration series everywhere it runs and leaves the bearing thermometers alone, and the
    difference matters here: a scope of every channel in the ring would make any handful of
    them a small fraction of it, and the fraction test would reject before the topology
    test was ever consulted.
    """
    families = sorted({c.partition(".")[2] for c in FLEET})[:metrics]
    return tuple(
        sorted(
            c
            for node in TOPOLOGY.nodes_in_ring(ring)
            for c in TOPOLOGY.channels_on(node)
            if c.partition(".")[2] in families
        )
    )


def episode(channel: str, onset_ms: int = ONSET_MS) -> Episode:
    return Episode(
        channel=channel,
        t_start_ms=100_000,
        t_end_ms=140_000,
        raised_by="zscore",
        peak_score=40.0,
        window_count=3,
        threshold=8.0,
        onset_ms=onset_ms,
    )


def deploy(scope: tuple[str, ...], event_id: str = "deploy-0001") -> ContextEvent:
    return ContextEvent(
        event_id=event_id,
        kind=ContextKind.DEPLOY,
        t_start_ms=0,
        t_end_ms=300_000,
        severity=Severity.WARNING,
        detail="rollout of inverter-telemetry-collector v1.9.2",
        scope=scope,
    )


def policy_for(
    event: ContextEvent,
    moved: tuple[str, ...],
    *,
    topology: FleetTopology | None = TOPOLOGY,
    **thresholds,
) -> ConditioningPolicy:
    """A policy whose index holds exactly the channels that moved, all synchronously."""
    index = FlaggedWindowIndex(synchrony_ms=5_000)
    for offset, channel in enumerate(moved):
        index.record(channel, ONSET_MS + offset * 700, ONSET_MS + 30_000)
    return ConditioningPolicy(
        source=StaticContextSource([event]),
        index=index,
        topology=topology if topology is not None else FleetTopology.empty(),
        thresholds=ConditioningThresholds(synchrony_ms=5_000, **thresholds),
    )


# ------------------------- what it must not attribute -------------------------


def test_a_machine_failing_inside_a_deploy_is_still_a_real_fault():
    """The case that costs recall in every measurement so far.

    Three metrics on one pump, moving within two seconds of each other, all of them
    channels the deploy touched. Every timing and scope test says deploy. The pump says
    otherwise, and the pump is right.
    """
    ring = TOPOLOGY.rings[0]
    node = TOPOLOGY.nodes_in_ring(ring)[0]
    # The deploy rolled three metric families across the ring, so three of this machine's
    # channels are inside its scope. All three move together when the machine seizes.
    scope = ring_scope(ring, metrics=3)
    moved = tuple(c for c in channels_on(node) if c in scope)
    assert len(moved) == 3

    decision = policy_for(deploy(scope), moved).decide(episode(moved[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.FAULT_DOMAIN
    assert node in decision.reason


def test_the_same_case_is_attributed_away_when_the_topology_test_is_off():
    """The control, and the reason this test file exists.

    With `require_blast_radius` off the policy is exactly the one v3 measured, and it
    attributes the machine fault to the deploy. The difference between the two verdicts is
    the whole contribution of this change.
    """
    ring = TOPOLOGY.rings[0]
    scope = ring_scope(ring, metrics=3)
    moved = tuple(c for c in channels_on(TOPOLOGY.nodes_in_ring(ring)[0]) if c in scope)

    decision = policy_for(deploy(scope), moved, require_blast_radius=False).decide(
        episode(moved[0])
    )
    assert decision.status is EpisodeStatus.ATTRIBUTED
    assert decision.verdict is Verdict.CORROBORATED


def test_a_cabinet_failing_is_not_a_rollout_even_though_it_spans_machines():
    """The population a node-spread-only rule gets wrong.

    Two machines in one rack lose power together. They are two nodes, so "did this spread
    across machines" says deploy. They are one cabinet, and a ring does not stop at a
    cabinet boundary.
    """
    ring = TOPOLOGY.rings[0]
    scope = ring_scope(ring)
    # Two machines of this ring that share a cabinet. Rings are round-robin over nodes and
    # racks hold three, so such a pair exists -- which is the point of that layout.
    rack = next(
        r
        for r in TOPOLOGY.racks
        if len([n for n in TOPOLOGY.nodes_in_rack(r) if n in TOPOLOGY.nodes_in_ring(ring)]) >= 2
    )
    nodes = [n for n in TOPOLOGY.nodes_in_rack(rack) if n in TOPOLOGY.nodes_in_ring(ring)][:2]
    moved = tuple(c for node in nodes for c in channels_on(node) if c in scope)

    decision = policy_for(deploy(scope), moved).decide(episode(moved[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.FAULT_DOMAIN
    assert rack in decision.reason


def test_a_rollout_to_one_machine_explains_nothing_because_a_fault_looks_the_same():
    # ADR-025's reasoning applied to shape rather than to count: with no way to tell the
    # two apart, "cannot tell" is answered with "raise".
    node = TOPOLOGY.nodes[0]
    scope = channels_on(node)
    decision = policy_for(deploy(scope), scope[:3]).decide(episode(scope[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.NARROW_BLAST_RADIUS


# ------------------------- what it must attribute -------------------------


def test_a_ring_rollout_across_machines_and_racks_is_attributed():
    ring = TOPOLOGY.rings[0]
    scope = ring_scope(ring)
    moved = tuple(TOPOLOGY.channels_on(node)[0] for node in TOPOLOGY.nodes_in_ring(ring))

    decision = policy_for(deploy(scope), moved).decide(episode(moved[0]))
    assert decision.status is EpisodeStatus.ATTRIBUTED
    assert decision.verdict is Verdict.CORROBORATED
    assert "rack" in decision.reason


def test_the_attribution_reason_describes_the_footprint_an_operator_can_check():
    ring = TOPOLOGY.rings[0]
    scope = ring_scope(ring)
    moved = tuple(TOPOLOGY.channels_on(node)[0] for node in TOPOLOGY.nodes_in_ring(ring))
    reason = policy_for(deploy(scope), moved).decide(episode(moved[0])).reason
    assert "node(s)" in reason
    assert ring in reason


# ------------------------- absence of evidence -------------------------


def test_without_an_inventory_the_topology_raises_no_objection():
    # Not the same as approving: the timing test still has to pass. But an unknown fleet
    # must not silently start rejecting every attribution, or a missing CMDB would quietly
    # turn conditioning off and nobody would see it in the numbers.
    ring = TOPOLOGY.rings[0]
    scope = ring_scope(ring, metrics=3)
    moved = tuple(c for c in channels_on(TOPOLOGY.nodes_in_ring(ring)[0]) if c in scope)
    decision = policy_for(deploy(scope), moved, topology=None).decide(episode(moved[0]))
    assert decision.verdict is Verdict.CORROBORATED


def test_a_channel_missing_from_the_inventory_is_not_guessed_at():
    ring = TOPOLOGY.rings[0]
    scope = (*ring_scope(ring), "ghost-channel")
    node = TOPOLOGY.nodes_in_ring(ring)[0]
    moved = ("ghost-channel", next(c for c in channels_on(node) if c in scope))
    decision = policy_for(deploy(scope), moved).decide(episode(moved[1]))
    assert decision.verdict is Verdict.CORROBORATED


def test_a_fleet_in_one_cabinet_does_not_have_the_rack_test_applied_to_it():
    # Every machine is in rack-00, so requiring a rack spread would reject everything and
    # the policy would look like it was discriminating when it was only refusing.
    narrow = FleetTopology.from_channels(
        tuple(spec.name for spec in default_fleet(12)), nodes_per_rack=8
    )
    assert not narrow.distinguishes_racks
    ring = narrow.rings[0]
    nodes = narrow.nodes_in_ring(ring)[:2]
    scope = tuple(sorted(c for node in nodes for c in narrow.channels_on(node)))
    moved = tuple(narrow.channels_on(node)[0] for node in nodes)
    decision = policy_for(deploy(scope), moved, topology=narrow).decide(episode(moved[0]))
    assert decision.status is EpisodeStatus.ATTRIBUTED


# ------------------------- the safety properties still hold -------------------------


def test_the_topology_test_never_turns_a_raise_into_an_attribution():
    """It can only ever refuse. A discriminator that could also approve would be a second
    way to suppress, and every way to suppress has to be argued for separately."""
    ring = TOPOLOGY.rings[0]
    scope = ring_scope(ring)
    lonely = TOPOLOGY.channels_on(TOPOLOGY.nodes_in_ring(ring)[0])[0]
    decision = policy_for(deploy(scope), (lonely,)).decide(episode(lonely))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.ISOLATED


def test_an_out_of_scope_channel_is_still_refused_before_the_topology_is_consulted():
    ring, other = TOPOLOGY.rings[0], TOPOLOGY.rings[1]
    scope = ring_scope(ring)
    outsider = ring_scope(other)[0]
    decision = policy_for(deploy(scope), (outsider,)).decide(episode(outsider))
    assert decision.verdict is Verdict.OUT_OF_SCOPE
