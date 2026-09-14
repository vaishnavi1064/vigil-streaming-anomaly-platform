"""FastAPI backend for the dashboard.

Deliberately thin: it shapes stored data for the UI and owns no detection logic. That
separation is what lets the API be down without detection being affected.

Phase 1 serves what Phase 1 has produced -- episodes, per-detector counts, and hot-path
latency percentiles. The reconciliation and drift panel needs the Phase 2 harness, and the
endpoint is absent until then rather than present and returning zeros: a panel that reads
"drift: 0" because nothing is measuring drift is worse than no panel at all.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from vigil.api.dashboard import DASHBOARD_HTML
from vigil.settings import ClickHouseSettings, PostgresSettings
from vigil.store import EpisodeStore
from vigil.warehouse import ReadingsWarehouse

app = FastAPI(
    title="Vigil",
    description="Context-conditioned anomaly detection on a provably-correct streaming backbone",
    version="0.1.0",
)

_STARTED = time.time()


def _store() -> EpisodeStore:
    # A connection per request. Phase 1 traffic is one dashboard; a pool is the sort of
    # thing worth adding when there is a measurement showing it is needed.
    return EpisodeStore(PostgresSettings.from_env())


def _warehouse() -> ReadingsWarehouse:
    """The serving store. Separate from `_store` because they answer different questions.

    Postgres holds episodes and app state and stays their source of truth; ClickHouse holds
    the high-volume readings and window scores that no transactional store should be asked
    to aggregate (ADR-006, ADR-045). Nothing is mirrored between them, so no endpoint has to
    decide which copy to believe.
    """
    return ReadingsWarehouse(ClickHouseSettings.from_env())


@app.get("/health")
def health() -> dict[str, Any]:
    """Per-service status. Each store is checked by using it, not by assuming it.

    Reported per store rather than as one flag: ClickHouse being down costs the serving
    panels and costs episodes nothing, and a single "healthy: false" would hide which half
    of the split is actually broken.
    """
    status: dict[str, Any] = {"uptime_s": round(time.time() - _STARTED, 1)}
    try:
        with _store() as store:
            status["postgres"] = "up"
            status["episodes"] = store.episode_count()
    except Exception as exc:  # noqa: BLE001 - health must report failure, not raise it
        status["postgres"] = "down"
        status["postgres_error"] = str(exc)
    try:
        with _warehouse() as warehouse:
            status["clickhouse"] = "up"
            # Rows stored, not distinct readings, and named so it cannot be read as the
            # latter: this counts pre-merge duplicates from any replay, so it is routinely
            # larger than /metrics' figure. Getting the distinct count here would mean
            # paying for FINAL on every health poll.
            status["reading_rows"] = warehouse.reading_count()
    except Exception as exc:  # noqa: BLE001 - same rule for the serving store
        status["clickhouse"] = "down"
        status["clickhouse_error"] = str(exc)
    return status


@app.get("/episodes")
def episodes(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    with _store() as store:
        rows = store.recent_episodes(limit=limit)
    return {
        "count": len(rows),
        "episodes": [
            {
                "id": r["id"],
                "channel": r["channel"],
                "t_start_ms": r["t_start_ms"],
                "t_end_ms": r["t_end_ms"],
                "duration_s": round((r["t_end_ms"] - r["t_start_ms"]) / 1000, 1),
                "raised_by": r["raised_by"],
                "peak_score": round(float(r["peak_score"]), 2),
                "window_count": r["window_count"],
                "status": r["status"],
                "attributed_to": r["attributed_to"],
                # Ground truth from the synthetic source. Shown so a viewer can see what
                # the detector was actually looking at; it is never an input to detection.
                "injected_origins": r["injected_origins"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


@app.get("/episodes/{episode_id}")
def episode(episode_id: int) -> dict[str, Any]:
    with _store() as store, store._conn.cursor() as cur:
        cur.execute("SELECT * FROM episodes WHERE id = %s", (episode_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"no episode {episode_id}")
        cur.execute(
            "SELECT detector, window_start_ms, window_end_ms, score, latency_ms"
            " FROM episode_scores WHERE episode_id = %s ORDER BY window_start_ms",
            (episode_id,),
        )
        scores = cur.fetchall()
    row["created_at"] = row["created_at"].isoformat()
    row["scores"] = [{**s, "score": round(float(s["score"]), 3)} for s in scores]
    return row


@app.get("/detectors")
def detectors() -> dict[str, Any]:
    """Per-detector episode counts and hot-path latency, side by side.

    Scores are on each detector's own scale and are not comparable across rows; the
    matched-alarm-budget comparison lives in the benchmark (docs/EVALUATION.md).
    """
    with _store() as store:
        with store._conn.cursor() as cur:
            cur.execute(
                """
                SELECT raised_by,
                       count(*) AS episodes,
                       round(avg(peak_score)::numeric, 2) AS avg_peak,
                       round(max(peak_score)::numeric, 2) AS max_peak,
                       sum(window_count) AS windows
                FROM episodes GROUP BY raised_by ORDER BY raised_by
                """
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            latency = store.detector_latency_percentiles(r["raised_by"])
            out.append(
                {
                    "detector": r["raised_by"],
                    "episodes": r["episodes"],
                    "windows": r["windows"],
                    "avg_peak_score": float(r["avg_peak"]),
                    "max_peak_score": float(r["max_peak"]),
                    "latency_ms": (
                        {
                            "n": latency["n"],
                            "p50": round(float(latency["p50"]), 3),
                            "p95": round(float(latency["p95"]), 3),
                            "p99": round(float(latency["p99"]), 3),
                            "max": round(float(latency["max"]), 3),
                        }
                        if latency
                        else None
                    ),
                }
            )
    return {"detectors": out, "hot_path_budget_ms_p99": 250}


@app.get("/reconciliation")
def reconciliation(windows: int = 60) -> dict[str, Any]:
    """The evidence behind any zero-drift claim, not a green light.

    Returns the most recent reconciliation run *and* the health windows behind it, because a
    single aggregate figure is exactly the thing a reader should not have to trust. Drift is
    reported next to the independent broker-offset audit: the first is what our own ledger
    says, the second is what the broker says, and agreement between two counts derived
    differently is the only reason to believe either.

    Absent data is reported as absent. A dashboard that renders zeros when the harness has
    never run would claim a clean pipeline on no evidence at all.
    """
    with _store() as store, store._conn.cursor() as cur:
        cur.execute(
            "SELECT topic, duration_s, readings, channels, drift, missing, duplicates,"
            " regressions, broker_available, broker_consumed, offset_drift, created_at"
            " FROM reconciliation_runs ORDER BY created_at DESC LIMIT 1"
        )
        latest = cur.fetchone()
        cur.execute(
            "SELECT window_start_ms, window_end_ms, channels, readings, missing, duplicates,"
            " regressions, max_lag_ms, severity FROM pipeline_health"
            " ORDER BY window_start_ms DESC LIMIT %s",
            (max(1, min(windows, 500)),),
        )
        recent = cur.fetchall()
        cur.execute("SELECT severity, count(*) AS windows FROM pipeline_health GROUP BY severity")
        by_severity = {r["severity"]: r["windows"] for r in cur.fetchall()}

    return {
        "latest_run": latest,
        "windows": recent,
        "windows_by_severity": by_severity,
        "disturbed_windows": sum(n for s, n in by_severity.items() if s != "ok"),
        "has_evidence": latest is not None,
    }


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    """Per-channel telemetry rollups, served from ClickHouse.

    This is the query the storage split exists for: a per-channel summary over every reading
    ever ingested, answered from the per-minute aggregate rather than by scanning the raw
    table. `readings` counts distinct sequence numbers, so a replayed batch does not inflate
    it -- the count is right before the underlying parts have merged, not only after.
    """
    with _warehouse() as warehouse:
        channels = warehouse.channels()
    for row in channels:
        for key in ("first_minute", "last_minute"):
            if row.get(key) is not None:
                row[key] = row[key].isoformat()
        # The identity invariant the reconciliation harness checks, computed here so the
        # panel shows whether the lake and the serving store agree with the ledger.
        row["seq_span"] = row["seq_max"] - row["seq_min"] + 1
        row["gap"] = row["seq_span"] - row["readings"]
    return {
        "channels": channels,
        "readings": sum(r["readings"] for r in channels),
        "channel_count": len(channels),
    }


@app.get("/metrics/{channel}")
def channel_series(channel: str, minutes: int = 60) -> dict[str, Any]:
    """Per-minute series for one channel: the dashboard's time-series panel."""
    with _warehouse() as warehouse:
        points = warehouse.series(channel, minutes=max(1, min(minutes, 1440)))
    if not points:
        raise HTTPException(status_code=404, detail=f"no readings for channel {channel}")
    return {
        "channel": channel,
        "points": [{**p, "minute": p["minute"].isoformat()} for p in points],
        "count": len(points),
    }


@app.get("/scores")
def window_scores() -> dict[str, Any]:
    """Per-detector window-score distribution from ClickHouse.

    Distinct from `/detectors`, which reports episodes and hot-path latency out of Postgres.
    This one is over every scored window, including the overwhelming majority that raised
    nothing -- the population an episode table by definition does not contain.

    Latency is deliberately absent: the only producer on the scores topic is the Flink job,
    which reports none, and the hot-path percentiles come from `/detectors` instead.
    """
    with _warehouse() as warehouse:
        return {"detectors": warehouse.detector_scores()}


@app.get("/context")
def context_events(limit: int = 100) -> dict[str, Any]:
    with _store() as store, store._conn.cursor() as cur:
        cur.execute(
            "SELECT event_id, kind, t_start_ms, t_end_ms, severity, detail, scope,"
            " perturbed_telemetry FROM context_events"
            " ORDER BY t_start_ms DESC LIMIT %s",
            (max(1, min(limit, 500)),),
        )
        rows = cur.fetchall()
    return {"count": len(rows), "events": rows}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML
