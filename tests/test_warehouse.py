"""Integration tests against a real ClickHouse.

Marked `integration`: they need `docker compose up -d`. Run the fast suite with
`pytest -m "not integration"`.

A real server rather than a fake because every behaviour asserted here is a property of
ClickHouse, not of our code: that `ReplacingMergeTree` collapses a replayed batch on the
identity key, that `FINAL` sees the collapse before a background merge has run, that an
`AggregatingMergeTree` materialized view fires on inserted rows and therefore never sees
that dedupe at all. A fake would assert that the fake agrees with itself, and the third of
those properties is one this schema was corrected for after measuring it.

Each test works in its own database so they can run in any order without truncating tables
out from under each other.
"""

from __future__ import annotations

import uuid

import pytest

from vigil.readings import Reading
from vigil.settings import ClickHouseSettings, MissingSetting
from vigil.warehouse import ReadingsWarehouse
from vigil.warehouse.sink import ClickHouseSink

pytestmark = pytest.mark.integration

BASE_MS = 1_757_000_000_000


@pytest.fixture(scope="module")
def base_settings() -> ClickHouseSettings:
    try:
        settings = ClickHouseSettings.from_env()
    except MissingSetting as exc:
        pytest.skip(f"clickhouse not configured: {exc}")
    probe = ReadingsWarehouse(settings)
    try:
        if not probe.ping():
            pytest.skip("clickhouse not reachable")
    except Exception as exc:  # noqa: BLE001 - an unreachable server is a skip, not a failure
        pytest.skip(f"clickhouse not reachable: {exc}")
    finally:
        probe.close()
    return settings


@pytest.fixture
def warehouse(base_settings: ClickHouseSettings):
    """A throwaway database per test, dropped afterwards."""
    from dataclasses import replace

    name = f"vigil_test_{uuid.uuid4().hex[:12]}"
    settings = replace(base_settings, database=name)
    store = ReadingsWarehouse(settings)
    store.apply_schema()
    try:
        yield store
    finally:
        try:
            store.connect().command(f"DROP DATABASE IF EXISTS {name}")
        finally:
            store.close()


def readings(n: int, *, channel: str = "pump-01.flow", start_seq: int = 1) -> list[Reading]:
    return [
        Reading(
            channel=channel,
            seq=start_seq + i,
            event_ts_ms=BASE_MS + (start_seq + i) * 1000,
            value=10.0 + i * 0.25,
        )
        for i in range(n)
    ]


# ------------------------------- schema -------------------------------


def test_the_schema_is_idempotent(warehouse: ReadingsWarehouse):
    first = warehouse.apply_schema()
    second = warehouse.apply_schema()

    assert first == second > 0
    tables = {r[0] for r in warehouse.connect().query("SHOW TABLES").result_rows}
    assert {"readings", "window_scores", "readings_per_minute", "readings_exact"} <= tables


def test_episodes_do_not_live_here(warehouse: ReadingsWarehouse):
    """The storage split is load-bearing: ADR-006 keeps episodes in Postgres."""
    tables = {r[0] for r in warehouse.connect().query("SHOW TABLES").result_rows}

    assert "episodes" not in tables
    assert "episode_scores" not in tables


# ------------------------------- writes and identity -------------------------------


def test_readings_land_and_are_queryable(warehouse: ReadingsWarehouse):
    written = warehouse.insert_readings(readings(500))

    assert written == 500
    assert warehouse.reading_count() == 500
    (channel,) = warehouse.channels()
    assert channel["channel"] == "pump-01.flow"
    assert channel["readings"] == 500
    assert channel["seq_min"] == 1
    assert channel["seq_max"] == 500


def test_a_replayed_batch_collapses_on_the_identity_key(warehouse: ReadingsWarehouse):
    """At-least-once delivery is absorbed by the table, not hoped away."""
    batch = readings(500)
    warehouse.insert_readings(batch)
    warehouse.insert_readings(batch)

    # Raw rows still hold both copies until a merge runs. That is the honest intermediate
    # state, and it is why the exact view exists rather than being the default path.
    assert warehouse.reading_count() == 1000
    assert warehouse.reading_count(exact=True) == 500


def test_the_rollup_counts_distinct_readings_not_rows_written(warehouse: ReadingsWarehouse):
    """The defect this schema was corrected for, pinned so it cannot come back.

    A materialized view fires on the rows being inserted and never sees the ReplacingMergeTree
    dedupe that happens later at merge time. With `count()` this rollup reported 1,000 after a
    500-reading batch was replayed once. `uniqExact(seq)` is immune.
    """
    batch = readings(500)
    warehouse.insert_readings(batch)
    warehouse.insert_readings(batch)

    (channel,) = warehouse.channels()
    assert channel["readings"] == 500


def test_a_partial_replay_also_leaves_the_count_right(warehouse: ReadingsWarehouse):
    batch = readings(500)
    warehouse.insert_readings(batch)
    warehouse.insert_readings(batch[:120])

    (channel,) = warehouse.channels()
    assert channel["readings"] == 500


def test_the_rollup_agrees_with_the_sequence_span(warehouse: ReadingsWarehouse):
    """The same identity invariant the reconciliation ledger checks, on the serving copy."""
    warehouse.insert_readings(readings(750))

    (channel,) = warehouse.channels()
    span = channel["seq_max"] - channel["seq_min"] + 1
    assert span - channel["readings"] == 0


