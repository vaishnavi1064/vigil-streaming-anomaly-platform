"""Where a channel physically lives, and which deploy ring reaches it.

Conditioning has so far asked a purely temporal question -- did these channels move at the
same instant -- and four measurements say timing alone is too weak to separate a deploy
artifact from a real fault (`docs/EVALUATION.md` section 3.7). Timing is not the only thing
that differs between the two, and it is not even the most characteristic one. **Shape is.**

A deploy and a fault have different *blast radii*, and the difference is structural rather
than statistical:

  - A rollout reaches a **deploy ring**. Rings are built to cut across physical failure
    domains on purpose -- you do not put a whole ring in one cabinet, because then a rack
    losing power and a ring going bad look identical to everyone downstream. So a deploy
    artifact appears on channels scattered across nodes and racks.
  - A fault is a property of a **physical unit**. A pump seizing moves its own vibration,
    its own bearing temperature and its own flow, and nothing on the pump beside it. Its
    footprint is concentrated in one fault domain by definition.

That gives a discriminator that does not depend on a threshold: an excursion whose channels
are confined to one physical fault domain is a fault, however well-timed; an excursion
spread across the domains a deploy ring spans is an artifact. This module is the graph that
question is asked against.

Prior art. MSCRED (arXiv 1811.08055) makes the same move in a different form -- it scores
the *inter-channel correlation structure* of a multivariate window rather than each series
alone, because the signature of a system-level event lives in which channels move together
rather than in any one of them. "Shape over Intensity" (arXiv 2607.05317) makes the
complementary point for the univariate case: the morphology of an excursion carries more
information than its magnitude. What is added here is that the relevant structure is not
learned from the data at all -- it is *known*, from the inventory, and an operator can read
why an episode was attributed without inspecting a correlation matrix.

The inventory is operational fact, not ground truth: it says which host and cabinet a
channel is collected from, and carries no information about whether anything is wrong. It
comes from a CMDB in a real deployment; here the synthetic fleet writes it, because here the
synthetic fleet *is* the inventory.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Placement:
    """One channel's position in the physical and rollout hierarchies."""

    channel: str
    node: str
    rack: str
    ring: str


@dataclass(frozen=True)
class Footprint:
    """The shape a set of channels makes on the topology.

    Deliberately reports counts rather than a single score. "Two nodes in one rack" and
    "two nodes in two racks" are different claims about the world, and collapsing them into
    a spread coefficient would throw away exactly the distinction a rack-level fault turns
    on.
    """

    channels: tuple[str, ...]
    nodes: tuple[str, ...]
    racks: tuple[str, ...]
    rings: tuple[str, ...]
    unplaced: tuple[str, ...]

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def rack_count(self) -> int:
        return len(self.racks)

    @property
    def confined_to_one_node(self) -> bool:
        return self.node_count == 1

    @property
    def confined_to_one_rack(self) -> bool:
        return self.rack_count == 1

    def describe(self) -> str:
        return (
            f"{len(self.channels)} channels on {self.node_count} node(s) "
            f"in {self.rack_count} rack(s), ring(s) {'/'.join(self.rings) or '-'}"
        )


