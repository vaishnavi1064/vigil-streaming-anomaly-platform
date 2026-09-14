"""Audit the Iceberg lake as the reconciliation source of truth.

The harness in `harness.py` audits the stream as it flows: it reads the Kafka log and checks
per-channel sequence identity against what the broker says it retains. That proves the
pipeline was consistent *while it was running*, and it can only ever ask about records the
broker still holds -- six hours and two gigabytes here, after which the evidence is gone.

This audits the stored bytes instead. The lake outlives the broker's retention, so the same
invariants can be checked about last month, and they are checked against what was actually
written rather than against a counter the writer kept about itself. That is what makes the
lake a *source of truth* rather than a second copy: a bug in the sink's own accounting has
nowhere to hide, because nothing here reads the sink's accounting.

Three findings, kept separate because they mean different things:

  * **Sequence gaps.** A channel's sequence is dense within a producer lifetime, so
    `distinct_seq == max - min + 1`. A shortfall is data that never reached the lake.
  * **Duplicate rows.** More than one row per `(channel, seq, event_ts)`. Zero is the
    effectively-once claim (ADR-044), and it is a claim about the sink.
  * **Reused sequence numbers.** Readings sharing a seq with a different reading. This is a
    fact about the *producer* restarting (ADR-046), not a fault in the lake, and collapsing
    it into either category above would make the sink look broken for something it did
    correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vigil.lake import ChannelIdentity, ReadingsLake


@dataclass(frozen=True)
class LakeAudit:
    """What the stored table says about itself."""

    rows: int
    readings: int
    channels: int
    snapshots: int
    sequence_gaps: int
    duplicate_rows: int
    reused_seq: int
    committed_offsets: dict[str, int] = field(default_factory=dict)
    per_channel: list[ChannelIdentity] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Gaps and duplicates only. Reused sequence numbers are the producer's doing."""
        return self.sequence_gaps == 0 and self.duplicate_rows == 0

    def line(self) -> str:
        verdict = "clean" if self.clean else "NOT CLEAN"
        return (
            f"lake audit: {self.rows:,} rows | {self.readings:,} readings | "
            f"{self.channels} channels | {self.snapshots} snapshots | "
            f"gaps {self.sequence_gaps} | duplicates {self.duplicate_rows} | "
            f"reused-seq {self.reused_seq:,} | {verdict}"
        )


def audit_lake(lake: ReadingsLake) -> LakeAudit:
    """Compute every finding from the stored table in one pass over its identity columns."""
    identities = lake.channel_identities()
    return LakeAudit(
        rows=lake.row_count(),
        readings=sum(c.readings for c in identities),
        channels=len(identities),
        snapshots=lake.snapshot_count(),
        sequence_gaps=sum(c.drift for c in identities),
        duplicate_rows=lake.duplicate_rows(),
        reused_seq=sum(c.reused_seq for c in identities),
        committed_offsets=lake.committed_offsets(),
        per_channel=identities,
    )


@dataclass(frozen=True)
class LedgerAgreement:
    """Whether the live ledger and the stored lake tell the same story.

    Two counts derived differently agreeing is the only reason to believe either. The live
    ledger counts records as they stream past; the lake counts rows written to object
    storage. They share no code and no state, so a disagreement is a real finding.
    """

    channel: str
    ledger_readings: int
    lake_readings: int

    @property
    def difference(self) -> int:
        return self.lake_readings - self.ledger_readings

    @property
    def agrees(self) -> bool:
        return self.difference == 0


def compare_to_ledger(audit: LakeAudit, ledger_by_channel: dict[str, int]) -> list[LedgerAgreement]:
    """Line up the lake's per-channel counts against the harness's.

    Every channel either side knows about appears, including ones only one of them saw --
    a channel the ledger counted and the lake never received is exactly the finding this
    comparison exists to surface, and dropping it for lack of a pair would hide it.
    """
    lake_by_channel = {c.channel: c.readings for c in audit.per_channel}
    return [
        LedgerAgreement(
            channel=channel,
            ledger_readings=ledger_by_channel.get(channel, 0),
            lake_readings=lake_by_channel.get(channel, 0),
        )
        for channel in sorted(set(lake_by_channel) | set(ledger_by_channel))
    ]
