"""The reconciliation harness: proves the invariants, and emits the signal.

Runs as its own consumer group, independent of the detector. Independent on purpose -- if
it shared the detector's offsets it could only ever report on what the detector had already
managed to read, which is precisely the thing under audit. A separate group means the
harness sees the log as the broker holds it.

Two outputs, one pass over the stream:

  1. **The proof.** Per-channel identity invariants over the dense sequence, plus a
     comparison against the broker's own log-end offsets. Reported as drift.
  2. **The signal.** A per-window `PipelineHealth` record on the context topic, which the
     conditioning policy consumes in Phase 3. Emitted for clean windows too, because a
     consumer has to be able to tell "clean" from "no signal" -- conditioning fails open on
     a missing signal (ADR-007), and those two cases must not look alike.

The second output is why this is built before Flink: it is load-bearing for the core
contribution, not merely for the correctness claim.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from confluent_kafka import Consumer, KafkaError, TopicPartition
from confluent_kafka.admin import AdminClient

from vigil.context import ContextEvent, ContextKind, Severity
from vigil.ingest.publisher import ReadingPublisher
from vigil.readings import Reading
from vigil.reconciliation.ledger import (
    HealthSeverity,
    HealthThresholds,
    PipelineHealth,
    ReconciliationLedger,
)

log = logging.getLogger("vigil.reconciliation")

_SEVERITY_TO_CONTEXT = {
    HealthSeverity.INFO: Severity.INFO,
    HealthSeverity.WARNING: Severity.WARNING,
    HealthSeverity.CRITICAL: Severity.CRITICAL,
}


@dataclass(frozen=True)
class OffsetAudit:
    """What the broker says the log holds, against what the harness actually read."""

    topic: str
    low_watermark: int
    log_end: int
    consumed: int

    @property
    def available(self) -> int:
        """Records currently retained. Anything below the low watermark has aged out."""
        return self.log_end - self.low_watermark

    @property
    def drift(self) -> int:
        return self.available - self.consumed

    def summary(self) -> str:
        return (
            f"broker log {self.topic}: low {self.low_watermark:,} high {self.log_end:,} "
            f"-> {self.available:,} retained | consumed {self.consumed:,} | "
            f"offset drift {self.drift:+,}"
        )


def audit_offsets(consumer: Consumer, bootstrap: str, topic: str, consumed: int) -> OffsetAudit:
    """Ask the broker what the log holds, independently of what we read.

    This is the second, independent check. The sequence invariant proves the stream was
    internally consistent; this proves we saw all of it. A harness that only checked its own
    consumption could read half the log flawlessly and declare success.
    """
    admin = AdminClient({"bootstrap.servers": bootstrap})
    metadata = admin.list_topics(topic=topic, timeout=10)
    partitions = list(metadata.topics[topic].partitions)
    low_total = 0
    high_total = 0
    for partition in partitions:
        low, high = consumer.get_watermark_offsets(
            TopicPartition(topic, partition), timeout=10, cached=False
        )
        low_total += low
        high_total += high
    return OffsetAudit(
        topic=topic, low_watermark=low_total, log_end=high_total, consumed=consumed
    )


class ReconciliationHarness:
    """Drives the ledger over the readings topic and publishes the health signal."""

    def __init__(
        self,
        publisher: ReadingPublisher | None,
        context_topic: str,
        *,
        window_ms: int = 30_000,
        grace_windows: int = 1,
        thresholds: HealthThresholds | None = None,
        emit_clean: bool = True,
    ) -> None:
        self.ledger = ReconciliationLedger(
            window_ms=window_ms, thresholds=thresholds or HealthThresholds()
        )
        self.publisher = publisher
        self.context_topic = context_topic
        self.grace_windows = grace_windows
        self.emit_clean = emit_clean
        self.health_emitted = 0
        self.disturbed_windows = 0
        self.malformed = 0
        self.recent: list[PipelineHealth] = []

    def observe(
        self, reading: Reading, ingest_wall_ms: int, source: str | None = None
    ) -> list[PipelineHealth]:
        self.ledger.observe(reading, ingest_wall_ms=ingest_wall_ms, source=source)
        due = self.ledger.close_due(grace_windows=self.grace_windows)
        for health in due:
            self._emit(health)
        return due

    def drain(self) -> list[PipelineHealth]:
        due = self.ledger.close_all()
        for health in due:
            self._emit(health)
        return due

    def _emit(self, health: PipelineHealth) -> None:
        self.health_emitted += 1
        if health.disturbed:
            self.disturbed_windows += 1
            log.warning("pipeline disturbance %s", health.summary())
        self.recent.append(health)
        if len(self.recent) > 200:
            del self.recent[:-200]

        if self.publisher is None:
            return
        if not health.disturbed and not self.emit_clean:
            return
        self.publisher.publish_context(self.context_topic, self.to_context_event(health))

    @staticmethod
    def to_context_event(health: PipelineHealth) -> ContextEvent:
        """Put a health record on the same wire as a deploy marker.

        Same schema, same topic, same consumer interface -- which is the whole point of
        ADR-003: a new context signal is an implementation of one contract, not a new path
        through the system. Scope is empty because a pipeline disturbance is fleet-wide by
        nature; a deploy is not, and that difference is exactly what scope encodes.
        """
        return ContextEvent(
            event_id=f"pipeline-{health.window_start_ms}",
            kind=ContextKind.PIPELINE,
            t_start_ms=health.window_start_ms,
            t_end_ms=health.window_end_ms,
            severity=_SEVERITY_TO_CONTEXT.get(health.severity, Severity.INFO),
            detail=(
                f"readings={health.readings} channels={health.channels} "
                f"missing={health.missing} duplicates={health.duplicates} "
                f"reordered={health.regressions} max_lag_ms={health.max_lag_ms} "
                f"severity={health.severity}"
            ),
            scope=(),
        )


def build_consumer(bootstrap: str, group: str, from_beginning: bool) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group,
            "auto.offset.reset": "earliest" if from_beginning else "latest",
            "enable.auto.commit": False,
            "max.poll.interval.ms": 600_000,
        }
    )


def consume_forever(
    consumer: Consumer,
    harness: ReconciliationHarness,
    *,
    topic: str,
    duration_s: float,
    stop_after_idle_s: float,
    report_interval_s: float,
    should_stop,
) -> None:
    """Poll loop. Extracted so the soak runner and the chaos suite can drive it too."""

    def on_assign(_consumer, partitions):
        # Register every assigned partition immediately, before any of them delivers. A
        # partition Kafka has not got round to serving must hold the watermark back rather
        # than be invisible to it.
        now = time.time()
        for tp in partitions:
            harness.ledger.register_source(f"p{tp.partition}", now)
        log.info("assigned %d partitions", len(partitions))

    def on_revoke(_consumer, partitions):
        for tp in partitions:
            harness.ledger.forget_source(f"p{tp.partition}")

    consumer.subscribe([topic], on_assign=on_assign, on_revoke=on_revoke)
    started = time.perf_counter()
    deadline = started + duration_s if duration_s > 0 else float("inf")
    last_report = started
    idle_s = 0.0

    while not should_stop() and time.perf_counter() < deadline:
        message = consumer.poll(1.0)
        if message is None:
            idle_s += 1.0
            if stop_after_idle_s and idle_s >= stop_after_idle_s:
                log.info("no messages for %.0fs; stopping", stop_after_idle_s)
                break
            continue
        if message.error():
            if message.error().code() != KafkaError._PARTITION_EOF:
                log.error("consume error: %s", message.error())
            continue

        idle_s = 0.0
        try:
            reading = Reading.from_json(message.value())
        except (ValueError, KeyError, TypeError):
            harness.malformed += 1
            continue

        # The partition is the watermark source: windows close on the minimum event time
        # across partitions, so one racing ahead cannot strand the others' readings.
        harness.observe(
            reading,
            ingest_wall_ms=int(time.time() * 1000),
            source=f"p{message.partition()}",
        )

        now = time.perf_counter()
        if now - last_report >= report_interval_s:
            led = harness.ledger
            print(
                f"[{now - started:7.1f}s] readings {led.total_readings:,} | "
                f"channels {len(led.channels)} | drift {led.total_drift:+,} | "
                f"missing {led.total_missing:,} | dupes {led.total_duplicates:,} | "
                f"reordered {led.total_regressions:,} | "
                f"health windows {harness.health_emitted:,} "
                f"({harness.disturbed_windows:,} disturbed)",
                flush=True,
            )
            last_report = now
