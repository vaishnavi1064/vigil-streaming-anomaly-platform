"""The scale harness: measure throughput against parallelism and find the plateau.

Sweeps the number of consumer processes in one group against the topic's partitions, and
records sustained end-to-end throughput at each point. Produces the curve NFR-5 asks for,
and -- more usefully -- names where it stops scaling and why.

Two rules this obeys, because breaking either produces a curve that looks better than the
system is:

  * **Pre-fill the topic, then measure the drain.** Measuring producer and consumers running
    against each other measures whichever is slower, which at low parallelism is the
    consumers and at high parallelism is the producer. Draining a fixed backlog measures the
    consumers alone, which is the thing being swept.
  * **The same backlog every time.** Each sweep point replays the identical pre-filled topic
    from offset zero in a fresh consumer group, so the points differ only in parallelism.

Consumer count cannot usefully exceed the partition count: Kafka assigns whole partitions,
so extra consumers sit idle. The sweep deliberately runs past that point so the plateau is
measured rather than assumed.

    python scale.py --fill 400000 --parallelism 1,2,3,4,6,8
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from vigil.settings import KafkaSettings

REPO = Path(__file__).resolve().parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


@dataclass
class SweepPoint:
    consumers: int
    partitions: int
    readings: int
    elapsed_s: float
    throughput_per_s: float
    per_consumer_per_s: float
    idle_consumers: int
    speedup_vs_one: float
    efficiency: float

    def line(self) -> str:
        idle = f" ({self.idle_consumers} idle)" if self.idle_consumers else ""
        return (
            f"  {self.consumers:>2} consumer(s){idle:<10} "
            f"{self.throughput_per_s:>10,.0f} readings/s | "
            f"speedup {self.speedup_vs_one:>4.2f}x | efficiency {self.efficiency:>5.0%} | "
            f"{self.per_consumer_per_s:>9,.0f}/s each"
        )


def fill_topic(bootstrap: str, count: int, rate: float, channels: int) -> int:
    """Produce a fixed backlog, unpaced, and return how many records landed.

    Bounded by volume rather than by time. Blast mode has no rate to divide by, and an
    earlier version fell back to a 30-second run there -- which filled 3.9 million readings
    for a `--fill 300000` sweep and quietly measured something other than what was asked for.
    """
    duration = count / rate if rate > 0 else 0
    print(f"filling the topic with ~{count:,} readings...", flush=True)
    proc = subprocess.run(
        [
            str(PYTHON),
            str(REPO / "loadgen.py"),
            "--rate",
            str(rate),
            "--duration",
            str(duration if duration else 0),
            "--max-readings",
            str(count),
            "--channels",
            str(channels),
            "--report-interval",
            "10",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    produced = 0
    for line in proc.stdout.splitlines():
        if line.startswith("delivered "):
            produced = int(line.split()[1].replace(",", ""))
    print(f"topic holds {produced:,} readings", flush=True)
    return produced


def reset_topic(bootstrap: str, topic: str, partitions: int) -> None:
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap})
    for future in admin.delete_topics([topic], operation_timeout=30).values():
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            if "UNKNOWN_TOPIC" not in str(exc).upper():
                print(f"delete topic: {exc}", file=sys.stderr)
    time.sleep(4)
    for future in admin.create_topics(
        [NewTopic(topic, num_partitions=partitions, replication_factor=1)]
    ).values():
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            if "ALREADY_EXISTS" not in str(exc).upper():
                raise
    time.sleep(3)


def drain_with(consumers: int, group: str, idle_s: float) -> tuple[int, float]:
    """Run N consumers in one group over the pre-filled topic; return readings and seconds.

    The consumers run the real detection spine, not a counting stub -- the number wanted is
    what the platform can actually process, not what a loop can read.
    """
    procs = [
        subprocess.Popen(
            [
                str(PYTHON),
                str(REPO / "detector.py"),
                "--from-beginning",
                "--group",
                group,
                "--no-foundation-model",
                "--stop-after-idle-s",
                str(idle_s),
                "--report-interval",
                "300",
            ],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(consumers)
    ]

    started = time.perf_counter()
    outputs = []
    for proc in procs:
        out, _ = proc.communicate(timeout=1800)
        outputs.append(out)
    # Subtract the idle wait: every consumer spends it discovering the topic is drained, and
    # counting it would penalise exactly the fast configurations that finish soonest.
    elapsed = max(time.perf_counter() - started - idle_s, 1e-9)

    total = 0
    for out in outputs:
        for line in out.splitlines():
            if line.startswith("read ") and " readings in " in line:
                total += int(line.split()[1].replace(",", ""))
    return total, elapsed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kafka = KafkaSettings.from_env()
    bootstrap = args.bootstrap or kafka.bootstrap
    topic = kafka.readings_topic
    partitions = args.partitions or kafka.readings_partitions
    levels = [int(x) for x in args.parallelism.split(",")]

    reset_topic(bootstrap, topic, partitions)
    filled = fill_topic(bootstrap, args.fill, args.fill_rate, args.channels)
    if filled == 0:
        print("nothing was produced; aborting", file=sys.stderr)
        return 1

    print(f"\nsweeping consumer parallelism over {partitions} partitions\n", flush=True)
    points: list[SweepPoint] = []
    baseline: float | None = None

    for n in levels:
        stamp = int(time.time())
        readings, elapsed = drain_with(n, f"vigil-scale-{n}-{stamp}", args.idle_s)
        if readings == 0:
            print(f"  {n} consumer(s): read nothing; skipping", file=sys.stderr)
            continue
        throughput = readings / elapsed
        if baseline is None:
            baseline = throughput
        point = SweepPoint(
            consumers=n,
            partitions=partitions,
            readings=readings,
            elapsed_s=round(elapsed, 2),
            throughput_per_s=round(throughput, 1),
            per_consumer_per_s=round(throughput / n, 1),
            idle_consumers=max(0, n - partitions),
            speedup_vs_one=round(throughput / baseline, 3),
            efficiency=round(throughput / baseline / n, 3),
        )
        points.append(point)
        print(point.line(), flush=True)

        if readings != filled:
            print(
                f"       note: read {readings:,} against {filled:,} produced "
                f"(difference {readings - filled:+,})",
                flush=True,
            )

    print(f"\n{'=' * 78}\nthroughput vs. parallelism\n{'=' * 78}", flush=True)
    for p in points:
        bar = "#" * max(1, int(40 * p.throughput_per_s / max(x.throughput_per_s for x in points)))
        print(f"  {p.consumers:>2} | {bar:<40} {p.throughput_per_s:>10,.0f}/s", flush=True)

    if points:
        best = max(points, key=lambda p: p.throughput_per_s)
        print(
            f"\npeak {best.throughput_per_s:,.0f} readings/s at {best.consumers} consumer(s) "
            f"over {partitions} partitions",
            flush=True,
        )
        beyond = [p for p in points if p.consumers > best.consumers]
        if beyond:
            print(
                "beyond the peak, adding consumers does not help: Kafka assigns whole "
                "partitions, so the extra processes are assigned nothing and sit idle.",
                flush=True,
            )

    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(
            json.dumps(
                {
                    "partitions": partitions,
                    "backlog_readings": filled,
                    "points": [asdict(p) for p in points],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"report written to {args.report_json}", flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--fill", type=int, default=300_000, help="backlog size to drain each sweep")
    p.add_argument("--fill-rate", type=float, default=0, help="0 means blast mode")
    p.add_argument("--channels", type=int, default=16)
    p.add_argument("--parallelism", default="1,2,3,4,6,8")
    p.add_argument("--partitions", type=int, default=None, help="override READINGS_PARTITIONS")
    p.add_argument("--idle-s", type=float, default=10, help="idle time that ends a drain")
    p.add_argument("--bootstrap", default=None)
    p.add_argument("--report-json", type=Path, default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
