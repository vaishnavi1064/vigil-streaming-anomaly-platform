"""Synthetic multi-channel load generator and throughput harness.

Two modes, one code path:

  --rate N   target-rate mode. Produces N events/s across all channels, paced against a
             virtual event-time clock that tracks wall time. Event timestamps are evenly
             spaced, so windowed detection sees a realistic timeline.

  --rate 0   blast mode. Produces as fast as the client and broker will accept, and event
             time is the wall clock at produce. This measures the pipeline's ceiling and
             its backpressure behaviour; it is not meant to produce a realistic timeline.

Both modes report sustained events/s. Report the hardware alongside any number taken from
this tool -- see docs/SCALE.md.

Examples:
    python loadgen.py --rate 2000 --duration 60
    python loadgen.py --rate 0 --duration 30 --channels 32
"""

from __future__ import annotations

import argparse
import csv
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from confluent_kafka import KafkaException, Producer

from vigil.readings import Reading
from vigil.settings import KafkaSettings
from vigil.synthetic import ChannelSimulator, default_fleet


@dataclass
class ProduceCounters:
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


def build_producer(bootstrap: str, linger_ms: int, batch_size: int) -> Producer:
    return Producer(
        {
            "bootstrap.servers": bootstrap,
            # Idempotence is what lets the reconciliation harness attribute any duplicate
            # it finds to a real defect rather than to a producer retry. It implies
            # acks=all and bounded in-flight requests; that throughput cost is the price of
            # being able to make an honest no-duplicates claim.
            "enable.idempotence": True,
            "compression.type": "lz4",
            "linger.ms": linger_ms,
            "batch.size": batch_size,
            "queue.buffering.max.messages": 500_000,
            "queue.buffering.max.kbytes": 512_000,
        }
    )


class ThroughputReporter:
    """Prints interval and sustained rates, and optionally records them for the curve."""

    def __init__(self, counters: ProduceCounters, interval_s: float, csv_path: Path | None):
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

    def maybe_report(self, queue_depth: int) -> None:
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
            f"queued {queue_depth:,} | backpressure waits {c.backpressure_waits:,}",
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
            f"\nproduced {c.produced:,} events in {elapsed:.1f}s "
            f"-> {sustained:,.0f} events/s sustained",
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


def run(args: argparse.Namespace) -> int:
    kafka = KafkaSettings.from_env()
    bootstrap = args.bootstrap or kafka.bootstrap
    topic = args.topic or kafka.readings_topic

    specs = default_fleet(args.channels, seed=args.seed)
    sims = [
        ChannelSimulator(spec, seed=args.seed + i, anomalies_per_hour=args.anomalies_per_hour)
        for i, spec in enumerate(specs)
    ]
    seqs = [0] * len(specs)

    counters = ProduceCounters()
    producer = build_producer(bootstrap, args.linger_ms, args.batch_size)
    reporter = ThroughputReporter(counters, args.report_interval, args.csv)

    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)

    blast = args.rate == 0
    per_event_s = 0.0 if blast else 1.0 / args.rate
    t0_wall = time.time()
    start_perf = time.perf_counter()
    deadline = start_perf + args.duration if args.duration > 0 else float("inf")

    mode = "BLAST (unpaced)" if blast else f"{args.rate:,.0f} ev/s target"
    print(
        f"producing to {topic!r} at {bootstrap} | {mode} | "
        f"{args.channels} channels | seed {args.seed}",
        flush=True,
    )

    k = 0
    try:
        while not stopping and time.perf_counter() < deadline:
            idx = k % len(sims)
            if blast:
                event_ts_ms = int(time.time() * 1000)
                stream_t_s = time.perf_counter() - start_perf
            else:
                offset_s = k * per_event_s
                event_ts_ms = int((t0_wall + offset_s) * 1000)
                stream_t_s = offset_s

            sample = sims[idx].sample(stream_t_s)
            seqs[idx] += 1
            reading = Reading(
                channel=specs[idx].name,
                seq=seqs[idx],
                event_ts_ms=event_ts_ms,
                value=sample.value,
                injected=sample.injected.value if sample.injected else None,
            )

            while True:
                try:
                    producer.produce(
                        topic,
                        key=specs[idx].name,
                        value=reading.to_json(),
                        # Kafka's message timestamp carries event time, so a consumer gets
                        # it without parsing the payload.
                        timestamp=event_ts_ms,
                        on_delivery=counters.on_delivery,
                    )
                    counters.produced += 1
                    break
                except BufferError:
                    counters.backpressure_waits += 1
                    producer.poll(0.05)
                except KafkaException as exc:
                    print(f"produce failed: {exc}", file=sys.stderr, flush=True)
                    return 1

            k += 1
            producer.poll(0)
            reporter.maybe_report(len(producer))

            if not blast:
                # Pace against the virtual clock rather than sleeping a fixed slice, so
                # scheduler jitter cannot accumulate into rate drift over a long run.
                slack = (start_perf + k * per_event_s) - time.perf_counter()
                if slack > 0.0005:
                    time.sleep(slack)
    finally:
        print("\nflushing producer...", flush=True)
        remaining = producer.flush(30.0)
        if remaining:
            print(f"{remaining:,} messages still queued after 30s flush", file=sys.stderr)
        reporter.summarise()

    return 1 if counters.failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--rate",
        type=float,
        default=2000,
        help="target events/s across all channels; 0 means blast mode (unpaced)",
    )
    p.add_argument(
        "--duration", type=float, default=60, help="seconds to run; 0 runs until Ctrl-C"
    )
    p.add_argument("--channels", type=int, default=8, help="number of synthetic channels")
    p.add_argument(
        "--seed", type=int, default=1729, help="RNG seed; identical seeds replay identically"
    )
    p.add_argument(
        "--anomalies-per-hour",
        type=float,
        default=12.0,
        help="expected injected anomaly episodes per channel per hour",
    )
    p.add_argument("--topic", default=None, help="override READINGS_TOPIC")
    p.add_argument("--bootstrap", default=None, help="override KAFKA_BOOTSTRAP")
    p.add_argument("--linger-ms", type=int, default=20)
    p.add_argument("--batch-size", type=int, default=1_048_576)
    p.add_argument(
        "--report-interval", type=float, default=2.0, help="seconds between rate lines"
    )
    p.add_argument("--csv", type=Path, default=None, help="write per-interval throughput rows here")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
