"""The Iceberg event lake: durable readings, and the reconciliation source of truth.

**Why a lake at all, when Kafka already holds the stream.** Kafka retains six hours here and
two gigabytes at most; it is a transport, not an archive. The lake is where a reading is still
readable next month, and it is what makes the reconciliation claim auditable after the fact
rather than only live -- a harness that can only check the broker's current contents cannot
answer a question about last Tuesday.

**The catalog is Postgres, the data files are on MinIO** (ADR-043). A JDBC catalog is a real
Iceberg catalog implementation, it survives restarts, it is backed up with the application
database, and it needs no extra container -- which matters on an 8.1 GB VM that already runs
four services.

**Effectively-once, by putting the offsets in the snapshot** (ADR-044). Each commit records
the Kafka offsets the batch consumed in the snapshot summary, so the data and the statement
of what produced it land atomically: Iceberg commits both or neither. A sink that crashes
resumes from the offsets in the current snapshot, which means the batch that was in flight is
re-read and re-written rather than half-written or skipped. The lake therefore holds no
duplicates and no gaps, and the claim rests on Iceberg's commit atomicity rather than on our
own bookkeeping being lucky.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from vigil.readings import Reading
from vigil.settings import LakeSettings

log = logging.getLogger("vigil.lake")

READINGS_TABLE = "readings"

# The snapshot summary key the sink writes its consumed offsets under. Namespaced because
# Iceberg's own summary keys share that dictionary.
OFFSETS_PROPERTY = "vigil.offsets"
ROWS_PROPERTY = "vigil.rows"


@dataclass(frozen=True)
class ChannelIdentity:
    """One channel's identity facts, as the lake holds them.

    The same shape the reconciliation ledger computes from the stream, so the two can be
    compared directly instead of through a translation that could hide a disagreement.

    A reading is identified by `(channel, seq, event_ts)` rather than `(channel, seq)`
    (ADR-046). The distinction is not pedantic: the producer's sequence restarts when the
    producer does, so `readings` and `distinct_seq` diverge across a restart, and reporting
    only one of them would either hide the reuse or mistake it for loss.
    """

    channel: str
    readings: int
    distinct_seq: int
    seq_min: int
    seq_max: int

    @property
    def span(self) -> int:
        return self.seq_max - self.seq_min + 1

    @property
    def drift(self) -> int:
        """Span minus distinct sequence numbers. Positive is a gap in the sequence, 0 is dense.

        Computed against distinct seq rather than against the reading count so that a
        producer restart, which legitimately reissues sequence numbers, does not read as a
        hole in the sequence. What a restart shows up as is `reused_seq`.
        """
        return self.span - self.distinct_seq

    @property
    def reused_seq(self) -> int:
        """Readings sharing a sequence number with another reading on the same channel.

        Non-zero means the producer's sequence restarted at least once inside this table's
        history. It is a fact about the producer, not a fault in the lake, and the two are
        worth being able to tell apart.
        """
        return self.readings - self.distinct_seq


@dataclass
class ReadingsLake:
    """The readings table in Iceberg, and the operations the platform needs on it."""

    settings: LakeSettings
    _catalog: Any = field(default=None, init=False)

    # ------------------------------------------------------------------ catalog

    def catalog(self) -> Any:
        """Built lazily. pyiceberg is an optional extra, so importing this module is free."""
        if self._catalog is None:
            from pyiceberg.catalog.sql import SqlCatalog

            self._catalog = SqlCatalog(
                "vigil",
                **{
                    "uri": self.settings.catalog_uri,
                    "warehouse": self.settings.warehouse,
                    "s3.endpoint": self.settings.s3_endpoint,
                    "s3.access-key-id": self.settings.access_key,
                    "s3.secret-access-key": self.settings.secret_key,
                    # MinIO serves one bucket per path, not per subdomain.
                    "s3.path-style-access": "true",
                    # MinIO has no region, but the AWS client insists on resolving one and
                    # logs a failure for every call when it cannot. Naming it silences a
                    # warning that would otherwise look like a real error in the sink's log.
                    "s3.region": "us-east-1",
                },
            )
        return self._catalog

    @property
    def identifier(self) -> tuple[str, str]:
        return (self.settings.namespace, READINGS_TABLE)

    def ensure_table(self) -> Any:
        """Create the namespace and table if absent. Idempotent."""
        from pyiceberg.partitioning import PartitionField, PartitionSpec
        from pyiceberg.schema import Schema
        from pyiceberg.transforms import DayTransform
        from pyiceberg.types import (
            DoubleType,
            LongType,
            NestedField,
            StringType,
            TimestamptzType,
        )

        catalog = self.catalog()
        catalog.create_namespace_if_not_exists(self.settings.namespace)
        if catalog.table_exists(self.identifier):
            return catalog.load_table(self.identifier)

        schema = Schema(
            NestedField(1, "channel", StringType(), required=True),
            NestedField(2, "seq", LongType(), required=True),
            NestedField(3, "event_ts", TimestamptzType(), required=True),
            NestedField(4, "value", DoubleType(), required=True),
            # Ground truth from the synthetic source, absent on live data. Optional because
            # "no label" and "labelled as nothing" are different facts.
            NestedField(5, "injected", StringType(), required=False),
            NestedField(6, "origin", StringType(), required=False),
        )
        # Partitioned by event day, not by ingest day: a late-arriving reading belongs in the
        # partition for when it happened, otherwise a replay of old data would scatter across
        # partitions named after the replay.
        spec = PartitionSpec(
            PartitionField(source_id=3, field_id=1000, transform=DayTransform(), name="event_day")
        )
        return catalog.create_table(self.identifier, schema=schema, partition_spec=spec)

    def table(self) -> Any:
        """Load the current table state. Always reloads: snapshots move under us."""
        return self.catalog().load_table(self.identifier)

    def drop(self) -> None:
        catalog = self.catalog()
        if catalog.table_exists(self.identifier):
            catalog.drop_table(self.identifier)

    # ------------------------------------------------------------------ writes

    def append(self, readings: Sequence[Reading], offsets: dict[str, int]) -> int:
        """Append a batch and record, in the same commit, the offsets that produced it.

        The atomicity is the guarantee. Iceberg commits the data files and the snapshot
        summary together, so there is no window in which the rows exist and the record of
        where they came from does not -- which is exactly the window a separate offset store
        would have, and exactly where duplicates come from.
        """
        if not readings:
            return 0
        import pyarrow as pa

        table = self.ensure_table()
        arrow = pa.Table.from_pydict(
            {
                "channel": [r.channel for r in readings],
                "seq": [r.seq for r in readings],
                "event_ts": [
                    datetime.fromtimestamp(r.event_ts_ms / 1000.0, tz=UTC) for r in readings
                ],
                "value": [r.value for r in readings],
                "injected": [r.injected for r in readings],
                "origin": [r.origin for r in readings],
            },
            schema=table.schema().as_arrow(),
        )
        table.append(
            arrow,
            snapshot_properties={
                OFFSETS_PROPERTY: json.dumps(offsets, sort_keys=True),
                ROWS_PROPERTY: str(len(readings)),
            },
        )
        return len(readings)

    # ------------------------------------------------------------------ reads

    def committed_offsets(self) -> dict[str, int]:
        """Where to resume from: the offsets recorded by the newest snapshot.

        An empty dict means nothing has been committed yet, which the sink reads as "start
        from the beginning" -- distinct from offset 0, which means one record was consumed.
        """
        table = self.ensure_table()
        snapshot = table.current_snapshot()
        if snapshot is None:
            return {}
        raw = snapshot.summary.get(OFFSETS_PROPERTY) if snapshot.summary else None
        if not raw:
            return {}
        try:
            return {str(k): int(v) for k, v in json.loads(raw).items()}
        except (ValueError, TypeError):
            log.warning("snapshot %s has an unreadable offsets property", snapshot.snapshot_id)
            return {}

    def row_count(self) -> int:
        return self.ensure_table().scan().to_arrow().num_rows

    def snapshot_count(self) -> int:
        return len(list(self.ensure_table().snapshots()))

    def channel_identities(self) -> list[ChannelIdentity]:
        """Per-channel identity facts, computed over the whole table.

        Reads the lake rather than trusting a counter kept alongside it. That is the point of
        auditing against a lake: the numbers come from the stored bytes, so a bug in the
        sink's own accounting cannot hide in them.
        """
        arrow = self.ensure_table().scan(selected_fields=("channel", "seq", "event_ts")).to_arrow()
        if arrow.num_rows == 0:
            return []
        # Distinct identities first, so a redelivered row cannot inflate any of the counts
        # below. Everything after this operates on one row per reading.
        distinct = arrow.group_by(["channel", "seq", "event_ts"]).aggregate([])
        grouped = distinct.group_by("channel").aggregate(
            [("seq", "count"), ("seq", "min"), ("seq", "max"), ("seq", "count_distinct")]
        )
        return sorted(
            (
                ChannelIdentity(
                    channel=row["channel"],
                    readings=row["seq_count"],
                    distinct_seq=row["seq_count_distinct"],
                    seq_min=row["seq_min"],
                    seq_max=row["seq_max"],
                )
                for row in grouped.to_pylist()
            ),
            key=lambda c: c.channel,
        )

    def duplicate_rows(self) -> int:
        """Rows beyond one per (channel, seq, event_ts). Zero is the effectively-once claim.

        Keyed on the full identity, so a producer that restarted and reissued sequence
        numbers is not counted here -- those are different readings that happen to share a
        seq, and calling them duplicates would make the claim fail on a fact about the
        producer rather than on anything the sink did (ADR-046).
        """
        arrow = self.ensure_table().scan(selected_fields=("channel", "seq", "event_ts")).to_arrow()
        if arrow.num_rows == 0:
            return 0
        distinct = arrow.group_by(["channel", "seq", "event_ts"]).aggregate([]).num_rows
        return arrow.num_rows - distinct