@dataclass(frozen=True)
class FleetTopology:
    """The inventory: channel -> node -> rack, plus the deploy ring each node sits in.

    Empty is a legitimate state and means "no inventory available". Every question then
    answers "cannot tell", and the policy treats that the way it treats every other absence
    of evidence: it raises the episode (ADR-007, ADR-025).
    """

    placements: Mapping[str, Placement]

    # -- lookups --

    def placement(self, channel: str) -> Placement | None:
        return self.placements.get(channel)

    def node_of(self, channel: str) -> str | None:
        p = self.placements.get(channel)
        return p.node if p else None

    def channels_on(self, node: str) -> tuple[str, ...]:
        return tuple(sorted(c for c, p in self.placements.items() if p.node == node))

    def nodes_in_ring(self, ring: str) -> tuple[str, ...]:
        return tuple(sorted({p.node for p in self.placements.values() if p.ring == ring}))

    def nodes_in_rack(self, rack: str) -> tuple[str, ...]:
        return tuple(sorted({p.node for p in self.placements.values() if p.rack == rack}))

    @property
    def nodes(self) -> tuple[str, ...]:
        return tuple(sorted({p.node for p in self.placements.values()}))

    @property
    def racks(self) -> tuple[str, ...]:
        return tuple(sorted({p.rack for p in self.placements.values()}))

    @property
    def rings(self) -> tuple[str, ...]:
        return tuple(sorted({p.ring for p in self.placements.values()}))

    @property
    def known(self) -> bool:
        return bool(self.placements)

    @property
    def distinguishes_racks(self) -> bool:
        """Whether the rack level carries any information in this fleet.

        A fleet in one cabinet has a rack hierarchy that is technically present and
        practically vacuous. Saying so here keeps the policy from applying a test that
        cannot fail, and keeps the write-up from claiming a level it did not exercise.
        """
        return len(self.racks) > 1

    # -- the question the policy asks --

    def footprint(self, channels: Iterable[str]) -> Footprint:
        wanted = tuple(sorted(set(channels)))
        placed = [self.placements[c] for c in wanted if c in self.placements]
        return Footprint(
            channels=wanted,
            nodes=tuple(sorted({p.node for p in placed})),
            racks=tuple(sorted({p.rack for p in placed})),
            rings=tuple(sorted({p.ring for p in placed})),
            unplaced=tuple(c for c in wanted if c not in self.placements),
        )

    def summary(self) -> str:
        by_rack = Counter(p.rack for p in self.placements.values())
        return (
            f"{len(self.placements)} channels | {len(self.nodes)} nodes | "
            f"{len(self.racks)} racks | {len(self.rings)} deploy rings | "
            f"channels per rack {dict(sorted(by_rack.items()))}"
        )

    # -- persistence --

    def to_json(self) -> str:
        return json.dumps(
            {
                "placements": [
                    {"channel": p.channel, "node": p.node, "rack": p.rack, "ring": p.ring}
                    for p in sorted(self.placements.values(), key=lambda p: p.channel)
                ]
            },
            indent=2,
        )

    def write(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_json(cls, raw: str | bytes) -> FleetTopology:
        data = json.loads(raw)
        placements = {
            entry["channel"]: Placement(
                channel=entry["channel"],
                node=entry["node"],
                rack=entry["rack"],
                ring=entry["ring"],
            )
            for entry in data.get("placements", ())
        }
        return cls(placements=placements)

    @classmethod
    def load(cls, path: Path | str) -> FleetTopology:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def empty(cls) -> FleetTopology:
        return cls(placements={})

    # -- construction --

    @classmethod
    def from_channels(
        cls,
        channels: Iterable[str],
        *,
        nodes_per_rack: int = 3,
        ring_count: int | None = None,
    ) -> FleetTopology:
        """Place a fleet, deriving the physical unit from the channel name.

        `pump-00.vibration_mm_s` and `pump-00.bearing_temp_c` are two metrics from one
        machine, so they share a node. Everything before the first dot is that machine.
        A channel with no dot is its own node, which is the honest reading of a name that
        carries no structure.

        Racks are consecutive blocks of nodes, because cabinets are filled in order. Rings
        are assigned **round-robin across nodes**, which is the one non-obvious choice here
        and the one the whole discriminator rests on: a deploy ring is deliberately spread
        over physical failure domains, so that a rack losing power and a ring going bad do
        not look the same. Assigning rings in blocks would have made a ring and a rack the
        same set, and the topology would then distinguish nothing.

        `nodes_per_rack` is 3 rather than 2 so that a rack still holds two machines of the
        *same* ring. With two machines per rack and rings round-robin, a cabinet's two
        machines always land in different rings, and a rack fault could never fall inside
        one deploy's scope -- the population that most needs testing would be unreachable
        by construction.
        """
        ordered = sorted(set(channels))
        nodes: list[str] = []
        node_of: dict[str, str] = {}
        for channel in ordered:
            node = channel.split(".", 1)[0]
            node_of[channel] = node
            if node not in nodes:
                nodes.append(node)

        if ring_count is None:
            # One ring below four nodes: two rings over three nodes leaves a ring of one,
            # and a rollout to a single machine is indistinguishable from that machine
            # failing -- a population worth having, but not worth making half the fleet.
            ring_count = max(1, len(nodes) // 3)
        ring_count = max(1, min(ring_count, len(nodes) or 1))

        rack_of: dict[str, str] = {}
        ring_of: dict[str, str] = {}
        for index, node in enumerate(nodes):
            rack_of[node] = f"rack-{index // max(nodes_per_rack, 1):02d}"
            ring_of[node] = f"ring-{index % ring_count:02d}"

        return cls(
            placements={
                channel: Placement(
                    channel=channel,
                    node=node_of[channel],
                    rack=rack_of[node_of[channel]],
                    ring=ring_of[node_of[channel]],
                )
                for channel in ordered
            }
        )
