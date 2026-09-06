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
from vigil.settings import PostgresSettings
from vigil.store import EpisodeStore

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


@app.get("/health")
def health() -> dict[str, Any]:
    """Per-service status. Postgres is checked by using it, not by assuming it."""
    status: dict[str, Any] = {"uptime_s": round(time.time() - _STARTED, 1)}
    try:
        with _store() as store:
            status["postgres"] = "up"
            status["episodes"] = store.episode_count()
    except Exception as exc:  # noqa: BLE001 - health must report failure, not raise it
        status["postgres"] = "down"
        status["error"] = str(exc)
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
