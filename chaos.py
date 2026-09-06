"""The chaos suite: inject a fault against live traffic, then prove recovery.

Each scenario runs the same shape:

  1. Start the load generator and let the pipeline settle.
  2. Inject a real fault -- kill a container, freeze it, cut its network, kill the consumer.
  3. Heal it, and time how long until the broker serves again (NFR-7 budget: 60 s).
  4. Stop producing, then run the **reconciliation harness from the beginning** and let its
     identity invariant judge whether the system is consistent.

Step 4 is the point. Recovery is not judged by the system's own opinion of itself: the
verdict comes from a separate process replaying the log and checking that every channel's
sequence is still dense. A pipeline that lost or duplicated records during the fault cannot
hide it, because the sequence numbers were assigned before the fault and are checked after.

The producer's own delivery counters are the second source: an idempotent producer that
reports N delivered has genuinely got N distinct records into the log, so comparing that
against what the harness reads closes the loop from both ends.

    python chaos.py --list
    python chaos.py --fault broker-kill
    python chaos.py --all --report-json docs/results/chaos.json
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vigil.chaos.faults import (
    ALL_FAULTS,
    BrokerKill,
    BrokerPause,
    ConsumerKill,
    Fault,
    NetworkPartition,
    wait_until,
)
from vigil.reconciliation.harness import (
    ReconciliationHarness,
    audit_offsets,
    build_consumer,
    consume_forever,
)
from vigil.reconciliation.ledger import HealthThresholds
from vigil.settings import KafkaSettings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vigil.chaos.run")

REPO = Path(__file__).resolve().parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():  # non-Windows layout
    PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


@dataclass
class ChaosResult:
    fault: str
    description: str
    injected: bool
    # Whether the fault was *observed* to break anything. Injecting a fault that turns out
    # to be a no-op and then reporting a pass is worse than reporting a failure: it is a
    # green result that means nothing. Every scenario has to earn its verdict by showing
    # the system was actually unserviceable at some point.
    disruption_observed: bool
    unhealthy_samples: int
    hold_samples: int
    recovery_s: float | None
    recovered_within_budget: bool
    budget_s: float
    produced: int
    delivered: int
    consumed: int
    drift: int
    missing: int
    duplicates: int
    regressions: int
    offset_drift: int
    unhealthy_channels: int
    consistent: bool
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.injected
            and self.disruption_observed
            and self.consistent
            and self.recovered_within_budget
        )

    def line(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        recovery = f"{self.recovery_s:.1f}s" if self.recovery_s is not None else "n/a"
        broke = (
            f"broke {self.unhealthy_samples}/{self.hold_samples}"
            if self.hold_samples
            else "broke ?"
        )
        return (
            f"[{verdict}] {self.fault:<20} {broke:>14} | recovery {recovery:>7} "
            f"(budget {self.budget_s:g}s) | produced {self.produced:,} "
            f"consumed {self.consumed:,} | drift {self.drift:+,} "
            f"missing {self.missing:,} dupes {self.duplicates:,}"
        )


def start_loadgen(rate: float, duration: float, channels: int, seed: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            str(PYTHON), str(REPO / "loadgen.py"),
            "--rate", str(rate),
            "--duration", str(duration),
            "--channels", str(channels),
            "--seed", str(seed),
            "--report-interval", "15",
        ],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def start_detector(group: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            str(PYTHON), str(REPO / "detector.py"),
            "--from-beginning",
            "--group", group,
            "--no-foundation-model",
            "--report-interval", "20",
        ],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def parse_loadgen_output(output: str) -> tuple[int, int]:
    produced = delivered = 0
    for line in output.splitlines():
        if line.startswith("produced ") and " events in " in line:
            produced = int(line.split()[1].replace(",", ""))
        elif line.startswith("delivered "):
            parts = line.split()
            delivered = int(parts[1].replace(",", ""))
        elif line.startswith("produced ") and " readings in " in line:
            produced = int(line.split()[1].replace(",", ""))
    return produced, delivered


def verify_consistency(bootstrap: str, topic: str, group: str) -> dict:
    """Replay the whole log with a fresh harness and let the invariant decide."""
    harness = ReconciliationHarness(
        None,
        "unused",
        window_ms=30_000,
        # Lag is meaningless here: this replays data produced minutes ago on purpose.
        thresholds=HealthThresholds(grade_lag=False),
    )
    consumer = build_consumer(bootstrap, group, from_beginning=True)
    try:
        consume_forever(
            consumer,
            harness,
            topic=topic,
            duration_s=300,
            stop_after_idle_s=12,
            report_interval_s=60,
            should_stop=lambda: False,
        )
        harness.drain()
        led = harness.ledger
        audit = audit_offsets(consumer, bootstrap, topic, led.total_readings)
        return {
            "consumed": led.total_readings,
            "channels": len(led.channels),
            "drift": led.total_drift,
            "missing": led.total_missing,
            "duplicates": led.total_duplicates,
            "regressions": led.total_regressions,
            "late": led.late_readings,
            "offset_drift": audit.drift,
            "broker_available": audit.available,
            "unhealthy_channels": len(led.unhealthy_channels),
        }
    finally:
        consumer.close()


def reset_topic(bootstrap: str, topic: str) -> None:
    """Each scenario starts from an empty log, so its drift is its own."""
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap})
    kafka = KafkaSettings.from_env()
    for future in admin.delete_topics([topic], operation_timeout=30).values():
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001 - absent is the desired state either way
            if "UNKNOWN_TOPIC" not in str(exc).upper():
                log.warning("delete topic: %s", exc)
    time.sleep(4)
    for future in admin.create_topics(
        [NewTopic(topic, num_partitions=kafka.readings_partitions, replication_factor=1)]
    ).values():
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            if "ALREADY_EXISTS" not in str(exc).upper():
                raise
    time.sleep(3)


def run_scenario(fault: Fault, args: argparse.Namespace) -> ChaosResult:
    kafka = KafkaSettings.from_env()
    bootstrap = args.bootstrap or kafka.bootstrap
    topic = kafka.readings_topic
    stamp = int(time.time())
    notes: list[str] = []

    print(f"\n{'=' * 78}\n{fault.name}: {fault.description}\n{'=' * 78}", flush=True)
    reset_topic(bootstrap, topic)

    load = start_loadgen(args.rate, args.duration, args.channels, seed=stamp)
    detector = None
    detector_group = f"vigil-chaos-{stamp}"
    if args.with_detector:
        detector = start_detector(detector_group)
    if isinstance(fault, ConsumerKill):
        fault.process = detector
        fault.bootstrap = bootstrap
        fault.group = detector_group
        fault.topic = topic
        # Restart through the same launcher and the same group, so the restarted consumer
        # resumes from the committed offsets rather than starting clean -- which is the
        # whole point of the test.
        fault.restart = lambda: start_detector(detector_group)

    recovery_s: float | None = None
    injected = False
    hold_samples = 0
    unhealthy_samples = 0
    try:
        print(f"settling for {args.settle_s:g}s before injecting...", flush=True)
        time.sleep(args.settle_s)

        fault.inject()
        injected = True
        print(f"fault injected; holding for {args.hold_s:g}s", flush=True)

        # Sample serviceability throughout the hold. This is the evidence that the fault
        # was real: if the broker never once failed to answer, nothing was broken and the
        # scenario proves nothing, however clean the drift figure looks afterwards.
        hold_deadline = time.perf_counter() + args.hold_s
        while time.perf_counter() < hold_deadline:
            hold_samples += 1
            if not fault.healthy():
                unhealthy_samples += 1
            time.sleep(1.0)

        if unhealthy_samples == 0:
            notes.append(
                "the system stayed serviceable for the whole hold: this fault did not "
                "disrupt anything, so its result proves nothing"
            )
            print("WARNING: no disruption observed during the hold", flush=True)
        else:
            print(
                f"unserviceable for {unhealthy_samples}/{hold_samples} samples during the hold",
                flush=True,
            )

        fault.heal()
        started = time.perf_counter()
        try:
            wait_until(fault.healthy, timeout_s=args.budget_s, poll_s=1.0)
            recovery_s = time.perf_counter() - started
            print(f"serving again after {recovery_s:.1f}s", flush=True)
        except TimeoutError:
            recovery_s = None
            notes.append(f"did not return to service within {args.budget_s:g}s")
            print(f"NOT serving within {args.budget_s:g}s", flush=True)

        remaining = max(0.0, args.duration - args.settle_s - args.hold_s)
        print(f"letting the producer finish ({remaining:.0f}s)...", flush=True)
    finally:
        try:
            fault.heal()
        except Exception as exc:  # noqa: BLE001 - must never leave the stack broken
            notes.append(f"heal on cleanup failed: {exc}")
            log.error("heal failed: %s", exc)

        out = ""
        try:
            out, _ = load.communicate(timeout=args.duration + 120)
        except subprocess.TimeoutExpired:
            load.kill()
            out, _ = load.communicate()
            notes.append("load generator had to be killed")

        for proc in (detector, getattr(fault, "_restarted", None)):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()

    produced, delivered = parse_loadgen_output(out)
    if produced and delivered < produced:
        notes.append(f"{produced - delivered:,} records were never acknowledged by the broker")

    print("verifying consistency by replaying the log...", flush=True)
    verdict = verify_consistency(bootstrap, topic, f"vigil-chaos-verify-{stamp}")

    consistent = (
        verdict["drift"] == 0
        and verdict["duplicates"] == 0
        and verdict["regressions"] == 0
        and verdict["unhealthy_channels"] == 0
    )
    if verdict["consumed"] != delivered and delivered:
        notes.append(
            f"broker acknowledged {delivered:,} but the log replays {verdict['consumed']:,}"
        )

    return ChaosResult(
        fault=fault.name,
        description=fault.description,
        injected=injected,
        disruption_observed=unhealthy_samples > 0,
        unhealthy_samples=unhealthy_samples,
        hold_samples=hold_samples,
        recovery_s=recovery_s,
        recovered_within_budget=recovery_s is not None,
        budget_s=args.budget_s,
        produced=produced,
        delivered=delivered,
        consumed=verdict["consumed"],
        drift=verdict["drift"],
        missing=verdict["missing"],
        duplicates=verdict["duplicates"],
        regressions=verdict["regressions"],
        offset_drift=verdict["offset_drift"],
        unhealthy_channels=verdict["unhealthy_channels"],
        consistent=consistent,
        notes=notes,
    )


def build_fault(name: str, args: argparse.Namespace) -> Fault:
    if name == "broker-pause":
        return BrokerPause(hold_s=args.hold_s)
    if name == "network-partition":
        return NetworkPartition(network=args.network)
    if name == "consumer-kill":
        return ConsumerKill()
    return BrokerKill()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        for name, cls in ALL_FAULTS.items():
            print(f"  {name:<20} {cls().description}")
        return 0

    names = list(ALL_FAULTS) if args.all else [args.fault]
    if names == [None]:
        print("pick --fault NAME, or --all, or --list", file=sys.stderr)
        return 2
    if args.fault == "consumer-kill" or args.all:
        args.with_detector = True

    results: list[ChaosResult] = []
    for name in names:
        results.append(run_scenario(build_fault(name, args), args))
        print(results[-1].line(), flush=True)

    print(f"\n{'=' * 78}\nchaos suite: {len(results)} fault modes\n{'=' * 78}", flush=True)
    for r in results:
        print(r.line(), flush=True)
        for note in r.notes:
            print(f"       note: {note}", flush=True)

    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed}/{len(results)} recovered to a verified consistent state", flush=True)

    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(
            json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
        )
        print(f"report written to {args.report_json}", flush=True)

    return 0 if passed == len(results) else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--fault", choices=sorted(ALL_FAULTS), default=None)
    p.add_argument("--all", action="store_true", help="run every fault mode in turn")
    p.add_argument("--list", action="store_true")
    p.add_argument("--rate", type=float, default=800)
    p.add_argument("--channels", type=int, default=8)
    p.add_argument("--duration", type=float, default=90, help="total producer run, seconds")
    p.add_argument("--settle-s", type=float, default=20, help="run clean before injecting")
    p.add_argument("--hold-s", type=float, default=15, help="how long to hold the fault")
    p.add_argument(
        "--budget-s", type=float, default=60, help="recovery budget; NFR-7 sets this at 60s"
    )
    p.add_argument("--with-detector", action="store_true", help="also run the detector")
    p.add_argument("--network", default="vigil_default")
    p.add_argument("--bootstrap", default=None)
    p.add_argument("--report-json", type=Path, default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