def test_a_gap_in_the_sequence_shows_up_as_a_gap(warehouse: ReadingsWarehouse):
    """The invariant has to be able to fail, or asserting it proves nothing."""
    warehouse.insert_readings(readings(100, start_seq=1))
    warehouse.insert_readings(readings(100, start_seq=201))

    (channel,) = warehouse.channels()
    span = channel["seq_max"] - channel["seq_min"] + 1
    assert channel["readings"] == 200
    assert span - channel["readings"] == 100


def test_channels_are_kept_apart(warehouse: ReadingsWarehouse):
    warehouse.insert_readings(readings(100, channel="pump-01.flow"))
    warehouse.insert_readings(readings(60, channel="pump-02.flow"))

    by_name = {c["channel"]: c for c in warehouse.channels()}
    assert by_name["pump-01.flow"]["readings"] == 100
    assert by_name["pump-02.flow"]["readings"] == 60


# ------------------------------- serving reads -------------------------------


def test_the_series_is_per_minute_and_ordered(warehouse: ReadingsWarehouse):
    warehouse.insert_readings(readings(600))

    points = warehouse.series("pump-01.flow", minutes=60)

    assert len(points) >= 2
    assert [p["minute"] for p in points] == sorted(p["minute"] for p in points)
    assert sum(p["readings"] for p in points) == 600
    assert all(p["value_min"] <= p["value_avg"] <= p["value_max"] for p in points)


def test_an_unknown_channel_has_no_series(warehouse: ReadingsWarehouse):
    warehouse.insert_readings(readings(10))

    assert warehouse.series("no-such-channel") == []


def test_window_scores_aggregate_per_detector(warehouse: ReadingsWarehouse):
    scores = [
        {
            "channel": "pump-01.flow",
            "detector": detector,
            "window_start_ms": BASE_MS + i * 10_000,
            "window_end_ms": BASE_MS + i * 10_000 + 30_000,
            "score": float(i),
            "latency_ms": None,
        }
        for detector in ("zscore", "chronos-bolt-tiny")
        for i in range(20)
    ]

    assert warehouse.insert_window_scores(scores) == 40

    by_detector = {d["detector"]: d for d in warehouse.detector_scores()}
    assert by_detector["zscore"]["windows"] == 20
    assert by_detector["zscore"]["score_max"] == 19.0
    assert by_detector["chronos-bolt-tiny"]["channels"] == 1


def test_a_score_with_no_reported_latency_stores_null_not_zero(warehouse: ReadingsWarehouse):
    """A fabricated 0.0 would be indistinguishable from a genuinely fast window."""
    warehouse.insert_window_scores(
        [
            {
                "channel": "pump-01.flow",
                "detector": "flink-zscore",
                "window_start_ms": BASE_MS,
                "window_end_ms": BASE_MS + 30_000,
                "score": 3.5,
                "latency_ms": None,
            }
        ]
    )

    rows = warehouse.connect().query("SELECT latency_ms FROM window_scores").result_rows
    assert rows == [(None,)]


def test_empty_batches_are_a_no_op(warehouse: ReadingsWarehouse):
    assert warehouse.insert_readings([]) == 0
    assert warehouse.insert_window_scores([]) == 0
    assert warehouse.reading_count() == 0


# ------------------------------- the sink's batching -------------------------------


def test_the_sink_batches_by_size(warehouse: ReadingsWarehouse):
    sink = ClickHouseSink(warehouse, batch_size=100, max_batch_age_s=3600)
    for reading in readings(99):
        sink.add_reading(reading.to_json())

    assert not sink.should_flush()
    sink.add_reading(readings(1, start_seq=100)[0].to_json())
    assert sink.should_flush()

    assert sink.flush() == 100
    assert warehouse.reading_count() == 100
    assert sink.counters.readings_written == 100


def test_the_sink_batches_by_age_so_a_quiet_channel_still_appears(warehouse: ReadingsWarehouse):
    sink = ClickHouseSink(warehouse, batch_size=10_000, max_batch_age_s=0.0)
    sink.add_reading(readings(1)[0].to_json())

    assert sink.should_flush()
    assert sink.flush() == 1


def test_an_undecodable_record_is_counted_and_does_not_stop_the_stream(
    warehouse: ReadingsWarehouse,
):
    sink = ClickHouseSink(warehouse, batch_size=10)

    assert sink.add_reading(b"{not json") is False
    assert sink.add_reading(b'{"channel": "x"}') is False
    assert sink.add_reading(readings(1)[0].to_json()) is True

    assert sink.counters.undecodable == 2
    assert sink.flush() == 1


def test_a_failed_write_raises_so_offsets_are_not_committed(warehouse: ReadingsWarehouse):
    """The delivery guarantee lives in this exception: a swallowed failure loses data."""

    class Broken:
        def insert_readings(self, _rows):
            raise RuntimeError("clickhouse unreachable")

        def insert_window_scores(self, _rows):
            return 0

    sink = ClickHouseSink(Broken(), batch_size=10)
    sink.add_reading(readings(1)[0].to_json())

    with pytest.raises(RuntimeError, match="unreachable"):
        sink.flush()
    assert sink.counters.write_failures == 1
    assert sink.pending == 1  # still buffered, so a retry can re-send it


def test_flushing_nothing_is_free(warehouse: ReadingsWarehouse):
    sink = ClickHouseSink(warehouse)

    assert sink.flush() == 0
    assert sink.counters.batches == 0
