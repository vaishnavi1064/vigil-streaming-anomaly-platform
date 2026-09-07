"""The inventory, and the shape it lets the policy read.

Four measurements said timing alone cannot separate a deploy artifact from a real fault
(`docs/EVALUATION.md` section 3.7). What is left is structure: a rollout reaches a deploy
ring, which is laid out across physical failure domains on purpose, while a machine failing
moves its own metrics and nothing else.

These tests hold the two halves of that claim. The first half is the graph -- a ring must
cut across racks, or a ring and a rack are the same set and the topology distinguishes
nothing. The second is the discriminator, and the population that matters most there is the
rack fault: several machines moving at once, in one cabinet, inside a deploy's scope. A
policy that only checks "did this spread across machines" calls that a deploy and loses a
real fault.
"""

from __future__ import annotations

from vigil.synthetic import default_fleet
from vigil.topology import FleetTopology

FLEET_24 = tuple(spec.name for spec in default_fleet(24))
FLEET_12 = tuple(spec.name for spec in default_fleet(12))


def topology(channels=FLEET_24) -> FleetTopology:
    return FleetTopology.from_channels(channels)


# ------------------------- the graph -------------------------


def test_metrics_from_one_machine_share_a_node():
    # A pump's vibration and its bearing temperature are the same physical unit. If they
    # were separate nodes, every node fault would look like a multi-machine event.
    fleet = topology()
    assert fleet.node_of("pump-00.vibration_mm_s") == fleet.node_of("pump-00.bearing_temp_c")
    assert fleet.node_of("pump-00.vibration_mm_s") != fleet.node_of("pump-01.vibration_mm_s")


def test_a_deploy_ring_cuts_across_racks():
    """The one structural choice the whole discriminator rests on.

    Deploy rings are built to span failure domains so that a cabinet losing power and a
    rollout going bad do not look alike. Assigning rings in contiguous blocks would make a
    ring and a rack the same set of machines, and then no footprint could tell them apart.
    """
    fleet = topology()
    assert len(fleet.rings) > 1
    for ring in fleet.rings:
        racks = {p.rack for p in fleet.placements.values() if p.ring == ring}
        assert len(racks) > 1, f"{ring} sits inside a single rack, so it carries no information"


def test_a_rack_holds_several_machines_so_a_rack_fault_is_possible():
    fleet = topology()
    assert any(len(fleet.nodes_in_rack(rack)) >= 2 for rack in fleet.racks)


def test_a_narrow_fleet_collapses_to_one_ring_rather_than_rings_of_one():
    # Two rings over three machines leaves a ring of one, and a rollout to one machine is
    # indistinguishable from that machine failing. Better to say the ring level carries
    # nothing here than to pretend it does.
    fleet = topology(FLEET_12)
    assert len(fleet.nodes) == 3
    assert len(fleet.rings) == 1


def test_a_channel_with_no_structure_in_its_name_is_its_own_machine():
    fleet = FleetTopology.from_channels(("ch-00", "ch-01"))
    assert fleet.node_of("ch-00") == "ch-00"
    assert len(fleet.nodes) == 2


def test_an_empty_inventory_knows_nothing_and_says_so():
    fleet = FleetTopology.empty()
    assert not fleet.known
    assert fleet.node_of("anything") is None
    assert not fleet.distinguishes_racks


# ------------------------- footprints -------------------------


def test_a_machine_fault_is_confined_to_one_node():
    fleet = topology()
    print_me = fleet.footprint(
        ["pump-00.vibration_mm_s", "pump-00.bearing_temp_c", "pump-00.flow_m3_h"]
    )
    assert print_me.node_count == 1
    assert print_me.confined_to_one_node
    assert print_me.confined_to_one_rack


def test_a_ring_rollout_spans_machines_and_racks():
    fleet = topology()
    ring = fleet.rings[0]
    channels = [c for node in fleet.nodes_in_ring(ring) for c in fleet.channels_on(node)]
    shape = fleet.footprint(channels)
    assert shape.node_count >= 2
    assert shape.rack_count >= 2
    assert shape.rings == (ring,)


def test_a_rack_fault_spans_machines_but_not_racks():
    """The population that punishes a node-spread-only rule."""
    fleet = topology()
    rack = next(r for r in fleet.racks if len(fleet.nodes_in_rack(r)) >= 2)
    channels = [fleet.channels_on(node)[0] for node in fleet.nodes_in_rack(rack)]
    shape = fleet.footprint(channels)
    assert shape.node_count >= 2
    assert shape.confined_to_one_rack


def test_a_channel_the_inventory_does_not_know_is_reported_not_guessed():
    shape = topology().footprint(["pump-00.vibration_mm_s", "not-in-the-cmdb"])
    assert shape.unplaced == ("not-in-the-cmdb",)


# ------------------------- persistence -------------------------


def test_an_inventory_round_trips_through_its_wire_format():
    fleet = topology()
    restored = FleetTopology.from_json(fleet.to_json())
    assert restored.placements == fleet.placements


def test_the_inventory_carries_no_ground_truth():
    # It says where a channel is collected from and nothing about whether it is unwell.
    # If a label ever appeared here the discriminator would be reading the answer key.
    payload = topology().to_json()
    for forbidden in ("fault", "anomaly", "injected", "origin", "perturb", "incident"):
        assert forbidden not in payload
