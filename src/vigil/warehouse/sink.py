"""Kafka to ClickHouse: the serving store's writer.

Its own consumer group, for the same reason the reconciliation harness has its own: a sink
that shared the detector's offsets could only ever serve what the detector had already
managed to read, and the dashboard would then be reporting on the detector rather than on
the stream.

**Offsets commit after the batch is in ClickHouse**, so a crash replays rather than loses.
That makes this at-least-once on the wire, and the replay is absorbed by the
`ReplacingMergeTree` identity key rather than by hoping it does not happen -- the duplicate
rows exist until a merge collapses them, and every aggregate that has to be exact before
then is written to be replay-proof (`uniqExact` over seq, not `count()`). The measured
behaviour is in `docs/EVALUATION.md` section 8.

Batching is by size or by age, whichever comes first. ClickHouse wants large inserts -- a
part per row would leave it merging forever -- but a dashboard that only updates when
5,000 readings have accumulated is broken on a quiet channel, so the age bound is what makes
the store usable rather than merely efficient.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from vigil.detectors.window_scores import parse_window_score
from vigil.readings import Reading
from vigil.warehouse.clickhouse import ReadingsWarehouse

log = logging.getLogger("vigil.warehouse.sink")


@dataclass
class SinkCounters:
    readings_written: int = 0
    scores_written: int = 0
    batches: int = 0
    undecodable: int = 0
    write_failures: int = 0
    total_write_ms: float = 0.0

    def line(self) -> str:
        mean = self.total_write_ms / self.batches if self.batches else 0.0
        return (
            f"clickhouse sink: {self.readings_written:,} readings | "
            f"{self.scores_written:,} scores | {self.batches:,} batches | "
            f"mean write {mean:.0f} ms | {self.undecodable:,} undecodable | "
            f"{self.write_failures:,} write failures"
        )


@dataclass
class ClickHouseSink:
    """Accumulates decoded records and flushes them in batches.

    Deliberately not a consumer: it is handed records, so the batching and the flush
    behaviour are testable without a broker. `warehouse.py` owns the Kafka loop.
    """

    warehouse: ReadingsWarehouse
    batch_size: int = 5_000
    max_batch_age_s: float = 5.0

    counters: SinkCounters = field(default_factory=SinkCounters)
    _readings: list[Reading] = field(default_factory=list, init=False)
    _scores: list[dict] = field(default_factory=list, init=False)
    _oldest_ms: float = field(default=0.0, init=False)

    @property
    def pending(self) -> int:
        return len(self._readings) + len(self._scores)

    def add_reading(self, raw: bytes | str) -> bool:
        reading = _decode_reading(raw)
        if reading is None:
            self.counters.undecodable += 1
            return False
        self._note_arrival()
        self._readings.append(reading)
        return True

    def add_score(self, raw: bytes | str) -> bool:
        score = parse_window_score(raw)
        if score is None:
            self.counters.undecodable += 1
            return False
        self._note_arrival()
        self._scores.append(
            {
                "channel": score.channel,
                "detector": score.detector,
                "window_start_ms": score.window_start_ms,
                "window_end_ms": score.window_end_ms,
                "score": score.score,
                # Always None from this topic. `parse_window_score` hardcodes 0.0 because
                # Flink reports no latency, and passing that through would put a fabricated
                # zero where "not measured" belongs.
                "latency_ms": None,
            }
        )
        return True

    def _note_arrival(self) -> None:
        if self.pending == 0:
            self._oldest_ms = time.monotonic()

    def should_flush(self) -> bool:
        if self.pending == 0:
            return False
        if self.pending >= self.batch_size:
            return True
        return (time.monotonic() - self._oldest_ms) >= self.max_batch_age_s

    def flush(self) -> int:
        """Write what is pending. Returns rows written; 0 when there was nothing.

        Raises on a write failure rather than swallowing it. The caller must not commit
        offsets for a batch that did not land, and the only way to guarantee that is to let
        the failure reach the loop that owns the commit.
        """
        if self.pending == 0:
            return 0
        started = time.perf_counter()
        try:
            written = self.warehouse.insert_readings(self._readings)
            written += self.warehouse.insert_window_scores(self._scores)
        except Exception:
            self.counters.write_failures += 1
            raise
        self.counters.total_write_ms += (time.perf_counter() - started) * 1000.0
        self.counters.batches += 1
        self.counters.readings_written += len(self._readings)
        self.counters.scores_written += len(self._scores)
        self._readings.clear()
        self._scores.clear()
        return written


def _decode_reading(raw: bytes | str) -> Reading | None:
    """Decode one reading. None for anything unusable.

    One malformed record must not stop the stream; the caller counts them so a producer
    regression shows up as a number rather than as silence.
    """
    try:
        return Reading.from_json(raw)
    except Exception:  # noqa: BLE001 - a bad record is counted, not raised
        return None
