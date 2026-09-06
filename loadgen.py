"""Synthetic load generator, throughput harness, and scenario driver.

This is the reproducible half of the data plan. The live source is the solar fleet feed
(`mqtt_bridge.py`); this generator exists because throughput, chaos and correctness tests
need a stream that is seeded, labelled, and drivable at an exact rate -- none of which a
public feed with no SLA can provide.

Rate modes:

  --rate N   target-rate mode. N events/s across all channels, paced against a virtual
             event-time clock that tracks wall time, so timestamps are evenly spaced.

  --rate 0   blast mode. As fast as the client and broker will accept, event time taken
             from the wall clock. Measures the ceiling and backpressure behaviour; it is
             not meant to produce a realistic timeline.

Scenario mode (`--scenario`) additionally schedules deploy markers, publishes them to the
context topic, and injects labelled excursions. It is built so that a system which simply
mutes everything during a deploy window fails visibly rather than scoring well (ADR-015):

  - a fraction of deploys perturb nothing (`--quiet-deploy-fraction`), so suppression
    triggered by the marker alone has nothing legitimate to hide behind;
  - real faults are placed both inside and outside deploy windows
    (`--fault-in-window-fraction`), and the ones inside must still be detected;
  - deploys touch a subset of channels, so a policy that ignores scope over-suppresses.

Report the hardware alongside any number taken from this tool -- see docs/SCALE.md.

    python loadgen.py --rate 2000 --duration 60
    python loadgen.py --rate 0 --duration 30 --channels 32
    python loadgen.py --rate 500 --duration 900 --scenario
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from vigil.ingest.publisher import ReadingPublisher, ThroughputReporter
from vigil.ingest.synthetic_source import SyntheticFleetSource
from vigil.settings import KafkaSettings


def run(args: argparse.Namespace) -> int:
    kafka = KafkaSettings.from_env()
    bootstrap = args.bootstrap or kafka.bootstrap
    topic = args.topic or kafka.readings_topic
    context_topic = args.context_topic or kafka.context_topic

    source = SyntheticFleetSource(
        channels=args.channels,
        rate_per_s=args.rate,
        seed=args.seed,
        anomalies_per_hour=args.anomalies_per_hour,
        duration_s=args.duration,
        scenario=args.scenario,
        deploys_per_hour=args.deploys_per_hour,
        faults_per_hour=args.faults_per_hour,
        quiet_deploy_fraction=args.quiet_deploy_fraction,
        fault_in_window_fraction=args.fault_in_window_fraction,
    )
    publisher = ReadingPublisher(
        bootstrap, topic, linger_ms=args.linger_ms, batch_size=args.batch_size
    )
    reporter = ThroughputReporter(publisher.counters, args.report_interval, args.csv)

    signal.signal(signal.SIGINT, lambda *_: source.close())

    mode = "BLAST (unpaced)" if source.blast else f"{args.rate:,.0f} ev/s target"
    print(
        f"producing to {topic!r} at {bootstrap} | {mode} | "
        f"{args.channels} channels | seed {args.seed}",
        flush=True,
    )
    if source.plan is not None:
        print(f"{source.plan.summary()} -> markers to {context_topic!r}", flush=True)

    try:
        with source:
            for reading in source.readings():
                publisher.publish(reading)
                if source.plan is not None:
                    stream_t_s = (reading.event_ts_ms - source.t0_wall_ms) / 1000.0
                    for event in source.due_context_events(stream_t_s):
                        publisher.publish_context(context_topic, event)
                        print(
                            f"  context: {event.event_id} {event.kind} "
                            f"[{event.t_start_ms}, {event.t_end_ms}] "
                            f"scope={len(event.scope)} ch "
                            f"perturbs={event.perturbed_telemetry} -- {event.detail}",
                            flush=True,
                        )
                publisher.poll(0.0)
                reporter.maybe_report(publisher.queue_depth)
    finally:
        print("\nflushing producer...", flush=True)
        remaining = publisher.flush(30.0)
        if remaining:
            print(f"{remaining:,} messages still queued after 30s flush", file=sys.stderr)
        reporter.summarise()
        if source.plan is not None:
            print(
                f"context markers published: {publisher.context_events_published}",
                flush=True,
            )
            if args.write_plan:
                _write_plan(args.write_plan, source)
                print(f"ground-truth plan written to {args.write_plan}", flush=True)

    return 1 if publisher.counters.failed else 0


def _anchor_event(event, anchor_ms: int):
    """Shift a stream-relative context event onto the run's wall clock."""
    from dataclasses import replace

    return replace(
        event,
        t_start_ms=anchor_ms + event.t_start_ms,
        t_end_ms=anchor_ms + event.t_end_ms,
    )


