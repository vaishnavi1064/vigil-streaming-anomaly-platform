"""Bridge the public solar-fleet MQTT feed into the Kafka readings topic.

This is the live source. It subscribes to mqtt.tdengine.com at QoS 0, fans each message
out into one reading per (entity, metric), stamps per-channel sequence numbers, and
publishes through the same path `loadgen.py` uses.

The edge guarantee is at-most-once and is scoped that way everywhere (ADR-011): QoS 0 can
drop a message between the public broker and this process, and the feed offers no history
API to backfill from. What the bridge can do is notice, so silence on a channel that was
arriving on a cadence is detected and counted rather than passing as normal.

The feed is free and carries no SLA. One connection per process, exponential reconnect
backoff to a one-minute ceiling, an identifying client id.

    python mqtt_bridge.py                        # MQTT_TOPIC from .env (default inverters)
    python mqtt_bridge.py --topic strings        # the high-rate channel, for scale tests
    python mqtt_bridge.py --topic sites,weather  # several at once
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

from vigil.ingest.publisher import ReadingPublisher, ThroughputReporter
from vigil.ingest.solar_feed import SOLAR_TOPICS, TOPIC_MAPPINGS, SolarFleetSource
from vigil.ingest.source import IngestGapWatch
from vigil.settings import KafkaSettings, MqttSettings

log = logging.getLogger("vigil.bridge")


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    kafka = KafkaSettings.from_env()
    mqtt = MqttSettings.from_env()

    topics = tuple(t.strip() for t in (args.topic or mqtt.topic).split(",") if t.strip())
    unknown = [t for t in topics if t not in TOPIC_MAPPINGS and t != "#"]
    if unknown:
        print(
            f"no field mapping for {unknown}; known topics: {', '.join(SOLAR_TOPICS)}",
            file=sys.stderr,
        )
        return 2

    bootstrap = args.bootstrap or kafka.bootstrap
    kafka_topic = args.kafka_topic or kafka.readings_topic

    source = SolarFleetSource(mqtt.host, mqtt.port, topics)
    publisher = ReadingPublisher(bootstrap, kafka_topic)
    reporter = ThroughputReporter(publisher.counters, args.report_interval, args.csv)
    gaps = IngestGapWatch(gap_factor=args.gap_factor, min_gap_s=args.min_gap_s)

    signal.signal(signal.SIGINT, lambda *_: source.close())

    for topic in topics:
        mapping = TOPIC_MAPPINGS.get(topic)
        if mapping:
            log.info("%s: primary detection metric is %s", topic, mapping.primary_metric)

    print(
        f"bridging mqtt://{mqtt.host}:{mqtt.port} {topics} -> kafka {kafka_topic!r} at {bootstrap}",
        flush=True,
    )

    deadline = time.perf_counter() + args.duration if args.duration > 0 else float("inf")
    try:
        with source:
            for reading in source.readings():
                gap = gaps.observe(reading.channel, time.time())
                if gap is not None:
                    log.warning(
                        "ingest gap: %s silent for %.1fs (cadence %.2fs, ~%d readings missed)",
                        gap.channel,
                        gap.duration_s,
                        gap.expected_cadence_s,
                        gap.estimated_missing,
                    )
                publisher.publish(reading)
                publisher.poll(0.0)
                reporter.maybe_report(
                    publisher.queue_depth,
                    extra=(
                        f" | mqtt msgs {source.messages_received:,}"
                        f" | channels {source.sequences.channels:,}"
                        f" | gaps {len(gaps.gaps):,}"
                    ),
                )
                if time.perf_counter() >= deadline:
                    source.close()
                    break
    finally:
        print("\nflushing producer...", flush=True)
        remaining = publisher.flush(30.0)
        if remaining:
            print(f"{remaining:,} messages still queued after 30s flush", file=sys.stderr)
        reporter.summarise()
        print(
            f"mqtt messages received {source.messages_received:,} | "
            f"unparsable {source.messages_unparsable:,} | "
            f"dropped at the intake queue {source.messages_dropped:,}",
            flush=True,
        )
        print(
            f"channels seen {source.sequences.channels:,} | "
            f"ingest gaps detected {len(gaps.gaps):,} "
            f"(~{gaps.estimated_missing:,} readings unaccounted for at the edge)",
            flush=True,
        )

    return 1 if publisher.counters.failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--topic",
        default=None,
        help=(
            "MQTT topic(s), comma separated; overrides MQTT_TOPIC. "
            f"Known: {', '.join(SOLAR_TOPICS)}"
        ),
    )
    p.add_argument("--duration", type=float, default=0, help="seconds to run; 0 runs until Ctrl-C")
    p.add_argument("--kafka-topic", default=None, help="override READINGS_TOPIC")
    p.add_argument("--bootstrap", default=None, help="override KAFKA_BOOTSTRAP")
    p.add_argument("--report-interval", type=float, default=5.0, help="seconds between rate lines")
    p.add_argument("--csv", type=Path, default=None, help="write per-interval throughput rows here")
    p.add_argument(
        "--gap-factor",
        type=float,
        default=6.0,
        help="a channel is 'gapped' after this many times its own median inter-arrival",
    )
    p.add_argument("--min-gap-s", type=float, default=1.0, help="never call anything shorter a gap")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
