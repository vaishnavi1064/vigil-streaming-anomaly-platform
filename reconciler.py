"""Run the reconciliation harness: prove the invariants, emit the health signal.

Two checks over one pass, deliberately independent of each other:

  * **Sequence identity.** Every channel's sequence is dense by construction, so
    `readings == max_seq - min_seq + 1` must hold. A shortfall is loss, an excess is
    duplication, and neither is inferable from a row count.
  * **Broker offsets.** What the broker says the log retains, against what this process
    actually consumed. The first check proves the stream was internally consistent; this
    one proves we saw all of it. A harness checking only its own consumption could read
    half the log flawlessly and declare success.

Runs in its own consumer group, separate from the detector's -- sharing offsets would mean
only ever auditing what the detector already managed to read, which is the thing under audit.

    python reconciler.py --from-beginning --stop-after-idle-s 20
    python reconciler.py --duration 14400 --report-interval 300   # the multi-hour soak
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

from vigil.ingest.publisher import ReadingPublisher
from vigil.reconciliation.harness import (
    ReconciliationHarness,
    audit_offsets,
    build_consumer,
    consume_forever,
)
from vigil.reconciliation.lake_audit import audit_lake, compare_to_ledger
from vigil.reconciliation.ledger import HealthThresholds
from vigil.settings import KafkaSettings, LakeSettings, PostgresSettings
from vigil.store import EpisodeStore

log = logging.getLogger("vigil.reconciler")


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    kafka = KafkaSettings.from_env()
    bootstrap = args.bootstrap or kafka.bootstrap
    topic = args.topic or kafka.readings_topic
    context_topic = args.context_topic or kafka.context_topic

    publisher = None if args.no_emit else ReadingPublisher(bootstrap, topic)
    harness = ReconciliationHarness(
        publisher,
        context_topic,
        window_ms=args.window_ms,
        grace_windows=args.grace_windows,
        thresholds=HealthThresholds(
            lag_info_ms=args.lag_info_ms,
            lag_warning_ms=args.lag_warning_ms,
            lag_critical_ms=args.lag_critical_ms,
            grade_lag=not args.ignore_lag,
        ),
        emit_clean=not args.only_disturbed,
    )

    store = None
    if not args.no_store:
        store = EpisodeStore(PostgresSettings.from_env())
        store.apply_schema()

    consumer = build_consumer(bootstrap, args.group, args.from_beginning)
    stopping = False

    def request_stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)

    print(
        f"reconciling {topic!r} at {bootstrap} as group {args.group!r} | "
        f"windows {args.window_ms / 1000:g}s | "
        f"health -> {'nowhere (--no-emit)' if args.no_emit else repr(context_topic)}",
        flush=True,
    )

    started = time.time()
    try:
        consume_forever(
            consumer,
            harness,
            topic=topic,
            duration_s=args.duration,
            stop_after_idle_s=args.stop_after_idle_s,
            report_interval_s=args.report_interval,
            should_stop=lambda: stopping,
        )
    finally:
        harness.drain()
        if publisher is not None:
            publisher.flush(30.0)

        led = harness.ledger
        audit = audit_offsets(consumer, bootstrap, topic, led.total_readings)
        consumer.close()

        elapsed = time.time() - started
        print(f"\n{'=' * 78}", flush=True)
        print(f"reconciliation over {elapsed / 60:.1f} minutes", flush=True)
        print(f"{'=' * 78}", flush=True)
        print(led.report(), flush=True)
        print(audit.summary(), flush=True)
        print(
            f"health windows emitted {harness.health_emitted:,} "
            f"({harness.disturbed_windows:,} disturbed)",
            flush=True,
        )
        if led.late_readings:
            print(
                f"readings arriving after their window closed: {led.late_readings:,} "
                f"({100 * led.late_readings / max(led.total_readings, 1):.2f}%) -- counted in "
                f"the identity invariant, but not attributable to a health window",
                flush=True,
            )
        if harness.malformed:
            print(f"malformed records skipped: {harness.malformed:,}", file=sys.stderr)

        unhealthy = led.unhealthy_channels
        if unhealthy:
            print(f"\n{len(unhealthy)} channel(s) failed their identity invariant:", flush=True)
            for channel in sorted(unhealthy, key=lambda c: -abs(c.drift))[:20]:
                print(
                    f"  {channel.channel:<40} readings {channel.readings:>9,} "
                    f"span {channel.span:>9,} drift {channel.drift:+,} "
                    f"missing {channel.missing:,} dupes {channel.duplicates:,} "
                    f"reordered {channel.regressions:,}",
                    flush=True,
                )

        lake_ok = _audit_lake(led) if args.audit_lake else True

        if store is not None:
            written = _persist(store, harness, audit, elapsed)
            print(f"persisted {written:,} health windows to postgres", flush=True)
            store.close()

        if args.report_json:
            _write_report(args.report_json, harness, audit, elapsed)
            print(f"report written to {args.report_json}", flush=True)

    # A non-zero exit on drift makes this usable as a gate in CI and in the chaos suite.
    return 0 if (led.total_drift == 0 and not unhealthy and lake_ok) else 1


def _audit_lake(led) -> bool:
    """Audit the stored lake and compare it, channel by channel, against the live ledger.

    Returns whether the lake is clean *and* agrees with the ledger. Two counts derived from
    different places agreeing is the only reason to believe either: the ledger counted
    records as they streamed past, the lake counted rows written to object storage, and the
    two share no code and no state.
    """
    try:
        from vigil.lake import ReadingsLake

        audit = audit_lake(ReadingsLake(LakeSettings.from_env()))
    except Exception as exc:  # noqa: BLE001 - an unreachable lake is a reported finding
        print()
        print(f"lake audit skipped: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return True

    print()
    print("=" * 78, flush=True)
    print("lake audit (Iceberg, the stored source of truth)", flush=True)
    print("=" * 78, flush=True)
    print(audit.line(), flush=True)
    if audit.reused_seq:
        print(
            f"  {audit.reused_seq:,} readings share a seq with another: the producer's "
            "sequence restarted inside this table's history (ADR-046), which is a fact "
            "about the producer and not a fault in the lake",
            flush=True,
        )

    ledger_counts = {name: ledger.readings for name, ledger in led.channels.items()}
    agreements = compare_to_ledger(audit, ledger_counts)
    disagreeing = [a for a in agreements if not a.agrees]
    print()
    print(
        f"ledger vs lake: {len(agreements) - len(disagreeing)}/{len(agreements)} channels agree",
        flush=True,
    )
    for a in disagreeing[:20]:
        print(
            f"  {a.channel:<40} ledger {a.ledger_readings:>9,}  lake {a.lake_readings:>9,}"
            f"  difference {a.difference:+,}",
            flush=True,
        )
    if disagreeing:
        print(
            "A difference here is expected when the two did not cover the same span of the "
            "log -- the lake retains what the broker has already aged out, and this run read "
            "only what the broker still held. It is a finding to explain, not automatically "
            "a fault.",
            flush=True,
        )
    return audit.clean and not disagreeing


def _persist(store: EpisodeStore, harness, audit, elapsed_s: float) -> int:
    led = harness.ledger
    with store._conn.cursor() as cur:
        for health in harness.recent:
            cur.execute(
                """
                INSERT INTO pipeline_health (
                    window_start_ms, window_end_ms, channels, readings, missing,
                    duplicates, regressions, max_lag_ms, severity
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (window_start_ms) DO UPDATE SET
                    readings = EXCLUDED.readings,
                    missing = EXCLUDED.missing,
                    duplicates = EXCLUDED.duplicates,
                    regressions = EXCLUDED.regressions,
                    max_lag_ms = GREATEST(pipeline_health.max_lag_ms, EXCLUDED.max_lag_ms),
                    severity = EXCLUDED.severity
                """,
                (
                    health.window_start_ms,
                    health.window_end_ms,
                    health.channels,
                    health.readings,
                    health.missing,
                    health.duplicates,
                    health.regressions,
                    health.max_lag_ms,
                    str(health.severity),
                ),
            )
        cur.execute(
            """
            INSERT INTO reconciliation_runs (
                topic, duration_s, readings, channels, drift, missing, duplicates,
                regressions, broker_available, broker_consumed, offset_drift
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                audit.topic,
                elapsed_s,
                led.total_readings,
                len(led.channels),
                led.total_drift,
                led.total_missing,
                led.total_duplicates,
                led.total_regressions,
                audit.available,
                audit.consumed,
                audit.drift,
            ),
        )
    return len(harness.recent)


