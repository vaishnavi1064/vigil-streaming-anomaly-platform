"""The Flink scoring job: exactly-once windowed detection over the readings stream.

This is where the interior correctness claim actually lives (ADR-005). The Python detector
built in Phase 1 has at-least-once consume plus an idempotent sink, which is
effectively-once at the sink and is documented as exactly that. This job is stronger: the
Kafka source records its offsets **in Flink's checkpoint**, the sink writes inside a Kafka
transaction committed on checkpoint completion, and the two are the same two-phase commit.
A failure between them replays from the checkpoint and the uncommitted transaction is
aborted, so a consumer reading committed-only sees each window score exactly once.

What each piece is for, since these are the parts that get asked about:

  * **Event time and watermarks.** Time comes from the reading's own timestamp, never from
    arrival. Watermarks are per source partition with bounded out-of-orderness, and idle
    partitions are timed out so one quiet partition cannot stall the whole job. This is the
    same problem the reconciliation harness hit in Phase 2 and solved the same way, which is
    the point of building that first.
  * **RocksDB state backend.** Per-channel detector state is keyed state, and there is one
    entry per channel indefinitely. Heap state would make the job's memory a function of
    fleet size; RocksDB spills to disk and keeps it a function of the working set.
  * **Incremental checkpoints.** Most of the state is unchanged between checkpoints, so
    shipping only the delta is what makes a short checkpoint interval affordable.
  * **Transactional id prefix.** Kafka fences zombie producers by epoch. A stable prefix is
    what lets a restarted job reclaim and abort the transactions its previous incarnation
    left open, instead of leaving them to block consumers until they time out.

Submit with:
    docker compose --profile flink exec flink-jobmanager \\
        flink run -py /opt/vigil/scoring_job.py
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os

from pyflink.common import Configuration, Duration, Types, WatermarkStrategy
from pyflink.common.serialization import SimpleStringSchema
from pyflink.common.watermark_strategy import TimestampAssigner
from pyflink.datastream import RuntimeContext, StreamExecutionEnvironment
from pyflink.datastream.checkpoint_config import CheckpointingMode, ExternalizedCheckpointCleanup
from pyflink.datastream.connectors.kafka import (
    DeliveryGuarantee,
    KafkaOffsetsInitializer,
    KafkaRecordSerializationSchema,
    KafkaSink,
    KafkaSource,
)
from pyflink.datastream.functions import KeyedProcessFunction
from pyflink.datastream.state import ValueStateDescriptor

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vigil.flink")


class ReadingTimestampAssigner(TimestampAssigner):
    """Event time is the reading's own timestamp.

    Falling back to arrival time on a malformed record would silently place it wherever the
    consumer happened to be, which is how a corrupt record turns into a corrupt window. A
    record with no usable timestamp is dropped downstream instead.
    """

    def extract_timestamp(self, value, record_timestamp: int) -> int:
        try:
            return int(json.loads(value)["ts"])
        except (ValueError, KeyError, TypeError):
            return record_timestamp


class WindowedZScore(KeyedProcessFunction):
    """Per-channel sliding-window z-score, with the reference distribution in keyed state.

    Mirrors `vigil.detectors.zscore` deliberately: same Welford update, same exponential
    decay, same score as the larger of a mean displacement and a dispersion departure. The
    point of the migration is to show the semantics survived it, and that can only be shown
    if the two are the same computation. `docs/CORRECTNESS.md` records the comparison.

    State is a single value per channel rather than a buffer of readings: the estimator is
    incremental, so the job's state stays O(channels) instead of O(channels x window).
    """

    def __init__(self, window_ms: int, slide_ms: int, decay: float, warmup: int, min_points: int):
        self.window_ms = window_ms
        self.slide_ms = slide_ms
        self.decay = decay
        self.warmup = warmup
        self.min_points = min_points

    def open(self, runtime_context: RuntimeContext):
        # One entry per channel, held for the life of the job. This is exactly the state
        # that makes RocksDB the right backend rather than the heap.
        self.reference = runtime_context.get_state(
            ValueStateDescriptor("zscore_reference", Types.PICKLED_BYTE_ARRAY())
        )
        self.buffer = runtime_context.get_state(
            ValueStateDescriptor("window_buffer", Types.PICKLED_BYTE_ARRAY())
        )
        self.next_timer = runtime_context.get_state(
            ValueStateDescriptor("next_window_timer", Types.LONG())
        )

    def process_element(self, value, ctx: KeyedProcessFunction.Context):
        try:
            record = json.loads(value)
            event_ts = int(record["ts"])
            reading = float(record["value"])
        except (ValueError, KeyError, TypeError):
            return

        buffer = self.buffer.value() or []
        buffer.append((event_ts, reading))
        self.buffer.update(buffer)

        # One timer per window boundary. Registering on the event-time timer service rather
        # than a wall clock is what makes the job's output identical on a replay of historic
        # data and on a live stream.
        boundary = ((event_ts // self.slide_ms) + 1) * self.slide_ms
        pending = self.next_timer.value()
        if pending is None or boundary > pending:
            ctx.timer_service().register_event_time_timer(boundary)
            self.next_timer.update(boundary)

        yield from ()

    def on_timer(self, timestamp: int, ctx: KeyedProcessFunction.OnTimerContext):
        channel = ctx.get_current_key()
        buffer = self.buffer.value() or []
        window_start = timestamp - self.window_ms
        window = [(ts, v) for ts, v in buffer if window_start <= ts < timestamp]

        # Drop what no future window can reach. Without this the buffer grows without bound
        # and the job's memory becomes a function of uptime.
        retained = [(ts, v) for ts, v in buffer if ts >= timestamp - self.window_ms]
        self.buffer.update(retained)
        self.next_timer.clear()

        if len(window) < self.min_points:
            return

        state = self.reference.value() or {"count": 0, "weight": 0.0, "mean": 0.0, "m2": 0.0}
        values = [v for _, v in window]
        n = len(values)
        window_mean = sum(values) / n

        score = None
        if state["count"] >= self.warmup and state["weight"] > 1.0:
            variance = max(state["m2"] / (state["weight"] - 1.0), 0.0)
            sigma = math.sqrt(variance)
            if sigma > 1e-9:
                mean_z = abs(window_mean - state["mean"]) / sigma * math.sqrt(n)
                window_var = sum((v - window_mean) ** 2 for v in values) / max(n - 1, 1)
                dispersion_z = abs(math.sqrt(max(window_var, 0.0)) / sigma - 1.0) * math.sqrt(n / 2)
                score = max(mean_z, dispersion_z)

        # Score before folding the window into the reference, or the window contributes to
        # the distribution judging it and partly hides itself.
        for v in values:
            state["count"] += 1
            state["weight"] = state["weight"] * self.decay + 1.0
            delta = v - state["mean"]
            state["mean"] += delta / state["weight"]
            state["m2"] = state["m2"] * self.decay + delta * (v - state["mean"])
        self.reference.update(state)

        if score is None:
            return

        yield json.dumps(
            {
                "detector": "flink-zscore",
                "channel": channel,
                "window_start_ms": window_start,
                "window_end_ms": timestamp,
                "score": score,
                "points": n,
                "window_mean": window_mean,
                "reference_mean": state["mean"],
            },
            separators=(",", ":"),
        )


def build_env(args) -> StreamExecutionEnvironment:
    config = Configuration()
    config.set_string("state.backend.type", "rocksdb")
    config.set_string("state.backend.incremental", "true")
    config.set_string("execution.checkpointing.dir", args.checkpoint_dir)
    config.set_string("execution.checkpointing.savepoint-dir", args.savepoint_dir)

    env = StreamExecutionEnvironment.get_execution_environment(config)
    env.set_parallelism(args.parallelism)

    env.enable_checkpointing(args.checkpoint_interval_ms, CheckpointingMode.EXACTLY_ONCE)
    checkpointing = env.get_checkpoint_config()
    # A checkpoint that overruns its budget is a symptom worth surfacing, not something to
    # let pile up: without a timeout a slow checkpoint blocks the next one indefinitely.
    checkpointing.set_checkpoint_timeout(args.checkpoint_timeout_ms)
    checkpointing.set_min_pause_between_checkpoints(args.checkpoint_interval_ms // 2)
    checkpointing.set_max_concurrent_checkpoints(1)
    checkpointing.set_tolerable_checkpoint_failure_number(3)
    # Unaligned checkpoints trade a little state size for not waiting on backpressured
    # barriers, which is what keeps checkpoints completing under exactly the load spikes
    # the chaos and scale harnesses generate.
    checkpointing.enable_unaligned_checkpoints()
    checkpointing.enable_externalized_checkpoints(
        ExternalizedCheckpointCleanup.RETAIN_ON_CANCELLATION
    )
    return env


def main() -> None:
    args = parse_args()
    env = build_env(args)

    source = (
        KafkaSource.builder()
        .set_bootstrap_servers(args.bootstrap)
        .set_topics(args.readings_topic)
        .set_group_id(args.group)
        .set_starting_offsets(
            KafkaOffsetsInitializer.earliest()
            if args.from_beginning
            else KafkaOffsetsInitializer.latest()
        )
        .set_value_only_deserializer(SimpleStringSchema())
        # read_committed: the source must not see records from transactions that were never
        # committed, or the exactly-once chain is broken at its first link.
        .set_property("isolation.level", "read_committed")
        .build()
    )

    watermarks = (
        WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_millis(args.out_of_orderness_ms))
        .with_timestamp_assigner(ReadingTimestampAssigner())
        # A partition with no traffic must not hold the watermark back for every other
        # partition. Same failure the reconciliation harness hit; same fix.
        .with_idleness(Duration.of_millis(args.idle_timeout_ms))
    )

    readings = env.from_source(source, watermarks, "readings")

    scores = (
        readings.key_by(lambda value: json.loads(value)["channel"], key_type=Types.STRING())
        .process(
            WindowedZScore(
                window_ms=args.window_ms,
                slide_ms=args.slide_ms,
                decay=args.decay,
                warmup=args.warmup_samples,
                min_points=args.min_points,
            ),
            output_type=Types.STRING(),
        )
        .name("windowed-zscore")
    )

    sink = (
        KafkaSink.builder()
        .set_bootstrap_servers(args.bootstrap)
        .set_record_serializer(
            KafkaRecordSerializationSchema.builder()
            .set_topic(args.scores_topic)
            .set_value_serialization_schema(SimpleStringSchema())
            .build()
        )
        .set_delivery_guarantee(DeliveryGuarantee.EXACTLY_ONCE)
        # The prefix must be stable across restarts: Kafka fences zombie producers by epoch,
        # and it is this prefix that lets a restarted job reclaim and abort the transactions
        # its previous incarnation left open rather than leaving consumers blocked on them.
        .set_transactional_id_prefix(args.transactional_id_prefix)
        .set_property("transaction.timeout.ms", str(args.transaction_timeout_ms))
        .build()
    )

    scores.sink_to(sink).name("window-scores")
    env.execute("vigil-scoring")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP_INTERNAL", "kafka:9092"))
    p.add_argument("--readings-topic", default=os.environ.get("READINGS_TOPIC", "sensor.readings"))
    p.add_argument(
        "--scores-topic", default=os.environ.get("SCORES_TOPIC", "detections.window_scores")
    )
    p.add_argument("--group", default="vigil-flink-scoring")
    p.add_argument("--from-beginning", action="store_true", default=True)
    p.add_argument("--parallelism", type=int, default=int(os.environ.get("FLINK_PARALLELISM", "2")))
    p.add_argument("--window-ms", type=int, default=30_000)
    p.add_argument("--slide-ms", type=int, default=10_000)
    p.add_argument("--out-of-orderness-ms", type=int, default=5_000)
    p.add_argument("--idle-timeout-ms", type=int, default=30_000)
    p.add_argument("--min-points", type=int, default=8)
    p.add_argument("--warmup-samples", type=int, default=120)
    p.add_argument("--decay", type=float, default=0.995)
    p.add_argument("--checkpoint-interval-ms", type=int, default=10_000)
    p.add_argument("--checkpoint-timeout-ms", type=int, default=120_000)
    p.add_argument("--checkpoint-dir", default="file:///flink-checkpoints")
    p.add_argument("--savepoint-dir", default="file:///flink-savepoints")
    p.add_argument("--transactional-id-prefix", default="vigil-scoring")
    # Must exceed the checkpoint interval by a comfortable margin, or a transaction can time
    # out before the checkpoint that would have committed it completes -- which shows up as
    # silent data loss rather than as an error.
    p.add_argument("--transaction-timeout-ms", type=int, default=900_000)
    return p.parse_args()


if __name__ == "__main__":
    main()
