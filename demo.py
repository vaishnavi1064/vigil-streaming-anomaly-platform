"""One command that runs the whole platform end to end, with a fault injected while it runs.

    python demo.py

What it does, in order: produces a scenario with labelled faults and deploy markers into
Kafka; **pauses the broker mid-run** so the demo has to survive something rather than merely
work; reconciles the stream against the broker's own offsets; detects with conditioning on;
and hands the sharpest episode to the safety-gated agent. Then it prints what happened and
whether each claim held.

The fault is the point. A demo that only runs on a healthy stack shows that the code
executes; this one shows that the producer's retry buffer absorbs a broker outage, that
reconciliation can prove nothing was lost afterwards, and that detection carries on. If the
fault does not disrupt anything, the demo says so rather than taking credit.

Nothing here is special-cased for demonstration. It calls the same `loadgen.py`,
`reconciler.py` and `detector.py` the measurements call, with the same flags, and it reads
its results out of Postgres afterwards.

    python demo.py --duration 90          # shorter or longer
    python demo.py --no-fault             # skip the injection
    python demo.py --keep                 # leave the episodes behind for the dashboard
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from vigil.agent import GatePolicy, RemediationAgent, SafetyGate, SandboxExecutor, load_runbooks
from vigil.chaos import BrokerPause
from vigil.episodes import Episode, EpisodeStatus
from vigil.settings import KafkaSettings, PostgresSettings

REPO = Path(__file__).resolve().parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


@dataclass
class Claim:
    """One thing the demo asserts, and whether it held. Printed whatever the answer."""

    name: str
    held: bool
    detail: str

    def line(self) -> str:
        return f"  [{'ok' if self.held else 'FAILED'}] {self.name}: {self.detail}"


def run(
    name: str, argv: list[str], timeout: float, env: dict | None = None
) -> subprocess.CompletedProcess:
    print(f"\n--- {name} --- ", flush=True)
    proc = subprocess.run(
        [str(PYTHON), *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    for line in proc.stdout.strip().splitlines()[-8:]:
        print(f"  {line}", flush=True)
    if proc.returncode != 0:
        print(proc.stderr[-1500:], file=sys.stderr)
    return proc


def field_from(output: str, prefix: str) -> str:
    for line in output.splitlines():
        if line.startswith(prefix):
            return line
    return ""


def reset_topics(kafka: KafkaSettings) -> None:
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": kafka.bootstrap})
    wanted = {kafka.readings_topic: kafka.readings_partitions, kafka.context_topic: 1}
    for future in admin.delete_topics(list(wanted), operation_timeout=30).values():
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            if "UNKNOWN_TOPIC" not in str(exc).upper():
                print(f"  delete topic: {exc}", file=sys.stderr)
    time.sleep(4)
    for future in admin.create_topics(
        [NewTopic(t, num_partitions=p, replication_factor=1) for t, p in wanted.items()]
    ).values():
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            if "ALREADY_EXISTS" not in str(exc).upper():
                raise
    time.sleep(3)


def require_stack(kafka: KafkaSettings, postgres: PostgresSettings) -> None:
    """Fail immediately and say what to run, rather than timing out three steps later."""
    from confluent_kafka.admin import AdminClient

    try:
        AdminClient({"bootstrap.servers": kafka.bootstrap}).list_topics(timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Kafka not reachable at {kafka.bootstrap} ({exc}).\nRun: docker compose up -d --wait"
        ) from exc
    try:
        psycopg.connect(postgres.dsn, connect_timeout=5).close()
    except psycopg.Error as exc:
        raise SystemExit(
            f"Postgres not reachable ({exc}).\nRun: docker compose up -d --wait"
        ) from exc


def inject_after(delay_s: float, hold_s: float, container: str) -> dict:
    """Pause the broker mid-run, in a thread, so production is genuinely interrupted.

    A pause rather than a kill: TCP connections stay open, so the producer sees silence
    rather than a reset, which is the shape of a GC pause or a saturated host and the case
    where the client's own timeouts decide whether anything is lost.
    """
    outcome: dict = {"injected": False, "error": None, "hold_s": hold_s}

    def worker() -> None:
        time.sleep(delay_s)
        fault = BrokerPause(container=container, hold_s=hold_s)
        try:
            print(f"\n  injecting fault: {fault.name} for {hold_s:g}s", flush=True)
            fault.inject()
            outcome["injected"] = True
            time.sleep(hold_s)
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = str(exc)
        finally:
            try:
                fault.heal()
                print("  fault healed", flush=True)
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = f"heal failed: {exc}"

    thread = threading.Thread(target=worker, name="chaos", daemon=True)
    thread.start()
    return outcome


def make_schema(settings: PostgresSettings, name: str) -> None:
    """A schema of its own.

    The demo used to read the default schema, which still held episodes from whatever ran
    last -- so it reported 62 episodes for a run that produced 8, and handed the agent a
    stale one. Isolating it also means the demo never deletes anything a user had.
    """
    with psycopg.connect(settings.dsn, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{name}"')


def drop_schema(settings: PostgresSettings, name: str) -> None:
    with psycopg.connect(settings.dsn, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def read_episodes(settings: PostgresSettings, schema: str) -> list[dict]:
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        conn.execute(f'SET search_path TO "{schema}"')
        return conn.execute(
            "SELECT id, channel, t_start_ms, t_end_ms, status, raised_by, peak_score,"
            " window_count, attributed_to FROM episodes ORDER BY peak_score DESC"
        ).fetchall()


def remediate(top: dict) -> tuple[str, list[str]]:
    """Hand the sharpest episode to the agent. Read-only against the real gate and sandbox."""
    episode = Episode(
        channel=top["channel"],
        t_start_ms=top["t_start_ms"],
        t_end_ms=top["t_end_ms"],
        raised_by=top["raised_by"],
        peak_score=float(top["peak_score"]),
        window_count=top["window_count"],
        threshold=8.0,
        status=EpisodeStatus(top["status"]),
    )
    agent = RemediationAgent(
        runbooks=load_runbooks(REPO / "runbooks"),
        gate=SafetyGate(policy=GatePolicy()),
        executor=SandboxExecutor(),
    )
    run_result = agent.handle(episode, top["id"])
    lines = [
        f"{s.action.kind} -> {s.decision.verdict} ({s.decision.reason})" for s in run_result.steps
    ]
    return run_result.diagnosis.summary, lines


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kafka = KafkaSettings.from_env()
    postgres = PostgresSettings.from_env()
    require_stack(kafka, postgres)

    print(f"vigil demo: {args.duration:g}s at {args.rate:g} ev/s on {args.channels} channels")
    schema = f"vigil_demo_{uuid.uuid4().hex[:8]}"
    pg_env = {"PGOPTIONS": f"-c search_path={schema}"}
    print(f"stack: {kafka.bootstrap} -> {kafka.readings_topic} -> postgres schema {schema}")
    reset_topics(kafka)
    make_schema(postgres, schema)

    fault = {"injected": False, "error": None, "hold_s": 0.0}
    if not args.no_fault:
        fault = inject_after(args.fault_at, args.fault_hold, args.container)

    produced = run(
        "producing a scenario (labelled faults + deploy markers)",
        [
            "loadgen.py",
            "--rate",
            str(args.rate),
            "--duration",
            str(args.duration),
            "--channels",
            str(args.channels),
            "--scenario",
            "--seed",
            str(args.seed),
            "--report-interval",
            "30",
            "--deploys-per-hour",
            str(args.deploys_per_hour),
            "--faults-per-hour",
            str(args.faults_per_hour),
            "--write-plan",
            str(args.plan),
        ],
        timeout=args.duration + 240,
    )

    reconciled = run(
        "reconciling against the broker's own offsets",
        [
            "reconciler.py",
            "--from-beginning",
            "--group",
            f"vigil-demo-recon-{int(time.time())}",
            "--stop-after-idle-s",
            "10",
            "--ignore-lag",
            "--report-interval",
            "30",
        ],
        timeout=600,
        env=pg_env,
    )

    detected = run(
        "detecting, with context conditioning on",
        [
            "detector.py",
            "--from-beginning",
            "--group",
            f"vigil-demo-detect-{int(time.time())}",
            "--stop-after-idle-s",
            "12",
            "--no-foundation-model",
            "--conditioning",
            "--report-interval",
            "30",
        ],
        timeout=900,
        env=pg_env,
    )

    episodes = read_episodes(postgres, schema)
    claims: list[Claim] = []

    delivered = field_from(produced.stdout, "delivered ")
    claims.append(
        Claim("produced", produced.returncode == 0, delivered or "no delivery line in output")
    )

    drift_line = field_from(reconciled.stdout, "ZERO DRIFT") or field_from(
        reconciled.stdout, "DRIFT"
    )
    claims.append(
        Claim(
            "reconciliation reports zero drift",
            reconciled.returncode == 0 and drift_line.startswith("ZERO DRIFT"),
            drift_line or "no drift line in output",
        )
    )
    claims.append(
        Claim(
            "broker offset audit agrees",
            "offset drift +0" in reconciled.stdout,
            field_from(reconciled.stdout, "broker log") or "no audit line",
        )
    )

    if not args.no_fault:
        claims.append(
            Claim(
                "a fault was injected while the stream ran",
                bool(fault["injected"]) and not fault["error"],
                fault["error"] or f"broker paused for {fault['hold_s']:g}s mid-production",
            )
        )

    claims.append(
        Claim(
            "episodes were raised",
            bool(episodes),
            f"{len(episodes)} episodes, sharpest peak {episodes[0]['peak_score']:.1f} on "
            f"{episodes[0]['channel']}"
            if episodes
            else "none",
        )
    )
    # The summary line, not the startup line: "conditioning: on, reading ..." says only that
    # the flag was set. What matters is that every episode got a verdict.
    summary_line = next(
        (line for line in detected.stdout.splitlines() if line.startswith("conditioning: decided")),
        "",
    )
    decided = 0
    if summary_line:
        decided = int(summary_line.split("decided")[1].split("|")[0].strip())
    claims.append(
        Claim(
            "conditioning recorded a verdict for every episode",
            bool(summary_line) and decided == len(episodes),
            f"{decided} decided against {len(episodes)} episodes"
            + (f" -- {summary_line}" if summary_line else " -- no conditioning summary"),
        )
    )

    if episodes:
        summary, steps = remediate(episodes[0])
        claims.append(
            Claim(
                "every agent action passed the safety gate",
                all("approved" in s for s in steps),
                f"{len(steps)} actions on episode {episodes[0]['id']}",
            )
        )
    else:
        summary, steps = "", []

    print(f"\n{'=' * 88}\nDEMO RESULT\n{'=' * 88}")
    for claim in claims:
        print(claim.line())
    if steps:
        print(f"\n  agent on the sharpest episode -- {summary}")
        for step in steps:
            print(f"    {step}")
    print("\n  dashboard: python -m uvicorn vigil.api:app --port 8000  ->  http://127.0.0.1:8000")

    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(
            json.dumps(
                {
                    "claims": [vars(c) for c in claims],
                    "episodes": len(episodes),
                    "fault": fault,
                    "agent_steps": steps,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n  report written to {args.report_json}")

    if args.keep:
        print("")
        print(f"  episodes kept in schema {schema}; point the dashboard at it with:")
        print(
            f'    PGOPTIONS="-c search_path={schema}" python -m uvicorn vigil.api:app --port 8000'
        )
    else:
        drop_schema(postgres, schema)

    failed = [c for c in claims if not c.held]
    print(f"\n{len(claims) - len(failed)}/{len(claims)} claims held")
    return 1 if failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--rate", type=float, default=800)
    p.add_argument("--channels", type=int, default=8)
    p.add_argument("--seed", type=int, default=20260906)
    # High enough that a 60-second demo actually contains deploys: the conditioning path is
    # the contribution, and a demo that never exercises it is showing the easy half.
    p.add_argument("--deploys-per-hour", type=float, default=180)
    p.add_argument("--faults-per-hour", type=float, default=240)
    p.add_argument("--fault-at", type=float, default=20, help="seconds into the run")
    p.add_argument("--fault-hold", type=float, default=8)
    p.add_argument("--container", default="vigil-kafka")
    p.add_argument("--no-fault", action="store_true", help="run without injecting anything")
    p.add_argument("--keep", action="store_true", help="leave episodes in Postgres afterwards")
    p.add_argument("--plan", type=Path, default=REPO / "docs" / "results" / "demo-plan.json")
    p.add_argument("--report-json", type=Path, default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
