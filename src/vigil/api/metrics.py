"""Prometheus metrics for the API, scraped from `/metrics`.

**What this exports and what it deliberately does not.** These are *gauges read from the
stores at scrape time*, not counters incremented by the hot path. The detector, the
reconciler and the two storage sinks are separate processes; a counter maintained inside the
API process would describe the API's own life, not the pipeline's, and would reset to zero
every time the API restarted while the pipeline kept running.

Reading at scrape time costs a few queries per scrape and buys a number that is true about
the system rather than about this process. At a 30-second scrape interval against a Postgres
holding episodes and a ClickHouse holding a per-minute rollup, that is cheap. If it ever is
not, the fix is a recording rule in Prometheus, not a counter here that would be wrong in a
more interesting way.

**Every store read is guarded.** A scrape must not fail because ClickHouse is down -- that is
exactly the moment the metrics matter most. A store that cannot be reached exports its `up`
gauge as 0 and omits the numbers it could not fetch, which is distinguishable from exporting
a zero.
"""

from __future__ import annotations

import logging

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from vigil.settings import ClickHouseSettings, PostgresSettings
from vigil.store import EpisodeStore
from vigil.warehouse import ReadingsWarehouse

log = logging.getLogger("vigil.api.metrics")

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def collect() -> bytes:
    """Build a fresh registry per scrape.

    A module-level registry would accumulate stale series for channels that no longer
    report. Rebuilding means the exported set always describes what the stores hold now,
    which is the property that makes `absent()` usable in an alert rule.
    """
    registry = CollectorRegistry()

    def gauge(name: str, doc: str, labels: list[str] | None = None) -> Gauge:
        return Gauge(name, doc, labels or [], registry=registry)

    # The two `up` gauges are the only ones created unconditionally, because their whole job
    # is to report a store that did not answer. Everything else is created *after* its value
    # has been fetched: a Gauge that is registered and never set exports 0, and a scrape
    # saying `vigil_reconciliation_drift 0` when the database was unreachable is the same lie
    # as a dashboard reading "drift: 0" with nothing measuring drift. An alert rule can tell
    # an absent series from a zero one; it cannot tell a real zero from a default.
    postgres_up = gauge("vigil_postgres_up", "Whether the episode store answered this scrape")
    clickhouse_up = gauge("vigil_clickhouse_up", "Whether the serving store answered this scrape")

    try:
        with EpisodeStore(PostgresSettings.from_env()) as store:
            episode_count = store.episode_count()
            with store._conn.cursor() as cur:
                cur.execute(
                    "SELECT drift, offset_drift FROM reconciliation_runs"
                    " ORDER BY created_at DESC LIMIT 1"
                )
                latest_run = cur.fetchone()
                cur.execute("SELECT count(*) AS n FROM pipeline_health WHERE severity <> 'ok'")
                disturbed_windows = cur.fetchone()["n"]

        postgres_up.set(1)
        gauge("vigil_episodes_total", "Episodes recorded in Postgres").set(episode_count)
        gauge(
            "vigil_pipeline_health_disturbed_windows", "Health windows whose severity is not ok"
        ).set(disturbed_windows)
        if latest_run is not None:
            # Absent until a reconciliation run has actually happened, so `absent()` in an
            # alert rule means "nothing has ever audited this" rather than "audited, clean".
            gauge(
                "vigil_reconciliation_drift",
                "Drift from the most recent reconciliation run. Zero is the claim",
            ).set(float(latest_run["drift"]))
            gauge(
                "vigil_reconciliation_offset_drift",
                "Independent broker-offset audit from the most recent reconciliation run",
            ).set(float(latest_run["offset_drift"]))
    except Exception as exc:  # noqa: BLE001 - a scrape reports failure, it does not raise it
        postgres_up.set(0)
        log.warning("metrics: postgres unreachable: %s", exc)

    try:
        with ReadingsWarehouse(ClickHouseSettings.from_env()) as warehouse:
            rows = warehouse.reading_count()
            per_channel = warehouse.channels()

        clickhouse_up.set(1)
        gauge(
            "vigil_reading_rows_total",
            "Rows stored in the serving store including pre-merge duplicates",
        ).set(rows)
        readings = gauge(
            "vigil_readings_total",
            "Distinct readings in the serving store, counted by identity so a replay does not "
            "inflate it",
            ["channel"],
        )
        channel_gap = gauge(
            "vigil_channel_sequence_gap",
            "Sequence span minus readings for a channel. Non-zero means the identity invariant "
            "does not hold and data is missing",
            ["channel"],
        )
        for channel in per_channel:
            readings.labels(channel=channel["channel"]).set(channel["readings"])
            span = channel["seq_max"] - channel["seq_min"] + 1
            channel_gap.labels(channel=channel["channel"]).set(span - channel["readings"])
    except Exception as exc:  # noqa: BLE001 - same rule for the serving store
        clickhouse_up.set(0)
        log.warning("metrics: clickhouse unreachable: %s", exc)

    return generate_latest(registry)