def _write_report(path: Path, harness, audit, elapsed_s: float) -> None:
    led = harness.ledger
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "duration_s": round(elapsed_s, 1),
                "readings": led.total_readings,
                "channels": len(led.channels),
                "drift": led.total_drift,
                "missing": led.total_missing,
                "duplicates": led.total_duplicates,
                "regressions": led.total_regressions,
                "health_windows": harness.health_emitted,
                "disturbed_windows": harness.disturbed_windows,
                "broker": {
                    "low_watermark": audit.low_watermark,
                    "log_end": audit.log_end,
                    "available": audit.available,
                    "consumed": audit.consumed,
                    "offset_drift": audit.drift,
                },
                "unhealthy_channels": [
                    {
                        "channel": c.channel,
                        "readings": c.readings,
                        "span": c.span,
                        "drift": c.drift,
                        "missing": c.missing,
                        "duplicates": c.duplicates,
                        "regressions": c.regressions,
                    }
                    for c in led.unhealthy_channels
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--topic", default=None, help="override READINGS_TOPIC")
    p.add_argument("--context-topic", default=None, help="override CONTEXT_TOPIC")
    p.add_argument("--bootstrap", default=None, help="override KAFKA_BOOTSTRAP")
    p.add_argument("--group", default="vigil-reconciler", help="consumer group id")
    p.add_argument("--from-beginning", action="store_true")
    p.add_argument("--duration", type=float, default=0, help="seconds to run; 0 until Ctrl-C")
    p.add_argument("--stop-after-idle-s", type=float, default=0)
    p.add_argument("--report-interval", type=float, default=15.0)
    p.add_argument("--window-ms", type=int, default=30_000, help="must match the detector's")
    p.add_argument("--grace-windows", type=int, default=1)
    p.add_argument("--lag-info-ms", type=int, default=5_000)
    p.add_argument("--lag-warning-ms", type=int, default=30_000)
    p.add_argument("--lag-critical-ms", type=int, default=120_000)
    p.add_argument(
        "--ignore-lag",
        action="store_true",
        help="do not grade severity on lag. Use for replays and benchmarks: lag is measured "
        "against the wall clock, so replaying an hour-old topic honestly reports an hour of "
        "lag, which is a fact about the data's age rather than a live disturbance",
    )
    p.add_argument(
        "--only-disturbed",
        action="store_true",
        help="publish health only for disturbed windows. Off by default: a consumer must be "
        "able to tell a clean window from a missing signal (ADR-007)",
    )
    p.add_argument(
        "--audit-lake",
        action="store_true",
        help="also audit the Iceberg lake and compare it channel-by-channel against the ledger",
    )
    p.add_argument("--no-emit", action="store_true", help="audit only, publish nothing")
    p.add_argument("--no-store", action="store_true", help="skip persisting to postgres")
    p.add_argument("--report-json", type=Path, default=None, help="write a JSON summary here")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
