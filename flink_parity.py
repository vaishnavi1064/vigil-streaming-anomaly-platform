"""Did the Flink migration preserve the detector's semantics?

`tests/test_flink_parity.py` proves the *arithmetic* was transcribed faithfully, by
re-implementing it from the job's source and comparing against `vigil.detectors.zscore` on
identical input. That is necessary and not sufficient: it says nothing about whether Flink
executes it correctly -- whether its windows contain the same readings, whether its
watermarks close them at the same point, whether its keyed state carries the same reference
distribution across a checkpoint.

This closes that gap empirically. It reads the window scores Flink published and the scores
the Python detector produces over the same Kafka topic, joins them on
`(channel, window_start_ms)`, and reports where they disagree.

Exact equality is not expected and demanding it would be wrong. The two differ legitimately
in when they consider a window complete -- Flink's watermark advances per partition through
its own source, the Python windower through its own -- so a window at the very start or end
of a run can be scored by one and not the other, and a channel's reference distribution can
be a few readings ahead in one. What must hold is that they agree on the windows they both
scored, and that neither systematically scores higher.

Read committed-only: the Flink sink writes inside a Kafka transaction, and reading
uncommitted would surrender the guarantee two-phase commit is paying for.

    python flink_parity.py --report-json docs/results/flink-parity.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from confluent_kafka import Consumer, KafkaError

from vigil.detectors.window_scores import committed_only_consumer_config, parse_window_score
from vigil.detectors.zscore import RollingZScoreDetector
from vigil.readings import Reading
from vigil.settings import KafkaSettings
from vigil.windows import SlidingWindowAssigner


def drain(consumer: Consumer, topic: str, idle_s: float, on_message) -> int:
    consumer.subscribe([topic])
    seen = 0
    idle = 0.0
    while idle < idle_s:
        message = consumer.poll(1.0)
        if message is None:
            idle += 1.0
            continue
        if message.error():
            if message.error().code() != KafkaError._PARTITION_EOF:
                print(f"consume error: {message.error()}", file=sys.stderr)
            continue
        idle = 0.0
        on_message(message)
        seen += 1
    return seen


def collect_flink_scores(bootstrap: str, topic: str, idle_s: float) -> dict[tuple[str, int], float]:
    scores: dict[tuple[str, int], float] = {}
    duplicates = 0
    malformed = 0

    def handle(message):
        nonlocal duplicates, malformed
        score = parse_window_score(message.value())
        if score is None:
            malformed += 1
            return
        key = (score.channel, score.window_start_ms)
        if key in scores:
            # Under exactly-once this must not happen. Counted rather than overwritten,
            # because a duplicate here would mean the transactional sink is not delivering
            # what it claims and that is the headline finding, not a detail.
            duplicates += 1
        scores[key] = score.score

    config = committed_only_consumer_config(bootstrap, f"flink-parity-{int(time.time())}", True)
    consumer = Consumer(config)
    try:
        drain(consumer, topic, idle_s, handle)
    finally:
        consumer.close()

    if malformed:
        print(f"malformed score records skipped: {malformed:,}", file=sys.stderr)
    if duplicates:
        print(
            f"WARNING: {duplicates:,} duplicate (channel, window) scores on a topic written "
            f"with exactly-once delivery -- this contradicts the guarantee",
            file=sys.stderr,
        )
    return scores


def collect_python_scores(
    bootstrap: str, topic: str, idle_s: float, args
) -> dict[tuple[str, int], float]:
    assigner = SlidingWindowAssigner(
        size_ms=args.window_ms,
        slide_ms=args.slide_ms,
        allowed_lateness_ms=args.lateness_ms,
        min_points=args.min_points,
    )
    detector = RollingZScoreDetector(warmup_samples=args.warmup_samples)
    scores: dict[tuple[str, int], float] = {}

    def handle(message):
        try:
            reading = Reading.from_json(message.value())
        except (ValueError, KeyError, TypeError):
            return
        for window in assigner.add(reading):
            score = detector.score(window)
            detector.observe(window)
            if score is not None:
                scores[(window.channel, window.start_ms)] = score.score

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"python-parity-{int(time.time())}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "isolation.level": "read_committed",
        }
    )
    try:
        drain(consumer, topic, idle_s, handle)
    finally:
        consumer.close()

    for window in assigner.close_all():
        score = detector.score(window)
        detector.observe(window)
        if score is not None:
            scores[(window.channel, window.start_ms)] = score.score
    return scores


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kafka = KafkaSettings.from_env()
    bootstrap = args.bootstrap or kafka.bootstrap

    print(f"reading Flink scores from {kafka.scores_topic!r} (committed only)...", flush=True)
    flink = collect_flink_scores(bootstrap, args.scores_topic or kafka.scores_topic, args.idle_s)
    print(f"  {len(flink):,} scored windows", flush=True)

    print(f"replaying {kafka.readings_topic!r} through the Python detector...", flush=True)
    python = collect_python_scores(
        bootstrap, args.topic or kafka.readings_topic, args.idle_s, args
    )
    print(f"  {len(python):,} scored windows", flush=True)

    shared = sorted(set(flink) & set(python))
    only_flink = set(flink) - set(python)
    only_python = set(python) - set(flink)

    print(f"\n{'=' * 78}\nFLINK vs PYTHON DETECTOR\n{'=' * 78}", flush=True)
    print(f"windows scored by both      {len(shared):,}", flush=True)
    print(f"windows only Flink scored   {len(only_flink):,}", flush=True)
    print(f"windows only Python scored  {len(only_python):,}", flush=True)

    if not shared:
        print(
            "\nno overlap: nothing can be concluded. Either the job has not been running long "
            "enough, or the two are windowing differently.",
            file=sys.stderr,
        )
        return 1

    diffs = [abs(flink[k] - python[k]) for k in shared]
    rel = [
        abs(flink[k] - python[k]) / max(abs(python[k]), 1e-9)
        for k in shared
        if abs(python[k]) > 1e-6
    ]
    signed = [flink[k] - python[k] for k in shared]
    agree = sum(1 for d, k in zip(diffs, shared, strict=True) if d <= args.tolerance)

    print(f"\nagreement within +/-{args.tolerance}: {agree:,}/{len(shared):,} "
          f"({100 * agree / len(shared):.1f}%)", flush=True)
    print(f"absolute difference   median {statistics.median(diffs):.4f} | "
          f"mean {statistics.fmean(diffs):.4f} | max {max(diffs):.4f}", flush=True)
    if rel:
        print(f"relative difference   median {statistics.median(rel):.2%} | "
              f"mean {statistics.fmean(rel):.2%}", flush=True)
    # A systematic sign would mean one implementation is consistently harsher, which is a
    # semantic divergence rather than the scheduling noise the differences are meant to be.
    bias = statistics.fmean(signed)
    print(f"signed bias (flink - python)  {bias:+.4f}", flush=True)
    if abs(bias) > args.tolerance:
        print(
            "  WARNING: one implementation scores systematically higher; that is not "
            "scheduling noise",
            flush=True,
        )

    worst = sorted(shared, key=lambda k: -abs(flink[k] - python[k]))[:8]
    print("\nlargest disagreements:", flush=True)
    for channel, start in worst:
        print(
            f"  {channel[:40]:<40} @{start}  flink {flink[(channel, start)]:>9.3f}  "
            f"python {python[(channel, start)]:>9.3f}  "
            f"diff {flink[(channel, start)] - python[(channel, start)]:>+9.3f}",
            flush=True,
        )

    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(
            json.dumps(
                {
                    "windows_both": len(shared),
                    "windows_only_flink": len(only_flink),
                    "windows_only_python": len(only_python),
                    "agreement_within_tolerance": agree,
                    "tolerance": args.tolerance,
                    "abs_diff_median": statistics.median(diffs),
                    "abs_diff_max": max(diffs),
                    "signed_bias": bias,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nreport written to {args.report_json}", flush=True)

    return 0 if agree / len(shared) >= args.min_agreement else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--topic", default=None)
    p.add_argument("--scores-topic", default=None)
    p.add_argument("--bootstrap", default=None)
    p.add_argument("--idle-s", type=float, default=12)
    p.add_argument("--window-ms", type=int, default=30_000)
    p.add_argument("--slide-ms", type=int, default=10_000)
    p.add_argument("--lateness-ms", type=int, default=5_000)
    p.add_argument("--min-points", type=int, default=8)
    p.add_argument("--warmup-samples", type=int, default=120)
    p.add_argument("--tolerance", type=float, default=0.5, help="absolute score difference")
    p.add_argument("--min-agreement", type=float, default=0.9)
    p.add_argument("--report-json", type=Path, default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
