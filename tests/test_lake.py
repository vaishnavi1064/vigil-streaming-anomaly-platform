"""Integration tests against a real Iceberg lake: Postgres catalog, MinIO object storage.

Marked `integration`: they need `docker compose up -d`. Run the fast suite with
`pytest -m "not integration"`.

A real catalog and a real bucket rather than fakes, because the properties asserted here are
properties of Iceberg and of S3, not of our code: that a snapshot's summary survives a commit
and can be read back, that a commit is atomic across data files and that summary, and that
scanning the table returns what was written to object storage. The effectively-once claim in
ADR-044 rests entirely on the first two, so testing them against a mock would be testing the
mock.

Each test gets its own namespace so they can run in any order.
"""

from __future__ import annotations

import uuid

import pytest

from vigil.lake import ReadingsLake
from vigil.readings import Reading
from vigil.reconciliation.lake_audit import audit_lake, compare_to_ledger
from vigil.settings import LakeSettings, MissingSetting

pytestmark = pytest.mark.integration

BASE_MS = 1_757_000_000_000


@pytest.fixture(scope="module")
def base_settings() -> LakeSettings:
    try:
        settings = LakeSettings.from_env()
    except MissingSetting as exc:
        pytest.skip(f"lake not configured: {exc}")
    probe = ReadingsLake(settings)
    try:
        probe.catalog().list_namespaces()
    except Exception as exc:  # noqa: BLE001 - an unreachable catalog is a skip
        pytest.skip(f"iceberg catalog not reachable: {exc}")
    return settings


@pytest.fixture
def lake(base_settings: LakeSettings):
    """A throwaway namespace per test, dropped afterwards."""
    from dataclasses import replace

    namespace = f"vigil_test_{uuid.uuid4().hex[:12]}"
    store = ReadingsLake(replace(base_settings, namespace=namespace))
    store.ensure_table()
    try:
        yield store
    finally:
        try:
            store.drop()
            store.catalog().drop_namespace(namespace)
        except Exception:  # noqa: BLE001 - cleanup failure must not mask a test result
            pass


def readings(
    n: int, *, channel: str = "pump-01.flow", start_seq: int = 1, start_ms: int = BASE_MS
) -> list[Reading]:
    return [
        Reading(
            channel=channel,
            seq=start_seq + i,
            event_ts_ms=start_ms + (start_seq + i) * 1000,
            value=10.0 + i * 0.5,
        )
        for i in range(n)
    ]


# ------------------------------- the table -------------------------------


def test_the_table_lands_on_object_storage(lake: ReadingsLake):
    table = lake.ensure_table()

    assert table.location().startswith("s3://")
    assert [f.name for f in table.schema().fields] == [
        "channel",
        "seq",
        "event_ts",
        "value",
        "injected",
        "origin",
    ]


def test_ensure_table_is_idempotent(lake: ReadingsLake):
    first = lake.ensure_table().location()
    second = lake.ensure_table().location()

    assert first == second


def test_it_is_partitioned_by_event_day_not_ingest_day(lake: ReadingsLake):
    """A replayed day of old data belongs in its own partition, not in today's."""
    fields = lake.ensure_table().spec().fields

    assert [f.name for f in fields] == ["event_day"]


def test_readings_round_trip_through_object_storage(lake: ReadingsLake):
    assert lake.append(readings(300), {"sensor.readings-0": 300}) == 300

    assert lake.row_count() == 300
    (channel,) = lake.channel_identities()
    assert channel.readings == 300
    assert channel.seq_min == 1
    assert channel.seq_max == 300
    assert channel.drift == 0


def test_an_empty_batch_writes_no_snapshot(lake: ReadingsLake):
    assert lake.append([], {"sensor.readings-0": 5}) == 0
    assert lake.snapshot_count() == 0


# ------------------------------- offsets in the snapshot -------------------------------


def test_offsets_are_absent_until_something_is_committed(lake: ReadingsLake):
    """Empty is not offset 0: one means nothing was consumed, the other means one record was."""
    assert lake.committed_offsets() == {}


def test_a_commit_records_the_offsets_that_produced_it(lake: ReadingsLake):
    lake.append(readings(100), {"sensor.readings-0": 100, "sensor.readings-1": 250})

    assert lake.committed_offsets() == {"sensor.readings-0": 100, "sensor.readings-1": 250}


def test_the_newest_snapshot_wins(lake: ReadingsLake):
    lake.append(readings(50, start_seq=1), {"sensor.readings-0": 50})
    lake.append(readings(50, start_seq=51), {"sensor.readings-0": 100})

    assert lake.committed_offsets() == {"sensor.readings-0": 100}
    assert lake.snapshot_count() == 2
    assert lake.row_count() == 100


