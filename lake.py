"""Run the Iceberg lake sink: land the readings stream durably, effectively once.

    python lake.py --from-beginning --stop-after-idle-s 15
    python lake.py --duration 600

**This sink does not use Kafka's offset store.** It assigns partitions explicitly and seeks
to the offsets recorded in the lake's current Iceberg snapshot, because those offsets and the
rows they produced were committed in the same atomic operation (ADR-044). A consumer-group
commit would be a second store that can disagree with the first, and every disagreement
between them is either a duplicate or a gap.

The consequence worth stating plainly: restart this process and it resumes exactly where the
lake says it got to, re-reading the batch that was in flight when it died. That batch was
never committed, so re-writing it creates no duplicate. `--verify` checks that claim against
the stored bytes rather than asserting it.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

from confluent_kafka import Consumer, KafkaError, TopicPartition

from vigil.lake import ReadingsLake
from vigil.readings import Reading
from vigil.settings import KafkaSettings, LakeSettings

log = logging.getLogger("vigil.lake")

_STOP = False


def _request_stop(*_: object) -> None:
    global _STOP
    _STOP = True


def _assign_from_lake(
    consumer: Consumer, lake: ReadingsLake, topic: str, partitions: int, from_beginning: bool
) -> dict[str, int]:
    """Seek each partition to where the lake says this sink got to.

    A partition the lake has never recorded starts at the beginning or the end depending on
    `from_beginning`, which is the only place that flag has any effect: once a snapshot
    exists its offsets win, because they are the ones that match the stored rows.
    """
    committed = lake.committed_offsets()
    assignment = []
    for partition in range(partitions):
        key = f"{topic}-{partition}"
        # OFFSET_BEGINNING / OFFSET_END for a partition the lake has never recorded.
        unseen = -2 if from_beginning else -1
        assignment.append(TopicPartition(topic, partition, committed.get(key, unseen)))
    consumer.assign(assignment)
    log.info(
        "resumed from the lake: %s",
        json.dumps(committed, sort_keys=True) if committed else "no snapshot yet, starting fresh",
    )
    return committed


def run(args: argparse.Namespace) -> int:
    kafka = KafkaSettings.from_env()
    lake = ReadingsLake(LakeSettings.from_env())
    lake.ensure_table()

    if args.verify:
        return _verify(lake)

    consumer = Consumer(
        {
            "bootstrap.servers": kafka.bootstrap,
            "group.id": args.group,
            # Deliberately off. The lake's snapshot is the offset store; a second one would
            # be a second opinion about the same fact.
            "enable.auto.commit": False,
            "max.poll.interval.ms": 600_000,
        }
    )
    _assign_from_lake(
        consumer, lake, kafka.readings_topic, kafka.readings_partitions, args.from_beginning
    )

    batch: list[Reading] = []
    # Next offset to read per partition, which is what a resume seeks to -- not the last
    # offset consumed. Storing the last one would re-read it on every restart.
    next_offsets: dict[str, int] = dict(lake.committed_offsets())
    written = 0
    commits = 0
    undecodable = 0
    started = time.monotonic()
    last_record = time.monotonic()
    oldest_in_batch = time.monotonic()

    def flush() -> None:
        nonlocal written, commits, batch
        if not batch:
            return
        appended = lake.append(batch, next_offsets)
        written += appended
        commits += 1
        log.info("committed %d readings to the lake at %s", appended, json.dumps(next_offsets))
        batch = []

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
                if not batch:
                    oldest_in_batch = now
                try:
                    batch.append(Reading.from_json(message.value()))
                    next_offsets[f"{message.topic()}-{message.partition()}"] = message.offset() + 1
                except Exception:  # noqa: BLE001 - a bad record is counted, not fatal
                    undecodable += 1

            if batch and (
                len(batch) >= args.batch_size
                or (time.monotonic() - oldest_in_batch) >= args.max_batch_age_s
            ):
                flush()

        flush()
    finally:
        consumer.close()

    print(
        f"lake sink: {written:,} readings | {commits:,} snapshots | "
        f"{undecodable:,} undecodable | offsets {json.dumps(next_offsets, sort_keys=True)}"
    )
    return 0


def _verify(lake: ReadingsLake) -> int:
    """Check the effectively-once claim against the stored bytes.

    Exits non-zero if the lake holds a duplicate or a gap, so the shell's status is the
    answer rather than something a reader has to infer from the output.
    """
    identities = lake.channel_identities()
    duplicates = lake.duplicate_rows()
    rows = lake.row_count()
    drift = sum(c.drift for c in identities)

    print(f"lake: {rows:,} rows | {len(identities)} channels | {lake.snapshot_count()} snapshots")
    print(f"offsets in current snapshot: {json.dumps(lake.committed_offsets(), sort_keys=True)}")
    reused = sum(c.reused_seq for c in identities)
    for c in identities:
        flag = "" if c.drift == 0 else f"   <- sequence gap {c.drift:+d}"
        reuse = f"  reused-seq {c.reused_seq:,}" if c.reused_seq else ""
        print(f"  {c.channel:<34} readings {c.readings:>8,}  span {c.span:>8,}{reuse}{flag}")
    print(f"duplicate rows (same channel+seq+event_ts): {duplicates}")
    print(f"sequence gaps: {drift}")
    if reused:
        print(
            f"readings sharing a seq with another: {reused:,} -- the producer's sequence "
            "restarted inside this table's history (ADR-046). Not a lake fault."
        )
    if duplicates or drift:
        print("LAKE NOT CLEAN")
        return 1
    print("LAKE CLEAN: no duplicates, no gaps")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--group", default="vigil-lake", help="only used for broker-side metadata")
    p.add_argument("--from-beginning", action="store_true")
    p.add_argument("--duration", type=float, default=0.0, help="seconds; 0 means run forever")
    p.add_argument("--stop-after-idle-s", type=float, default=0.0)
    p.add_argument("--batch-size", type=int, default=10_000)
    p.add_argument("--max-batch-age-s", type=float, default=10.0)
    p.add_argument(
        "--verify",
        action="store_true",
        help="audit the stored table for duplicates and gaps, then exit",
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
