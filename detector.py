"""The detection spine: Kafka readings -> event-time windows -> detectors -> episodes.

One process, one consumer group. It reads the readings topic, assigns readings to
event-time sliding windows per channel, scores every closed window, merges consecutive
flagged windows into episodes, and writes those to Postgres.

Offsets are committed **after** the episodes derived from those windows are durably
written, never before. Committing first would mean a crash in between loses episodes the
platform has already claimed to have found. Because the sink upserts on
(channel, t_start_ms, raised_by), a replay after a crash re-derives the same episodes
rather than duplicating them -- at-least-once delivery plus an idempotent sink is
effectively-once at the sink, which is precisely the claim `docs/CORRECTNESS.md` makes and
no more (Flink and true 2PC arrive in Phase 2).

    python detector.py                      # run until Ctrl-C
    python detector.py --duration 120       # bounded run, for tests and demos
    python detector.py --from-beginning     # replay the whole topic
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections import Counter

from confluent_kafka import Consumer, KafkaError, TopicPartition

from vigil.detectors.zscore import RollingZScoreDetector
from vigil.episodes import EpisodeBuilder
from vigil.readings import Reading
from vigil.settings import KafkaSettings, PostgresSettings
from vigil.store import EpisodeStore
from vigil.windows import SlidingWindowAssigner

log = logging.getLogger("vigil.detector")


class DetectionSpine:
    """Windows, scores, and episode-building for one consumer."""

    def __init__(
        self,
        store: EpisodeStore,
        *,
        window_ms: int,
        slide_ms: int,
        lateness_ms: int,
        min_points: int,
        threshold: float,
        merge_gap_ms: int,
        warmup_samples: int,
    ) -> None:
        self.store = store
        self.windows = SlidingWindowAssigner(
            size_ms=window_ms,
            slide_ms=slide_ms,
            allowed_lateness_ms=lateness_ms,
            min_points=min_points,
        )
        self.baseline = RollingZScoreDetector(warmup_samples=warmup_samples)
        self.builder = EpisodeBuilder(threshold=threshold, merge_gap_ms=merge_gap_ms)
        self.readings_seen = 0
        self.episodes_written = 0
        self.origin_counts: Counter[str] = Counter()
        self.latencies: list[float] = []

    def consume(self, reading: Reading) -> list[int]:
        """Feed one reading; returns the ids of any episodes this closed and persisted."""
        self.readings_seen += 1
        written: list[int] = []
        for window in self.windows.add(reading):
            score = self.baseline.score(window)
            # Fold the window into the reference only after scoring it, so it cannot
            # contribute to the distribution it is being judged against.
            self.baseline.observe(window)
            if score is None:
                continue
            self.latencies.append(score.latency_ms)
            episode = self.builder.add(score, window.injected)
            if episode is not None:
                written.append(self._persist(episode))
        return written

    def drain(self) -> list[int]:
        """Close every open window and episode. For shutdown and bounded runs."""
        written: list[int] = []
        for window in self.windows.close_all():
            score = self.baseline.score(window)
            self.baseline.observe(window)
            if score is None:
                continue
            self.latencies.append(score.latency_ms)
            episode = self.builder.add(score, window.injected)
            if episode is not None:
                written.append(self._persist(episode))
        for episode in self.builder.close_all():
            written.append(self._persist(episode))
        return written

    def _persist(self, episode) -> int:
        episode_id = self.store.record_episode(episode)
        self.episodes_written += 1
        for origin in episode.injected_origins:
            self.origin_counts[origin] += 1
        log.info(
            "episode %s %s [%d, %d] peak=%.1f windows=%d origins=%s",
            episode_id,
            episode.channel,
            episode.t_start_ms,
            episode.t_end_ms,
            episode.peak_score,
            episode.window_count,
            ",".join(episode.injected_origins) or "-",
        )
        return episode_id

    def latency_report(self) -> str:
        if not self.latencies:
            return "no windows scored"
        ordered = sorted(self.latencies)

        def pct(p: float) -> float:
            return ordered[min(int(len(ordered) * p), len(ordered) - 1)]

        return (
            f"hot-path scoring latency over {len(ordered):,} windows: "
            f"p50 {pct(0.50):.3f} ms | p95 {pct(0.95):.3f} ms | p99 {pct(0.99):.3f} ms | "
            f"max {ordered[-1]:.3f} ms"
        )


def build_consumer(bootstrap: str, group: str, from_beginning: bool) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group,
            "auto.offset.reset": "earliest" if from_beginning else "latest",
            # Offsets are committed by hand after the episodes are durable. Auto-commit
            # would advance them on a timer, silently losing episodes on a crash.
            "enable.auto.commit": False,
            "max.poll.interval.ms": 600_000,
        }
    )


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    kafka = KafkaSettings.from_env()
    bootstrap = args.bootstrap or kafka.bootstrap
    topic = args.topic or kafka.readings_topic

    store = EpisodeStore(PostgresSettings.from_env())
    store.apply_schema()

    spine = DetectionSpine(
        store,
        window_ms=args.window_ms,
        slide_ms=args.slide_ms,
        lateness_ms=args.lateness_ms,
        min_points=args.min_points,
        threshold=args.threshold,
        merge_gap_ms=args.merge_gap_ms or args.slide_ms * 2,
        warmup_samples=args.warmup_samples,
    )

    consumer = build_consumer(bootstrap, args.group, args.from_beginning)
    consumer.subscribe([topic])

    stopping = False

    def request_stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)

    print(
        f"consuming {topic!r} at {bootstrap} as group {args.group!r} | "
        f"windows {args.window_ms / 1000:g}s/{args.slide_ms / 1000:g}s | "
        f"threshold {args.threshold} | episodes -> postgres",
        flush=True,
    )

    started = time.perf_counter()
    deadline = started + args.duration if args.duration > 0 else float("inf")
    last_report = started
    malformed = 0
    idle_polls = 0

    try:
        while not stopping and time.perf_counter() < deadline:
            message = consumer.poll(1.0)
            if message is None:
                idle_polls += 1
                if args.stop_after_idle_s and idle_polls * 1.0 >= args.stop_after_idle_s:
                    log.info("no messages for %.0fs; stopping", args.stop_after_idle_s)
                    break
                continue
            if message.error():
                if message.error().code() == KafkaError._PARTITION_EOF:
                    continue
                log.error("consume error: %s", message.error())
                continue

            idle_polls = 0
            try:
                reading = Reading.from_json(message.value())
            except (ValueError, KeyError, TypeError):
                # A record we cannot parse is a real event worth counting, not a reason to
                # stop the stream. Silently skipping it would hide a producer regression.
                malformed += 1
                continue

            spine.consume(reading)

            # Commit only once the episodes derived from these windows are durable.
            if spine.readings_seen % args.commit_every == 0:
                consumer.commit(
                    offsets=[
                        TopicPartition(message.topic(), message.partition(), message.offset() + 1)
                    ],
                    asynchronous=False,
                )

            now = time.perf_counter()
            if now - last_report >= args.report_interval:
                elapsed = now - started
                print(
                    f"[{elapsed:6.1f}s] readings {spine.readings_seen:,} "
                    f"({spine.readings_seen / elapsed:,.0f}/s) | "
                    f"windows {spine.windows.emitted_windows:,} | "
                    f"scored {spine.baseline.windows_scored:,} "
                    f"(cold {spine.baseline.windows_skipped_cold:,}) | "
                    f"flagged {spine.builder.windows_flagged:,} | "
                    f"episodes {spine.episodes_written:,} | "
                    f"late {spine.windows.late_readings:,}",
                    flush=True,
                )
                last_report = now
    finally:
        print("\ndraining open windows...", flush=True)
        spine.drain()
        try:
            consumer.commit(asynchronous=False)
        except Exception as exc:  # noqa: BLE001 - a failed final commit only costs a replay
            log.warning("final commit failed (a replay will re-derive these): %s", exc)
        consumer.close()

        elapsed = time.perf_counter() - started
        print(
            f"\nread {spine.readings_seen:,} readings in {elapsed:.1f}s "
            f"({spine.readings_seen / max(elapsed, 1e-9):,.0f}/s)",
            flush=True,
        )
        print(
            f"windows emitted {spine.windows.emitted_windows:,} | "
            f"scored {spine.baseline.windows_scored:,} | "
            f"skipped cold {spine.baseline.windows_skipped_cold:,} | "
            f"thin dropped {spine.windows.dropped_thin_windows:,} | "
            f"late readings {spine.windows.late_readings:,}",
            flush=True,
        )
        print(
            f"windows flagged {spine.builder.windows_flagged:,} -> "
            f"episodes {spine.episodes_written:,} "
            f"(merged {spine.builder.windows_flagged - spine.episodes_written:,} "
            f"consecutive windows into existing incidents)",
            flush=True,
        )
        if spine.origin_counts:
            print(
                "injected ground truth inside episodes: "
                + ", ".join(f"{k}={v}" for k, v in sorted(spine.origin_counts.items())),
                flush=True,
            )
        if malformed:
            print(f"malformed records skipped: {malformed:,}", file=sys.stderr, flush=True)
        print(spine.latency_report(), flush=True)
        store.close()

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--topic", default=None, help="override READINGS_TOPIC")
    p.add_argument("--bootstrap", default=None, help="override KAFKA_BOOTSTRAP")
    p.add_argument("--group", default="vigil-detector", help="consumer group id")
    p.add_argument(
        "--from-beginning", action="store_true", help="replay the topic from the earliest offset"
    )
    p.add_argument("--duration", type=float, default=0, help="seconds to run; 0 runs until Ctrl-C")
    p.add_argument(
        "--stop-after-idle-s",
        type=float,
        default=0,
        help="stop once the topic has been quiet this long; 0 disables",
    )

    w = p.add_argument_group("windowing")
    w.add_argument("--window-ms", type=int, default=30_000)
    w.add_argument("--slide-ms", type=int, default=10_000)
    w.add_argument("--lateness-ms", type=int, default=5_000)
    w.add_argument("--min-points", type=int, default=8)

    d = p.add_argument_group("detection")
    d.add_argument("--threshold", type=float, default=8.0, help="score above which a window flags")
    d.add_argument(
        "--merge-gap-ms",
        type=int,
        default=0,
        help="quiet gap that closes an episode; defaults to two slides",
    )
    d.add_argument("--warmup-samples", type=int, default=120)

    p.add_argument("--commit-every", type=int, default=500, help="readings between offset commits")
    p.add_argument("--report-interval", type=float, default=10.0)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
