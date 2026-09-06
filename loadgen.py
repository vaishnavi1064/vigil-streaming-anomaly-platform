"""Synthetic load generator and throughput harness.

This is the reproducible half of the data plan. The live source is the solar fleet feed
(`mqtt_bridge.py`); this generator exists because throughput, chaos and correctness tests
need a stream that is seeded, labelled, and drivable at an exact rate -- none of which a
public feed with no SLA can provide.

Two modes:

  --rate N   target-rate mode. N events/s across all channels, paced against a virtual
             event-time clock that tracks wall time, so timestamps are evenly spaced.

  --rate 0   blast mode. As fast as the client and broker will accept, event time taken
             from the wall clock. Measures the ceiling and backpressure behaviour; it is
             not meant to produce a realistic timeline.

Both modes report sustained events/s. Report the hardware alongside any number taken from
this tool -- see docs/SCALE.md.

    python loadgen.py --rate 2000 --duration 60
    python loadgen.py --rate 0 --duration 30 --channels 32
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from vigil.ingest.publisher import ReadingPublisher, ThroughputReporter
from vigil.ingest.synthetic_source import SyntheticFleetSource
from vigil.settings import KafkaSettings


def run(args: argparse.Namespace) -> int:
    kafka = KafkaSettings.from_env()
    bootstrap = args.bootstrap or kafka.bootstrap
    topic = args.topic or kafka.readings_topic

    source = SyntheticFleetSource(
        channels=args.channels,
        rate_per_s=args.rate,
        seed=args.seed,
        anomalies_per_hour=args.anomalies_per_hour,
        duration_s=args.duration,
    )
    publisher = ReadingPublisher(
        bootstrap, topic, linger_ms=args.linger_ms, batch_size=args.batch_size
    )
    reporter = ThroughputReporter(publisher.counters, args.report_interval, args.csv)

    signal.signal(signal.SIGINT, lambda *_: source.close())

    mode = "BLAST (unpaced)" if source.blast else f"{args.rate:,.0f} ev/s target"
    print(
        f"producing to {topic!r} at {bootstrap} | {mode} | "
        f"{args.channels} channels | seed {args.seed}",
        flush=True,
    )

    try:
        with source:
            for reading in source.readings():
                publisher.publish(reading)
                publisher.poll(0.0)
                reporter.maybe_report(publisher.queue_depth)
    finally:
        print("\nflushing producer...", flush=True)
        remaining = publisher.flush(30.0)
        if remaining:
            print(f"{remaining:,} messages still queued after 30s flush", file=sys.stderr)
        reporter.summarise()

    return 1 if publisher.counters.failed else 0


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
    p.add_argument("--duration", type=float, default=60, help="seconds to run; 0 runs until Ctrl-C")
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
    p.add_argument("--report-interval", type=float, default=2.0, help="seconds between rate lines")
    p.add_argument("--csv", type=Path, default=None, help="write per-interval throughput rows here")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
