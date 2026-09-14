"""Run the ClickHouse sink: Kafka readings and window scores into the serving store.

    python warehouse.py --from-beginning --stop-after-idle-s 15
    python warehouse.py --duration 600

Its own consumer group, independent of the detector and of the reconciliation harness. That
independence is the point: this process being down costs the dashboard freshness and costs
detection nothing, and the three consumers can be restarted in any order.

Offsets commit only after a batch is in ClickHouse, so a crash replays the batch rather than
dropping it. The replay lands as duplicate rows that `ReplacingMergeTree` collapses on merge,
and the rollup counts distinct sequence numbers so it is right before the merge too.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from confluent_kafka import Consumer, KafkaError

from vigil.settings import ClickHouseSettings, KafkaSettings
from vigil.warehouse import ReadingsWarehouse
from vigil.warehouse.sink import ClickHouseSink

log = logging.getLogger("vigil.warehouse")

_STOP = False


def _request_stop(*_: object) -> None:
    global _STOP
    _STOP = True


def run(args: argparse.Namespace) -> int:
    kafka = KafkaSettings.from_env()
    warehouse = ReadingsWarehouse(ClickHouseSettings.from_env())
    applied = warehouse.apply_schema()
    log.info("clickhouse schema applied (%d statements)", applied)

    sink = ClickHouseSink(
        warehouse, batch_size=args.batch_size, max_batch_age_s=args.max_batch_age_s
    )
    consumer = Consumer(
        {
            "bootstrap.servers": kafka.bootstrap,
            "group.id": args.group,
            "auto.offset.reset": "earliest" if args.from_beginning else "latest",
            # Manual commit, after the write. This is the whole of the delivery guarantee.
            "enable.auto.commit": False,
            # The scores topic is written inside Flink's transactions; reading uncommitted
            # would surrender the exactly-once chain at its last link.
            "isolation.level": "read_committed",
            "max.poll.interval.ms": 600_000,
        }
    )
    topics = [kafka.readings_topic]
    if not args.no_scores:
        topics.append(kafka.scores_topic)
    consumer.subscribe(topics)
    log.info("subscribed to %s as group %s", ", ".join(topics), args.group)

    started = time.monotonic()
    last_record = time.monotonic()
    try:
        while not _STOP:
            if args.duration and time.monotonic() - started >= args.duration:
                break
            message = consumer.poll(timeout=1.0)
            now = time.monotonic()
            if message is None:
                if args.stop_after_idle_s and now - last_record >= args.stop_after_idle_s:
                    log.info("idle for %.0fs, stopping", args.stop_after_idle_s)
                    break
            elif message.error():
                if message.error().code() != KafkaError._PARTITION_EOF:
                    log.warning("kafka error: %s", message.error())
            else:
                last_record = now
                if message.topic() == kafka.readings_topic:
                    sink.add_reading(message.value())
                else:
                    sink.add_score(message.value())

            if sink.should_flush():
                _flush_and_commit(sink, consumer)

        _flush_and_commit(sink, consumer)
    finally:
        consumer.close()
        warehouse.close()

    print(sink.counters.line())
    return 0


def _flush_and_commit(sink: ClickHouseSink, consumer: Consumer) -> None:
    """Write, then commit. Never the other way round.

    Committing first would make a crash between the two a silent data loss -- the offsets
    would say the records were handled and no row would exist. This order makes the same
    crash a replay, which the table's identity key absorbs.
    """
    if sink.pending == 0:
        return
    written = sink.flush()
    consumer.commit(asynchronous=False)
    log.debug("wrote %d rows and committed", written)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--group", default="vigil-warehouse", help="Kafka consumer group id")
    p.add_argument("--from-beginning", action="store_true")
    p.add_argument("--duration", type=float, default=0.0, help="seconds; 0 means run forever")
    p.add_argument("--stop-after-idle-s", type=float, default=0.0)
    p.add_argument("--batch-size", type=int, default=5_000)
    p.add_argument("--max-batch-age-s", type=float, default=5.0)
    p.add_argument(
        "--no-scores",
        action="store_true",
        help="readings only; use when the Flink profile is down and the scores topic is empty",
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