def _write_plan(path: Path, source: SyntheticFleetSource) -> None:
    """Persist the ground truth so the evaluation harness scores against the plan.

    Written after the run so the wall-clock anchor is known: the plan schedules in
    stream-relative seconds, and only a completed run knows what those map to.
    """
    plan = source.plan
    assert plan is not None
    anchor = source.t0_wall_ms
    payload = {
        "seed": source.name,
        "t0_wall_ms": anchor,
        "duration_s": plan.duration_s,
        "channels": list(plan.channels),
        "deploys": [
            {
                # Anchor the marker to wall clock exactly as the published marker is. The
                # plan previously wrote deploy windows stream-relative while writing faults
                # anchored, so the two never overlapped and the evaluation read zero faults
                # inside context windows -- silently deleting the population that exists to
                # catch blanket suppression.
                "event": json.loads(_anchor_event(d.event, anchor).to_json()),
                "artifacts": {
                    channel: [
                        {
                            "kind": str(e.kind),
                            "origin": str(e.origin),
                            "t_start_ms": anchor + int(e.start_s * 1000),
                            "t_end_ms": anchor + int(e.end_s * 1000),
                            "magnitude_sigma": e.magnitude,
                        }
                        for e in episodes
                    ]
                    for channel, episodes in d.artifacts.items()
                },
            }
            for d in plan.deploys
        ],
        "faults": {
            channel: [
                {
                    "kind": str(e.kind),
                    "origin": str(e.origin),
                    "t_start_ms": anchor + int(e.start_s * 1000),
                    "t_end_ms": anchor + int(e.end_s * 1000),
                    "magnitude_sigma": e.magnitude,
                }
                for e in episodes
            ]
            for channel, episodes in plan.faults.items()
        },
        "populations": {
            "perturbing_deploys": len(plan.perturbing_deploys),
            "quiet_deploys": len(plan.quiet_deploys),
            "faults_inside_deploy_windows": len(plan.faults_inside_deploy_windows()),
            "faults_outside_deploy_windows": len(plan.faults_outside_deploy_windows()),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--rate",
        type=float,
        default=2000,
        help="target events/s across all channels; 0 means blast mode (unpaced)",
    )
    p.add_argument("--duration", type=float, default=60, help="seconds to run; 0 runs until Ctrl-C")
    p.add_argument("--channels", type=int, default=8, help="number of synthetic channels")
    p.add_argument(
        "--seed", type=int, default=1729, help="RNG seed; identical seeds replay identically"
    )
    p.add_argument(
        "--anomalies-per-hour",
        type=float,
        default=12.0,
        help="expected injected episodes per channel per hour (ignored under --scenario)",
    )

    scenario = p.add_argument_group("scenario mode (deploy markers + labelled excursions)")
    scenario.add_argument(
        "--scenario",
        action="store_true",
        help="schedule deploys, publish markers to the context topic, inject labelled excursions",
    )
    scenario.add_argument("--deploys-per-hour", type=float, default=30.0)
    scenario.add_argument("--faults-per-hour", type=float, default=40.0)
    scenario.add_argument(
        "--quiet-deploy-fraction",
        type=float,
        default=None,
        help="fraction of deploys that perturb nothing; these are what expose blanket suppression",
    )
    scenario.add_argument(
        "--fault-in-window-fraction",
        type=float,
        default=None,
        help="fraction of real faults placed inside a deploy window; these must still be detected",
    )
    scenario.add_argument(
        "--write-plan",
        type=Path,
        default=None,
        help="write the ground-truth plan here for the evaluation harness",
    )

    p.add_argument("--topic", default=None, help="override READINGS_TOPIC")
    p.add_argument("--context-topic", default=None, help="override CONTEXT_TOPIC")
    p.add_argument("--bootstrap", default=None, help="override KAFKA_BOOTSTRAP")
    p.add_argument("--linger-ms", type=int, default=20)
    p.add_argument("--batch-size", type=int, default=1_048_576)
    p.add_argument("--report-interval", type=float, default=2.0, help="seconds between rate lines")
    p.add_argument("--csv", type=Path, default=None, help="write per-interval throughput rows here")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