def test_offsets_and_rows_land_in_the_same_commit(lake: ReadingsLake):
    """The whole of ADR-044: there is no window where one exists without the other."""
    lake.append(readings(80), {"sensor.readings-0": 80})

    table = lake.table()
    snapshot = table.current_snapshot()
    assert snapshot.summary["vigil.offsets"] == '{"sensor.readings-0": 80}'
    assert snapshot.summary["vigil.rows"] == "80"
    # Same snapshot id for both facts, which is what makes them atomic.
    assert table.scan().to_arrow().num_rows == 80


# ------------------------------- identity and auditing -------------------------------


def test_a_redelivered_batch_is_a_duplicate(lake: ReadingsLake):
    """What at-least-once would look like, so the effectively-once claim can be falsified."""
    batch = readings(120)
    lake.append(batch, {"sensor.readings-0": 120})
    lake.append(batch, {"sensor.readings-0": 120})

    assert lake.row_count() == 240
    assert lake.duplicate_rows() == 120
    assert not audit_lake(lake).clean


def test_a_gap_in_the_sequence_is_found(lake: ReadingsLake):
    lake.append(readings(50, start_seq=1), {"sensor.readings-0": 50})
    lake.append(readings(50, start_seq=101), {"sensor.readings-0": 100})

    (channel,) = lake.channel_identities()
    assert channel.readings == 100
    assert channel.span == 150
    assert channel.drift == 50
    assert not audit_lake(lake).clean


def test_a_producer_restart_is_not_a_duplicate(lake: ReadingsLake):
    """ADR-046. Two distinct readings that reuse a seq must not read as a sink fault.

    Without event_ts in the identity these would be counted as 60 duplicates and the lake
    would be declared unclean for something the producer did and the sink handled correctly.
    """
    lake.append(readings(60, start_seq=1, start_ms=BASE_MS), {"sensor.readings-0": 60})
    lake.append(
        readings(60, start_seq=1, start_ms=BASE_MS + 86_400_000), {"sensor.readings-0": 120}
    )

    audit = audit_lake(lake)
    assert audit.rows == 120
    assert audit.readings == 120
    assert audit.duplicate_rows == 0
    assert audit.sequence_gaps == 0
    assert audit.reused_seq == 60
    assert audit.clean


def test_a_clean_lake_reports_itself_clean(lake: ReadingsLake):
    lake.append(readings(200), {"sensor.readings-0": 200})

    audit = audit_lake(lake)
    assert audit.clean
    assert audit.rows == audit.readings == 200
    assert audit.channels == 1
    assert audit.snapshots == 1
    assert "clean" in audit.line()


def test_channels_are_audited_separately(lake: ReadingsLake):
    lake.append(readings(100, channel="pump-01.flow"), {"sensor.readings-0": 100})
    lake.append(readings(40, channel="pump-02.flow"), {"sensor.readings-0": 140})

    by_name = {c.channel: c for c in lake.channel_identities()}
    assert by_name["pump-01.flow"].readings == 100
    assert by_name["pump-02.flow"].readings == 40


# ------------------------------- agreement with the ledger -------------------------------


def test_the_lake_and_the_ledger_agreeing_is_the_point(lake: ReadingsLake):
    lake.append(readings(150, channel="pump-01.flow"), {"sensor.readings-0": 150})

    agreements = compare_to_ledger(audit_lake(lake), {"pump-01.flow": 150})

    assert len(agreements) == 1
    assert agreements[0].agrees
    assert agreements[0].difference == 0


def test_a_channel_the_lake_never_received_is_surfaced_not_dropped(lake: ReadingsLake):
    """The finding this comparison exists for: data the ledger saw and the lake did not."""
    lake.append(readings(150, channel="pump-01.flow"), {"sensor.readings-0": 150})

    agreements = compare_to_ledger(
        audit_lake(lake), {"pump-01.flow": 150, "pump-99.missing": 4_000}
    )

    by_name = {a.channel: a for a in agreements}
    assert by_name["pump-99.missing"].lake_readings == 0
    assert by_name["pump-99.missing"].difference == -4_000
    assert not by_name["pump-99.missing"].agrees


def test_a_shortfall_against_the_ledger_is_reported_signed(lake: ReadingsLake):
    lake.append(readings(90, channel="pump-01.flow"), {"sensor.readings-0": 90})

    (agreement,) = compare_to_ledger(audit_lake(lake), {"pump-01.flow": 100})

    assert agreement.difference == -10
    assert not agreement.agrees


def test_an_empty_lake_audits_without_raising(lake: ReadingsLake):
    audit = audit_lake(lake)

    assert audit.rows == 0
    assert audit.channels == 0
    assert audit.clean
