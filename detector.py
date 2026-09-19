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
from collections import Counter, deque

from confluent_kafka import Consumer, KafkaError, TopicPartition

from vigil.conditioning.barrier import CorroborationBarrier
from vigil.conditioning.policy import (
    ConditioningPolicy,
    ConditioningThresholds,
    FlaggedWindowIndex,
)
from vigil.conditioning.second_opinion import SecondOpinionIndex
from vigil.conditioning.signals import KafkaContextSource
from vigil.detectors.foundation import ChronosResidualDetector, FoundationModelUnavailable
from vigil.detectors.offpath import OffPathScorer
from vigil.detectors.zscore import RollingZScoreDetector
from vigil.episodes import EpisodeBuilder, EpisodeStatus
from vigil.explain import ExplanationRequest, ExplanationWorker, window_explainer_from_env
from vigil.readings import Reading
from vigil.settings import KafkaSettings, PostgresSettings
from vigil.store import EpisodeStore
from vigil.topology import FleetTopology
from vigil.windows import SlidingWindowAssigner

log = logging.getLogger("vigil.detector")


def _attach_explanation(store, episode_id: int, result) -> None:
    """Write an explanation from the worker thread. Absence is recorded, not silent.

    A failed or skipped explanation writes nothing rather than writing its reason into the
    episode: `explanation` is what an operator reads, and filling it with "no API key" would
    put an infrastructure note where a description of the anomaly belongs. The reason lives
    in the log and in the explainer's counters.
    """
    if not result.available:
        log.info("episode %s carries no explanation: %s", episode_id, result.reason)
        return
    try:
        store.attach_explanation(episode_id, result.text)
    except Exception:  # noqa: BLE001 - an explanation is never worth failing a run over
        log.exception("could not attach explanation to episode %s", episode_id)


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
        second_opinion: SecondOpinionIndex | None = None,
        verdict_buffer_ms: int = 30_000,
        watermark_idle_ms: int = 60_000,
        explanation_worker: ExplanationWorker | None = None,
        explain_history_s: float = 300.0,
    ) -> None:
        self.store = store
        self.windows = SlidingWindowAssigner(
            size_ms=window_ms,
            slide_ms=slide_ms,
            allowed_lateness_ms=lateness_ms,
            min_points=min_points,
        )
        self.baseline = RollingZScoreDetector(warmup_samples=warmup_samples)
        self.builder = EpisodeBuilder(
            threshold=threshold, merge_gap_ms=merge_gap_ms, on_open=self._on_episode_opened
        )
        self.readings_seen = 0
        self.episodes_written = 0
        self.origin_counts: Counter[str] = Counter()
        self.latencies: list[float] = []
        # None means the shadow pass: detection runs unconditioned, which is the baseline
        # every conditioned result is measured against (ADR-016).
        self.conditioning = conditioning
        self.attributed = 0
        # The verdict waits behind an event-time barrier so the corroboration index holds
        # every sibling that could exonerate the episode, not merely the ones that happened
        # to close first (G-7). Absent on the shadow pass, which decides nothing.
        self.barrier = (
            CorroborationBarrier(buffer_ms=verdict_buffer_ms, idle_ms=watermark_idle_ms)
            if conditioning is not None
            else None
        )
        self.watermark_idle_ms = watermark_idle_ms
        # Both the main loop and the off-path worker open and close episodes, and the
        # corroboration index and the barrier are plain Python structures.
        self._conditioning_lock = threading.Lock()

        # Per-channel sample history, kept only when there is somewhere to send it. An
        # explainer that is not configured must not cost the hot path a deque per channel:
        # the picture is for the rare path, and the rare path is off.
        self.explanation_worker = explanation_worker
        self.explain_history_s = explain_history_s
        self._history: dict[str, deque[tuple[int, float]]] = {}

        # The foundation model keeps its own episode builder rather than annotating the
        # baseline's. The two detectors score on different scales and disagree, and the
        # whole point of running them side by side is to see where -- folding the model's
        # opinion into episodes the baseline chose would discard exactly that comparison.
        #
        # Unless it is here as a corroborating second opinion (ADR-050), in which case it
        # raises nothing at all: its scores go to the index the policy reads and the
        # baseline stays the only detector that pages anyone, so the episode population
        # stays identical to the shadow pass and the two remain comparable.
        self.foundation = foundation
        self.second_opinion = second_opinion
        self.foundation_builder = (
            EpisodeBuilder(
                threshold=foundation_threshold,
                merge_gap_ms=merge_gap_ms,
                on_open=self._on_episode_opened,
            )
            if foundation is not None and second_opinion is None
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

    def _on_episode_opened(self, episode) -> None:
        """Record the departure the moment it is known, not when the episode ends.

        The corroboration index answers "which channels moved at this instant". Filling it
        at episode close made the answer depend on how long each episode happened to run:
        a two-minute excursion is evidence about its first second, and it was arriving two
        minutes late. This is the index half of G-7; the barrier is the other half.
        """
        if self.conditioning is None:
            return
        with self._conditioning_lock:
            self.conditioning.index.record(episode.channel, episode.began_ms, episode.t_end_ms)

    def _release_ready(self) -> list[int]:
        """Decide every held episode whose sibling evidence has now closed in event time."""
        if self.barrier is None or not self.barrier.pending:
            # Checked once per reading, so the empty case has to be free. Reading the
            # length outside the lock is safe: a hold racing this call is released by the
            # next reading, and there is always a next reading or a drain.
            return []
        with self._conditioning_lock:
            due = self.barrier.release(self._evidence_watermark_ms())
        return [self._decide_and_store(episode) for episode in due]

    def _evidence_watermark_ms(self) -> int | None:
        """Event time through which every piece of evidence a verdict needs has arrived.

        Call with `_conditioning_lock` held: the second detector's progress is written
        from the off-path worker thread.

        The fleet watermark alone answers for the other channels (ADR-037). With a second
        detector in the policy there is a second source to wait for, and deciding ahead of
        it would read "has not scored this yet" as "found nothing" -- the same mistake
        G-7 was, one source along. So the barrier releases on the minimum of the two, and
        the ablation pass waits on the same minimum even though it ignores what it waited
        for, which is what keeps the two passes differing in the decision alone.
        """
        fleet = self.windows.fleet_watermark_ms(self.watermark_idle_ms)
        if self.second_opinion is None:
            return fleet
        model = self.second_opinion.progress_ms(self.watermark_idle_ms)
        if fleet is None or model is None:
            return None
        return min(fleet, model)

    def _flush_barrier(self) -> list[int]:
        if self.barrier is None:
            return []
        with self._conditioning_lock:
            due = self.barrier.flush()
        return [self._decide_and_store(episode) for episode in due]

    def _on_foundation_scores(self, scores) -> None:
        """Called from the off-path worker thread; must stay cheap and thread-safe."""
        if self.second_opinion is not None:
            with self._conditioning_lock:
                for score in scores:
                    self.foundation_latencies.append(score.latency_ms)
                    self.second_opinion.record(score)
            return
        with self._foundation_lock:
            for score in scores:
                self.foundation_latencies.append(score.latency_ms)
                episode = self.foundation_builder.add(score)
                if episode is not None:
                    self._persist(episode)

    def consume(self, reading: Reading) -> list[int]:
        """Feed one reading; returns the ids of any episodes this closed and persisted."""
        self.readings_seen += 1
        if self.explanation_worker is not None:
            self._remember(reading)
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
                written.extend(self._persist(episode))
        written.extend(self._release_ready())
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
                written.extend(self._persist(episode))
        for episode in self.builder.close_all():
            written.extend(self._persist(episode))

        if self.offpath is not None:
            # Let the model finish its backlog before closing its episodes, or the last
            # windows of the run would be reported as never scored when in fact they were
            # merely still queued. As a second opinion it closes no episodes, but the same
            # wait matters more there: the verdicts still held are about to be taken, and
            # they are entitled to every score the model has left in the queue.
            self.offpath.stop()
            if self.foundation_builder is not None:
                with self._foundation_lock:
                    for episode in self.foundation_builder.close_all():
                        written.extend(self._persist(episode))

        # The stream is over, so no further watermark will ever arrive to release what the
        # barrier is still holding. Those episodes are decided on the evidence that exists,
        # which is now all there will ever be.
        written.extend(self._release_ready())
        written.extend(self._flush_barrier())
        return written

    def _remember(self, reading: Reading) -> None:
        """Keep a bounded window of recent samples per channel, for the plot.

        Bounded by *time* rather than by count, because channels report at different rates
        and a fixed-length deque would give a fast channel ten seconds of context and a slow
        one an hour.
        """
        history = self._history.get(reading.channel)
        if history is None:
            history = deque()
            self._history[reading.channel] = history
        history.append((reading.event_ts_ms, reading.value))
        cutoff = reading.event_ts_ms - int(self.explain_history_s * 1000)
        while history and history[0][0] < cutoff:
            history.popleft()

    def _request_explanation(self, episode_id: int, episode) -> None:
        """Hand a flagged episode to the explainer. Never blocks, never raises.

        Only episodes that would actually page someone: an attributed episode has already
        been explained by the context event it was attributed to, and paying for a picture of
        one would be paying to explain something twice.
        """
        if self.explanation_worker is None or episode.status is not EpisodeStatus.REAL:
            return
        history = self._history.get(episode.channel)
        if not history:
            return
        snapshot = list(history)
        self.explanation_worker.submit(
            ExplanationRequest(
                episode_id=episode_id,
                episode=episode,
                timestamps_ms=[t for t, _ in snapshot],
                values=[v for _, v in snapshot],
            )
        )

    def _persist(self, episode) -> list[int]:
        """Route a closed episode: straight to the store, or behind the verdict barrier.

        Unconditioned, an episode is durable as soon as it closes. Conditioned, it waits
        until the fleet watermark has passed its onset by the buffer, because the verdict
        is a statement about several channels and cannot be made from one of them.
        """
        if self.barrier is None:
            return [self._store(episode)]
        with self._conditioning_lock:
            self.barrier.hold(episode, self._evidence_watermark_ms())
        return []

    def _decide_and_store(self, episode) -> int:
        assert self.conditioning is not None
        attribution = self.conditioning.apply(episode)
        if attribution.event is not None:
            # The episode's attributed_to is a foreign key into context_events, so the
            # event has to exist before the episode that points at it. Writing the
            # episode first raises ForeignKeyViolation -- which is the constraint doing
            # its job, and is how this was found.
            with self._store_lock:
                self.store.record_context_event(attribution.event)
        if episode.status is not EpisodeStatus.REAL:
            self.attributed += 1
        log.info(
            "conditioning %s -> %s (%s): %s",
            episode.channel,
            episode.status,
            attribution.verdict,
            attribution.reason,
        )
        return self._store(episode)

    def _store(self, episode) -> int:
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
        # After the episode is durable, never before: an explanation that arrived first
        # would have nothing to attach to, and the write is what makes the episode real.
        self._request_explanation(episode_id, episode)
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
    if args.second_opinion and args.no_foundation_model:
        print(
            "--second-opinion needs the foundation model, and --no-foundation-model turns "
            "it off. Pick one.",
            file=sys.stderr,
        )
        return 2
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
            # that promise is kept -- except when the model *is* the measurement, in which
            # case degrading silently would publish a number for a signal that never ran.
            if args.second_opinion:
                print(f"--second-opinion asked for, model unavailable: {exc}", file=sys.stderr)
                return 2
            log.warning("foundation model unavailable, continuing on the baseline: %s", exc)

    conditioning = None
    context_source = None
    second_opinion = SecondOpinionIndex() if args.second_opinion and foundation else None
    if args.conditioning:
        context_source = KafkaContextSource(
            bootstrap, args.context_topic or kafka.context_topic, group=f"{args.group}-context"
        )
        context_source.start()
        topology = FleetTopology.empty()
        if args.topology:
            topology = FleetTopology.load(args.topology)
            log.info("fleet inventory loaded: %s", topology.summary())
        elif args.require_blast_radius:
            log.warning(
                "no --topology given, so the blast-radius test cannot run and conditioning "
                "falls back to timing and scope alone"
            )
        conditioning = ConditioningPolicy(
            source=context_source,
            index=FlaggedWindowIndex(synchrony_ms=args.synchrony_ms),
            topology=topology,
            second_opinion=second_opinion,
            thresholds=ConditioningThresholds(
                min_corroborating_channels=args.min_corroborating_channels,
                min_scope_fraction=args.min_scope_fraction,
                synchrony_ms=args.synchrony_ms,
                require_blast_radius=args.require_blast_radius,
                min_blast_nodes=args.min_blast_nodes,
                use_second_opinion=second_opinion is not None and not args.second_opinion_advisory,
                second_opinion_agrees_at=args.second_opinion_agrees_at,
                second_opinion_protects_attributed=args.second_opinion_protects_attributed,
            ),
        )

    # The explainer is built whether or not it is configured, so its counters report how
    # many flagged windows *would* have been explained. Off entirely with --no-explain.
    explanation_worker = None
    if not args.no_explain:
        explainer = window_explainer_from_env()
        if explainer.available:
            explanation_worker = ExplanationWorker(
                explainer,
                lambda episode_id, result: _attach_explanation(store, episode_id, result),
                max_pending=args.explain_queue,
            )
            explanation_worker.start()
            log.info("explainer configured against %s", explainer.model_name)
        else:
            log.info(
                "explainer not configured (set ANTHROPIC_API_KEY, or all of "
                "VLM_ENDPOINT / VLM_API_KEY / VLM_MODEL); episodes will carry no "
                "explanation and detection is unaffected"
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
        second_opinion=second_opinion,
        verdict_buffer_ms=args.verdict_buffer_ms,
        watermark_idle_ms=args.watermark_idle_ms,
        explanation_worker=explanation_worker,
        explain_history_s=args.explain_history_s,
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
            f"on, reading {args.context_topic or kafka.context_topic!r}, "
            f"verdicts held {args.verdict_buffer_ms / 1000:g}s of event time behind the "
            f"fleet watermark"
            if conditioning is not None
            else "OFF (shadow pass -- this is the unconditioned baseline)"
        ),
        flush=True,
    )
    print(
        f"detectors: {spine.baseline.name} (hot path)"
        + (
            f" + {foundation.name} (off critical path, batch {args.foundation_batch}, "
            + (
                f"second opinion only, agreement at {args.second_opinion_agrees_at:g}"
                + (", advisory" if args.second_opinion_advisory else "")
                if second_opinion is not None
                else f"threshold {args.foundation_threshold}"
            )
            + ")"
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
            if spine.barrier is not None:
                print(f"  {spine.barrier.hold_report()}", flush=True)
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
    c.add_argument(
        "--synchrony-ms",
        type=int,
        default=5_000,
        help="how close together in-scope channels must move to count as moving together. "
        "A deploy artifact blips its channels at the same instant; independent faults in "
        "the same window are not synchronised",
    )
    c.add_argument(
        "--verdict-buffer-ms",
        type=int,
        default=30_000,
        help="event-time delay between an episode closing and its verdict, so every "
        "in-scope sibling that could exonerate it has closed first (G-7). Costs detection "
        "latency and buys evidence; 0 restores the racing behaviour v1-v3 measured",
    )
    c.add_argument(
        "--topology",
        default=None,
        help="fleet inventory: channel -> node, rack, deploy ring. Operational fact from "
        "the CMDB, never ground truth about anomalies. Without it the blast-radius test "
        "cannot run and conditioning falls back to timing and scope",
    )
    c.add_argument(
        "--no-blast-radius",
        dest="require_blast_radius",
        action="store_false",
        help="skip the topology test, leaving the timing-only policy v1-v3 measured. The "
        "only way those results stay reproducible",
    )
    c.add_argument(
        "--min-blast-nodes",
        type=int,
        default=2,
        help="how many machines the channels that moved must span before a rollout can "
        "explain them. Below 2 the excursion is confined to one failure domain, which is "
        "what a machine failing looks like",
    )
    c.add_argument(
        "--second-opinion",
        action="store_true",
        help="let the foundation model corroborate or contradict episodes the baseline "
        "raised (ADR-050). It raises no episodes of its own in this role, so the episode "
        "population is unchanged and the run stays comparable to the shadow pass. The "
        "only conditioning signal that needs no context event, and therefore the only one "
        "that can answer a false page nothing in the context topic explains (B-6)",
    )
    c.add_argument(
        "--second-opinion-advisory",
        action="store_true",
        help="run the second detector, wait for it, record what it said, and let it change "
        "nothing. The ablation: identical timing and identical evidence to the pass that "
        "uses it, differing in the decision alone",
    )
    c.add_argument(
        "--second-opinion-agrees-at",
        type=float,
        default=3.0,
        help="the corroborating detector's score above which it counts as having seen the "
        "same excursion. Deliberately below its own alarm threshold (6.0): the question is "
        "corroboration, not independent detection, and the generous setting is the one that "
        "protects recall and costs false-positive reduction",
    )
    c.add_argument(
        "--no-second-opinion-veto",
        dest="second_opinion_protects_attributed",
        action="store_false",
        help="let a context event attribute an episode even when both detectors saw it. "
        "On by default the other way round: agreement outranks an explanation",
    )
    c.add_argument(
        "--watermark-idle-ms",
        type=int,
        default=60_000,
        help="how far behind the fastest channel a channel may fall before the fleet "
        "watermark stops waiting for it (ADR-019). 0 waits for every channel forever",
    )

    e = p.add_argument_group("explanation (rare path, ADR-004)")
    e.add_argument(
        "--no-explain",
        action="store_true",
        help="do not build the explainer at all. Without it the explainer still only runs "
        "when ANTHROPIC_API_KEY, or all of VLM_ENDPOINT / VLM_API_KEY / VLM_MODEL, is set",
    )
    e.add_argument(
        "--explain-history-s",
        type=float,
        default=300.0,
        help="how much history to plot around a flagged window, in seconds",
    )
    e.add_argument(
        "--explain-queue",
        type=int,
        default=32,
        help="pending explanations before new ones are dropped and counted",
    )

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
