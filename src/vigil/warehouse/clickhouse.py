"""ClickHouse: the serving store for readings and window scores.

**What lives here and what does not.** ADR-006 splits storage three ways and this is the
serving third: high-volume, append-mostly, read by aggregate. Episodes do not live here.
They live in Postgres, which stays their transactional source of truth, and there is no
mirror of them here -- a second copy of a table that is small enough not to need one buys
a consistency problem and no query speed (ADR-045).

**Dedupe is structural, not hopeful.** The readings topic is consumed at least once, so a
replay re-delivers records this sink has already written. Both tables are
`ReplacingMergeTree` keyed on identity -- `(channel, seq, event_ts)` for readings (ADR-046:
the producer's sequence restarts with the producer, so the pair alone is not unique across
its lifetimes), `(channel, detector, window_start_ms)` for scores -- so a replayed batch
collapses instead of inflating every count derived from it. That dedupe is
*eventual*: it happens when parts merge. Anything that must be exact before a merge has
happened reads the `readings_exact` view, which pays for `FINAL` explicitly rather than
getting correctness by luck.

The connection is lazy and the schema is applied on first use, so importing this module
costs nothing and a detector that never writes never opens a socket.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vigil.readings import Reading
from vigil.settings import ClickHouseSettings

log = logging.getLogger("vigil.warehouse")


def _utc(epoch_ms: int) -> datetime:
    """Epoch milliseconds to an aware datetime.

    A tz-aware `DateTime64(3, 'UTC')` column rejects a bare float, and a naive datetime
    would be read as local time -- which on this machine is not UTC, so every event time
    would land hours from where it belongs without anything raising.
    """
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _strip_comments(sql: str) -> str:
    """Remove `--` line comments, respecting single-quoted literals.

    Necessary before splitting on semicolons, not merely tidy: a semicolon inside a prose
    comment is indistinguishable from a statement terminator to a naive split, and the
    resulting half-statement fails with a syntax error pointing at a line that is correct.
    Quote-awareness is what keeps a literal like 'a; b' from being mistaken for a comment.
    """
    out = []
    for line in sql.splitlines():
        in_quote = False
        cut = len(line)
        i = 0
        while i < len(line):
            char = line[i]
            if char == "'":
                in_quote = not in_quote
            elif not in_quote and char == "-" and line[i : i + 2] == "--":
                cut = i
                break
            i += 1
        out.append(line[:cut].rstrip())
    return "\n".join(out)


def _statements(sql: str) -> list[str]:
    """Split the schema file into executable statements.

    ClickHouse's HTTP interface takes one statement per request, unlike psycopg which will
    happily run a whole file.
    """
    return [chunk.strip() for chunk in _strip_comments(sql).split(";") if chunk.strip()]


@dataclass
class ReadingsWarehouse:
    """A ClickHouse connection that knows this platform's two serving tables."""

    settings: ClickHouseSettings
    _client: Any = None

    def __enter__(self) -> ReadingsWarehouse:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> Any:
        if self._client is None:
            import clickhouse_connect

            # Connect without naming the database first: on a volume that was initialised
            # before CLICKHOUSE_DB was set, the image's entrypoint has already run and will
            # not create it, so the bootstrap has to be able to.
            admin = clickhouse_connect.get_client(
                host=self.settings.host,
                port=self.settings.http_port,
                username=self.settings.user,
                password=self.settings.password,
                connect_timeout=self.settings.connect_timeout_s,
            )
            admin.command(f"CREATE DATABASE IF NOT EXISTS {self.settings.database}")
            admin.close()

            self._client = clickhouse_connect.get_client(
                host=self.settings.host,
                port=self.settings.http_port,
                username=self.settings.user,
                password=self.settings.password,
                database=self.settings.database,
                connect_timeout=self.settings.connect_timeout_s,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def apply_schema(self) -> int:
        """Idempotent. Returns the number of statements run."""
        client = self.connect()
        statements = _statements(SCHEMA_PATH.read_text(encoding="utf-8"))
        for statement in statements:
            client.command(statement)
        return len(statements)

    # ---------------------------------------------------------------- writes

    def insert_readings(self, readings: Sequence[Reading]) -> int:
        """Append a batch. Returns the row count handed to ClickHouse, not a dedupe count.

        The number returned is what was *written*, which after a replay is deliberately more
        than the number of distinct readings. Reporting the post-merge figure here would
        mean reporting a number this process cannot know yet.
        """
        if not readings:
            return 0
        rows = [
            [
                r.channel,
                r.seq,
                _utc(r.event_ts_ms),
                r.value,
                r.injected or "",
                r.origin or "",
            ]
            for r in readings
        ]
        self.connect().insert(
            "readings",
            rows,
            column_names=["channel", "seq", "event_ts", "value", "injected", "origin"],
        )
        return len(rows)

    def insert_window_scores(self, scores: Sequence[dict[str, Any]]) -> int:
        """Append per-window detector scores.

        `latency_ms` stays None when the producer did not report one -- the Flink job does
        not -- rather than becoming 0.0, which would corrupt the NFR-1 percentiles.
        """
        if not scores:
            return 0
        rows = [
            [
                s["channel"],
                s["detector"],
                int(s["window_start_ms"]),
                int(s["window_end_ms"]),
                _utc(int(s["window_start_ms"])),
                float(s["score"]),
                None if s.get("latency_ms") is None else float(s["latency_ms"]),
            ]
            for s in scores
        ]
        self.connect().insert(
            "window_scores",
            rows,
            column_names=[
                "channel",
                "detector",
                "window_start_ms",
                "window_end_ms",
                "window_start",
                "score",
                "latency_ms",
            ],
        )
        return len(rows)

    # ---------------------------------------------------------------- reads

    def reading_count(self, *, exact: bool = False) -> int:
        """Rows held. `exact=True` pays for FINAL and counts distinct (channel, seq)."""
        table = "readings_exact" if exact else "readings"
        return int(self.connect().query(f"SELECT count() FROM {table}").result_rows[0][0])

    def channels(self) -> list[dict[str, Any]]:
        """Per-channel summary, served from the rollup rather than the raw table."""
        rows = (
            self.connect()
            .query(
                """
            SELECT channel,
                   uniqExactMerge(readings) AS readings,
                   minMerge(seq_min)     AS seq_min,
                   maxMerge(seq_max)     AS seq_max,
                   round(avgMerge(value_avg), 4) AS value_avg,
                   minMerge(value_min)   AS value_min,
                   maxMerge(value_max)   AS value_max,
                   min(minute)           AS first_minute,
                   max(minute)           AS last_minute
            FROM readings_per_minute
            GROUP BY channel
            ORDER BY channel
            """
            )
            .named_results()
        )
        return [dict(r) for r in rows]

    def series(self, channel: str, *, minutes: int = 60) -> list[dict[str, Any]]:
        """Per-minute rollup for one channel: the dashboard's time-series panel.

        Reads pre-aggregated rows, which is the reason this store exists. A chart over an
        hour of a 1 Hz channel is 60 rows here against 3,600 raw ones, and the ratio is what
        grows.
        """
        rows = (
            self.connect()
            .query(
                """
            SELECT minute,
                   uniqExactMerge(readings)      AS readings,
                   round(avgMerge(value_avg), 4) AS value_avg,
                   minMerge(value_min)           AS value_min,
                   maxMerge(value_max)           AS value_max
            FROM readings_per_minute
            WHERE channel = {channel:String}
            GROUP BY minute
            ORDER BY minute DESC
            LIMIT {limit:UInt32}
            """,
                parameters={"channel": channel, "limit": max(1, min(minutes, 10_000))},
            )
            .named_results()
        )
        return [dict(r) for r in reversed(list(rows))]

    def recent_points(self, *, seconds: int = 120, channels: int = 6) -> dict[str, Any]:
        """Per-second values per channel over a trailing window: the live chart's feed.

        Downsampled to one point per channel per second, which is the resolution a chart can
        actually show. The raw alternative is arithmetic: 400 readings/s across 6 channels
        over a 120-second window is 288,000 rows to move and 288,000 points to draw, for a
        line roughly 900 pixels wide. Averaging per second gives 120 points per channel and
        loses nothing a viewer could have seen.

        The window is anchored on the newest event time in the table rather than on wall
        clock. A replayed or paused stream would otherwise scroll away into an empty chart
        while the data sat there, which looks like a broken dashboard rather than a stopped
        producer -- and `last_event_ms` is returned so the page can say which it is.
        """
        client = self.connect()
        newest = client.query(
            "SELECT toUnixTimestamp64Milli(max(event_ts)) AS t FROM readings"
        ).result_rows
        last_event_ms = int(newest[0][0]) if newest and newest[0][0] else 0
        if not last_event_ms:
            return {"channels": [], "last_event_ms": 0, "from_ms": 0, "to_ms": 0, "readings": 0}

        span = max(10, min(seconds, 3600))
        from_ms = last_event_ms - span * 1000

        rows = client.query(
            """
            SELECT channel,
                   toUnixTimestamp64Milli(toDateTime64(toStartOfSecond(event_ts), 3)) AS t_ms,
                   round(avg(value), 4) AS value,
                   uniqExact(seq, event_ts) AS readings
            FROM readings
            WHERE event_ts >= fromUnixTimestamp64Milli({from_ms:Int64})
            GROUP BY channel, t_ms
            ORDER BY channel, t_ms
            """,
            parameters={"from_ms": from_ms},
        ).named_results()

        by_channel: dict[str, list[dict[str, Any]]] = {}
        total = 0
        for row in rows:
            by_channel.setdefault(row["channel"], []).append(
                {"t_ms": int(row["t_ms"]), "value": float(row["value"])}
            )
            total += int(row["readings"])

        # Busiest channels first, so a fleet with more channels than the chart can show
        # drops the quiet ones rather than an arbitrary alphabetical tail.
        ordered = sorted(by_channel.items(), key=lambda kv: -len(kv[1]))[: max(1, channels)]
        return {
            "channels": [{"channel": name, "points": points} for name, points in ordered],
            "last_event_ms": last_event_ms,
            "from_ms": from_ms,
            "to_ms": last_event_ms,
            "readings": total,
        }

    def detector_scores(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Per-detector score distribution, the aggregate Postgres would scan for."""
        rows = (
            self.connect()
            .query(
                """
            SELECT detector,
                   count()                         AS windows,
                   uniqExact(channel)              AS channels,
                   round(avg(score), 4)            AS score_avg,
                   round(max(score), 4)            AS score_max,
                   round(quantile(0.95)(score), 4) AS score_p95,
                   round(quantile(0.99)(score), 4) AS score_p99
            FROM window_scores
            GROUP BY detector
            ORDER BY detector
            LIMIT {limit:UInt32}
            """,
                parameters={"limit": limit},
            )
            .named_results()
        )
        return [dict(r) for r in rows]

    def ping(self) -> bool:
        try:
            self.connect().query("SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - health reports failure rather than raising it
            return False
