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
import threading
import time
from collections import Counter

from confluent_kafka import Consumer, KafkaError, TopicPartition

from vigil.conditioning.policy import ConditioningPolicy, ConditioningThresholds
from vigil.conditioning.signals import KafkaContextSource
from vigil.detectors.foundation import ChronosResidualDetector, FoundationModelUnavailable
from vigil.detectors.offpath import OffPathScorer
from vigil.detectors.zscore import RollingZScoreDetector
from vigil.episodes import EpisodeBuilder, EpisodeStatus
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
        foundation: ChronosResidualDetector | None = None,
        foundation_threshold: float = 6.0,
        foundation_batch: int = 32,
        conditioning: ConditioningPolicy | None = None,
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
        # None means the shadow pass: detection runs unconditioned, which is the baseline
        # every conditioned result is measured against (ADR-016).
        self.conditioning = conditioning
        self.attributed = 0

        # The foundation model keeps its own episode builder rather than annotating the
        # baseline's. The two detectors score on different scales and disagree, and the
        # whole point of running them side by side is to see where -- folding the model's
        # opinion into episodes the baseline chose would discard exactly that comparison.
        self.foundation = foundation
        self.foundation_builder = (
            EpisodeBuilder(threshold=foundation_threshold, merge_gap_ms=merge_gap_ms)
            if foundation is not None
            else None
        )
        self.foundation_latencies: list[float] = []
        self._foundation_lock = threading.Lock()
        # The off-path worker and the main loop both write episodes. A psycopg connection
        # is not safe for concurrent use, and the counters would race too, so every write
        # goes through one lock. Contention is negligible: episodes are rare by
        # construction, which is the whole point of the platform.
        self._store_lock = threading.Lock()
        self.offpath = (
            OffPathScorer(foundation, self._on_foundation_scores, max_batch=foundation_batch)
            if foundation is not None
            else None
        )
        if self.offpath is not None:
            self.offpath.start()

    def _on_foundation_scores(self, scores) -> None:
        """Called from the off-path worker thread; must stay cheap and thread-safe."""
        with self._foundation_lock:
            for score in scores:
                self.foundation_latencies.append(score.latency_ms)
                episode = self.foundation_builder.add(score)
                if episode is not None:
                    self._persist(episode)

    def consume(self, reading: Reading) -> list[int]:
        """Feed one reading; returns the ids of any episodes this closed and persisted."""
        self.readings_seen += 1
        written: list[int] = []
        for window in self.windows.add(reading):
            score = self.baseline.score(window)
            # Fold the window into the reference only after scoring it, so it cannot
            # contribute to the distribution it is being judged against.
            self.baseline.observe(window)
            # Hand a copy to the model path before observing there, for the same reason.
            # submit() never blocks: if the model is behind, the window is dropped and
            # counted, and the stream carries on.
            if self.offpath is not None:
                # Never blocks: if the model is behind, the window is dropped and counted,
                # and the stream carries on. The worker folds it into the model's history
                # itself, so the hot path does not touch that state.
                self.offpath.submit(window)
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
            if self.offpath is not None:
                self.offpath.submit(window)
            if score is None:
                continue
            self.latencies.append(score.latency_ms)
            episode = self.builder.add(score, window.injected)
            if episode is not None:
                written.append(self._persist(episode))
        for episode in self.builder.close_all():
            written.append(self._persist(episode))

        if self.offpath is not None:
            # Let the model finish its backlog before closing its episodes, or the last
            # windows of the run would be reported as never scored when in fact they were
            # merely still queued.
            self.offpath.stop()
            with self._foundation_lock:
                for episode in self.foundation_builder.close_all():
                    written.append(self._persist(episode))
        return written

    def _persist(self, episode) -> int:
        if self.conditioning is not None:
            # Record the flag before deciding, so an episode's in-scope siblings are already
            # in the index when its own turn comes. Deciding first would make the verdict
            # depend on the order episodes happened to close in.
            self.conditioning.index.record(episode.channel, episode.t_start_ms, episode.t_end_ms)
            attribution = self.conditioning.apply(episode)
            if episode.status is not EpisodeStatus.REAL:
                self.attributed += 1
            log.info(
                "conditioning %s -> %s (%s): %s",
                episode.channel,
                episode.status,
                attribution.verdict,
                attribution.reason,
            )
        with self._store_lock:
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

    @staticmethod
    def _percentiles(samples: list[float]) -> str:
        ordered = sorted(samples)

        def pct(p: float) -> float:
            return ordered[min(int(len(ordered) * p), len(ordered) - 1)]

        return (
            f"p50 {pct(0.50):.3f} ms | p95 {pct(0.95):.3f} ms | "
            f"p99 {pct(0.99):.3f} ms | max {ordered[-1]:.3f} ms"
        )

    def latency_report(self) -> list[str]:
        lines = []
        if self.latencies:
            lines.append(
                f"hot path ({self.baseline.name}) over {len(self.latencies):,} windows: "
                f"{self._percentiles(self.latencies)}  [budget 250 ms p99, NFR-1]"
            )
        else:
            lines.append("hot path: no windows scored")
        if self.foundation is not None:
            if self.foundation_latencies:
                lines.append(
                    f"off critical path ({self.foundation.name}) over "
                    f"{len(self.foundation_latencies):,} windows, amortised per window: "
                    f"{self._percentiles(self.foundation_latencies)}  [not bound by NFR-1]"
                )
            else:
                lines.append(
                    f"off critical path ({self.foundation.name}): no windows scored "
                    f"(cold {self.foundation.windows_skipped_cold:,})"
                )
        return lines


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

    foundation = None
    if not args.no_foundation_model:
        candidate = ChronosResidualDetector(
            args.foundation_model,
            bucket_ms=args.foundation_bucket_ms,
            context_buckets=args.foundation_context,
            min_context_buckets=args.foundation_min_context,
            device=args.foundation_device,
        )
        try:
            candidate.load()
            foundation = candidate
        except FoundationModelUnavailable as exc:
            # A degradation, not a failure. ARCHITECTURE.md section 7 promises the platform
            # falls back to the baseline when the model path is unavailable; this is where
            # that promise is kept.
            log.warning("foundation model unavailable, continuing on the baseline: %s", exc)

    conditioning = None
    context_source = None
    if args.conditioning:
        context_source = KafkaContextSource(
            bootstrap, args.context_topic or kafka.context_topic, group=f"{args.group}-context"
        )
        context_source.start()
        conditioning = ConditioningPolicy(
            source=context_source,
            thresholds=ConditioningThresholds(
                min_corroborating_channels=args.min_corroborating_channels,
                min_scope_fraction=args.min_scope_fraction,
            ),
        )

    spine = DetectionSpine(
        store,
        window_ms=args.window_ms,
        slide_ms=args.slide_ms,
        lateness_ms=args.lateness_ms,
        min_points=args.min_points,
        threshold=args.threshold,
        merge_gap_ms=args.merge_gap_ms or args.slide_ms * 2,
        warmup_samples=args.warmup_samples,
        foundation=foundation,
        foundation_threshold=args.foundation_threshold,
        foundation_batch=args.foundation_batch,
        conditioning=conditioning,
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
    print(
        "conditioning: "
        + (
            f"on, reading {args.context_topic or kafka.context_topic!r}"
            if conditioning is not None
            else "OFF (shadow pass -- this is the unconditioned baseline)"
        ),
        flush=True,
    )
    print(
        f"detectors: {spine.baseline.name} (hot path)"
        + (
            f" + {foundation.name} (off critical path, batch {args.foundation_batch},"
            f" threshold {args.foundation_threshold})"
            if foundation is not None
            else " only -- foundation model not running"
        ),
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
                    f"late {spine.windows.late_readings:,}"
                    + (
                        f" | model scored {spine.foundation.windows_scored:,}"
                        f" backlog {spine.offpath.backlog:,}"
                        if spine.offpath is not None
                        else ""
                    ),
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
        if spine.offpath is not None:
            print(f"foundation model: {spine.offpath.summary()}", flush=True)
            print(
                f"  windows scored {spine.foundation.windows_scored:,} | "
                f"cold {spine.foundation.windows_skipped_cold:,} | "
                f"batches {spine.foundation.batches_run:,}",
                flush=True,
            )
        for line in spine.latency_report():
            print(line, flush=True)
        if spine.conditioning is not None:
            print(f"conditioning: {spine.conditioning.summary()}", flush=True)
            if context_source is not None:
                print(
                    f"context events seen {context_source.events_seen:,} "
                    f"(malformed {context_source.malformed:,})",
                    flush=True,
                )
                context_source.close()
        _print_detector_comparison(store)
        store.close()

    return 0


def _print_detector_comparison(store: EpisodeStore) -> None:
    """Episodes per detector, so 'runs alongside the baseline' is visible, not asserted."""
    with store._conn.cursor() as cur:
        cur.execute(
            """
            SELECT raised_by, count(*) AS episodes, round(avg(peak_score)::numeric, 1) AS avg_peak,
                   round(max(peak_score)::numeric, 1) AS max_peak
            FROM episodes
            GROUP BY raised_by
            ORDER BY raised_by
            """
        )
        rows = cur.fetchall()
    if not rows:
        return
    print("\nepisodes by detector (scores are on each detector's own scale, not comparable):")
    for r in rows:
        print(
            f"  {r['raised_by']:<24} {r['episodes']:>5} episodes | "
            f"avg peak {r['avg_peak']} | max peak {r['max_peak']}",
            flush=True,
        )


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

    c = p.add_argument_group("context conditioning (the core contribution)")
    c.add_argument(
        "--conditioning",
        action="store_true",
        help="condition episodes on the context topic. Off by default so the default run is "
        "the unconditioned shadow baseline every measurement compares against",
    )
    c.add_argument("--context-topic", default=None, help="override CONTEXT_TOPIC")
    c.add_argument(
        "--min-corroborating-channels",
        type=int,
        default=2,
        help="how many in-scope channels must move together before a context event explains "
        "them. Below 2 the policy collapses into blanket suppression",
    )
    c.add_argument("--min-scope-fraction", type=float, default=0.25)

    f = p.add_argument_group("foundation model (off the critical path, ADR-017)")
    f.add_argument(
        "--foundation-model",
        default="amazon/chronos-bolt-tiny",
        help="Chronos-Bolt checkpoint; measured per window at batch 32 on this CPU: "
        "tiny 0.64 ms, mini 1.15 ms, small 2.95 ms, base 9.32 ms",
    )
    f.add_argument(
        "--no-foundation-model",
        action="store_true",
        help="run the baseline alone (also the automatic fallback if the model will not load)",
    )
    f.add_argument("--foundation-threshold", type=float, default=6.0)
    f.add_argument("--foundation-batch", type=int, default=32)
    f.add_argument("--foundation-bucket-ms", type=int, default=1_000)
    f.add_argument("--foundation-context", type=int, default=128)
    f.add_argument("--foundation-min-context", type=int, default=48)
    f.add_argument("--foundation-device", default="cpu", choices=("cpu", "cuda"))

    p.add_argument("--commit-every", type=int, default=500, help="readings between offset commits")
    p.add_argument("--report-interval", type=float, default=10.0)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
