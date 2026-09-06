"""The one path from a Reading to Kafka.

Both sources publish through this, so the live feed and the synthetic harness cannot drift
in producer configuration -- which matters because the producer's idempotence setting is
what the no-duplicates half of the correctness claim rests on.
"""

from __future__ import annotations

import csv
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from confluent_kafka import KafkaException, Producer

from vigil.readings import Reading


@dataclass
class PublishCounters:
    produced: int = 0
    delivered: int = 0
    failed: int = 0
    backpressure_waits: int = 0
    first_error: str | None = None

    def on_delivery(self, err, _msg) -> None:
        if err is None:
            self.delivered += 1
            return
        self.failed += 1
        if self.first_error is None:
            self.first_error = str(err)


class ReadingPublisher:
    """Publishes readings to the readings topic, keyed by channel.

    Keying by channel puts every reading for a channel in one partition, which is what
    preserves per-channel sequence order and lets the reconciliation harness check
    identity invariants partition-locally instead of having to globally sort a stream.
    """

    def __init__(
        self,
        bootstrap: str,
        topic: str,
        *,
        linger_ms: int = 20,
        batch_size: int = 1 << 20,
        counters: PublishCounters | None = None,
    ) -> None:
        self.topic = topic
        self.counters = counters or PublishCounters()
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap,
                # Idempotence is what lets the reconciliation harness attribute any
                # duplicate it finds to a real defect rather than to a producer retry. It
                # implies acks=all and bounded in-flight requests; that throughput cost is
                # the price of an honest no-duplicates claim.
                "enable.idempotence": True,
                "compression.type": "lz4",
                "linger.ms": linger_ms,
                "batch.size": batch_size,
                "queue.buffering.max.messages": 500_000,
                "queue.buffering.max.kbytes": 512_000,
            }
        )

    def publish(self, reading: Reading) -> None:
        while True:
            try:
                self._producer.produce(
                    self.topic,
                    key=reading.channel,
                    value=reading.to_json(),
                    # Kafka's message timestamp carries event time, so a consumer gets it
                    # without parsing the payload.
                    timestamp=reading.event_ts_ms,
                    on_delivery=self.counters.on_delivery,
                )
                self.counters.produced += 1
                return
            except BufferError:
                # The local queue is full: the broker or the network is the limit. Count it
                # and wait, so overload is reported as backpressure rather than as
                # mysteriously low throughput.
                self.counters.backpressure_waits += 1
                self._producer.poll(0.05)

    def poll(self, timeout: float = 0.0) -> None:
        self._producer.poll(timeout)

    @property
    def queue_depth(self) -> int:
        return len(self._producer)

    def flush(self, timeout: float = 30.0) -> int:
        return self._producer.flush(timeout)


class ThroughputReporter:
    """Prints interval and sustained rates, and optionally records them for the curve."""

    def __init__(
        self,
        counters: PublishCounters,
        interval_s: float,
        csv_path: Path | None = None,
    ) -> None:
        self.counters = counters
        self.interval_s = interval_s
        self.started = time.perf_counter()
        self._last_at = self.started
        self._last_produced = 0
        self._csv_file = csv_path.open("w", newline="", encoding="utf-8") if csv_path else None
        self._csv = csv.writer(self._csv_file) if self._csv_file else None
        if self._csv:
            self._csv.writerow(
                [
                    "elapsed_s",
                    "interval_events_per_s",
                    "sustained_events_per_s",
                    "delivered",
                    "failed",
                ]
            )

    def maybe_report(self, queue_depth: int, extra: str = "") -> None:
        now = time.perf_counter()
        if now - self._last_at < self.interval_s:
            return
        c = self.counters
        interval_rate = (c.produced - self._last_produced) / (now - self._last_at)
        elapsed = now - self.started
        sustained = c.produced / elapsed
        print(
            f"[{elapsed:6.1f}s] {interval_rate:10,.0f} ev/s now | "
            f"{sustained:10,.0f} ev/s sustained | "
            f"delivered {c.delivered:,} | failed {c.failed:,} | "
            f"queued {queue_depth:,} | backpressure waits {c.backpressure_waits:,}"
            f"{extra}",
            flush=True,
        )
        if self._csv:
            self._csv.writerow(
                [
                    f"{elapsed:.3f}",
                    f"{interval_rate:.1f}",
                    f"{sustained:.1f}",
                    c.delivered,
                    c.failed,
                ]
            )
        self._last_at = now
        self._last_produced = c.produced

    def summarise(self) -> float:
        elapsed = time.perf_counter() - self.started
        c = self.counters
        sustained = c.produced / elapsed if elapsed > 0 else 0.0
        print(
            f"\nproduced {c.produced:,} readings in {elapsed:.1f}s "
            f"-> {sustained:,.0f} readings/s sustained",
            flush=True,
        )
        print(f"delivered {c.delivered:,}  failed {c.failed:,}", flush=True)
        if c.failed:
            print(f"first delivery error: {c.first_error}", file=sys.stderr, flush=True)
        if c.backpressure_waits:
            print(
                f"producer queue was full {c.backpressure_waits:,} times "
                f"(client-side backpressure; the broker or network was the limit)",
                flush=True,
            )
        if self._csv_file:
            self._csv_file.close()
        return sustained


__all__ = ["KafkaException", "PublishCounters", "ReadingPublisher", "ThroughputReporter"]
