"""Persisting episodes to Postgres.

Postgres holds detected episodes and application state, never raw readings (ADR-006).

Writes are idempotent on (channel, t_start_ms, raised_by). A consumer restarted from its
last committed offset re-derives the same episodes from the same windows; without an
upsert the replay would duplicate them, and a reconciliation harness that then reported
drift would be reporting our own bookkeeping rather than a pipeline fault. The interior
exactly-once story has to hold at the sink too, or it is only a story about the middle.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from vigil.context import ContextEvent
from vigil.episodes import Episode
from vigil.settings import PostgresSettings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class EpisodeStore:
    """A connection to the episode database, plus the few statements the platform needs."""

    def __init__(self, settings: PostgresSettings, *, autocommit: bool = True) -> None:
        self.settings = settings
        self._conn = psycopg.connect(settings.dsn, autocommit=autocommit, row_factory=dict_row)

    def __enter__(self) -> EpisodeStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if not self._conn.closed:
            self._conn.close()

    def apply_schema(self) -> None:
        """Create the schema if it is not there. Idempotent, run on every startup."""
        self._conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    def record_episode(self, episode: Episode) -> int:
        """Insert or update an episode and its per-detector scores. Returns its id.

        On conflict the episode is updated rather than skipped, because a replay can carry
        *more* information than the first pass: the off-critical-path model may have
        scored windows the hot path had already raised on (ADR-017).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO episodes (
                    channel, t_start_ms, t_end_ms, raised_by, peak_score, mean_score,
                    window_count, threshold, status, attributed_to, explanation,
                    injected_origins, onset_ms, verdict
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (channel, t_start_ms, raised_by) DO UPDATE SET
                    t_end_ms      = GREATEST(episodes.t_end_ms, EXCLUDED.t_end_ms),
                    peak_score    = GREATEST(episodes.peak_score, EXCLUDED.peak_score),
                    mean_score    = EXCLUDED.mean_score,
                    window_count  = GREATEST(episodes.window_count, EXCLUDED.window_count),
                    status        = EXCLUDED.status,
                    attributed_to = COALESCE(EXCLUDED.attributed_to, episodes.attributed_to),
                    verdict       = COALESCE(EXCLUDED.verdict, episodes.verdict),
                    explanation   = COALESCE(EXCLUDED.explanation, episodes.explanation),
                    onset_ms      = COALESCE(episodes.onset_ms, EXCLUDED.onset_ms)
                RETURNING id
                """,
                (
                    episode.channel,
                    episode.t_start_ms,
                    episode.t_end_ms,
                    episode.raised_by,
                    episode.peak_score,
                    episode.mean_score,
                    episode.window_count,
                    episode.threshold,
                    str(episode.status),
                    episode.attributed_to,
                    episode.explanation,
                    list(episode.injected_origins),
                    episode.onset_ms,
                    episode.verdict,
                ),
            )
            episode_id = cur.fetchone()["id"]

            if episode.scores:
                cur.executemany(
                    """
                    INSERT INTO episode_scores (
                        episode_id, detector, window_start_ms, window_end_ms, score, latency_ms
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (episode_id, detector, window_start_ms) DO UPDATE SET
                        score = EXCLUDED.score,
                        latency_ms = EXCLUDED.latency_ms
                    """,
                    [
                        (
                            episode_id,
                            s.detector,
                            s.window_start_ms,
                            s.window_end_ms,
                            s.score,
                            s.latency_ms,
                        )
                        for s in episode.scores
                    ],
                )
        return episode_id

    def record_context_event(self, event: ContextEvent) -> None:
        """Persist a context marker. Idempotent on the producer-assigned event id."""
        self._conn.execute(
            """
            INSERT INTO context_events (
                event_id, kind, t_start_ms, t_end_ms, severity, detail, scope,
                perturbed_telemetry
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event.event_id,
                str(event.kind),
                event.t_start_ms,
                event.t_end_ms,
                str(event.severity),
                event.detail,
                list(event.scope),
                event.perturbed_telemetry,
            ),
        )

    def attach_explanation(self, episode_id: int, text: str) -> bool:
        """Attach an explanation to an episode already written. Returns whether it landed.

        Separate from `record_episode` because the explanation arrives later, from another
        thread, and long after the episode was durable -- and because an episode that never
        gets one must remain a complete episode rather than an incomplete write.
        """
        with self._conn.cursor() as cur:
            cur.execute("UPDATE episodes SET explanation = %s WHERE id = %s", (text, episode_id))
            return cur.rowcount == 1

    def recent_episodes(self, limit: int = 50) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, channel, t_start_ms, t_end_ms, raised_by, peak_score, mean_score,
                       window_count, status, attributed_to, injected_origins, created_at
                FROM episodes
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()

    def episode_count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM episodes")
            return cur.fetchone()["n"]

    def detector_latency_percentiles(self, detector: str) -> dict[str, float] | None:
        """p50/p95/p99 of per-window scoring latency, for the NFR-1 report."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS n,
                       percentile_disc(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                       percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                       percentile_disc(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99,
                       max(latency_ms) AS max
                FROM episode_scores
                WHERE detector = %s
                """,
                (detector,),
            )
            row = cur.fetchone()
            return None if not row or row["n"] == 0 else row
