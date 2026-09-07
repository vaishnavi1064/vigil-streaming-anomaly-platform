"""The paired evaluation: shadow against conditioned, on byte-identical data.

The protocol from ADR-016, run end to end:

  1. Produce one scenario into Kafka, writing its ground-truth plan to disk **before** any
     detector sees the data.
  2. Run the reconciliation harness so pipeline-health events reach the context topic.
  3. **Shadow pass**: the detector with conditioning off. This is the unconditioned
     baseline, and it reads exactly the same records the conditioned pass will.
  4. **Conditioned pass**: same records, same seed, conditioning on.
  5. **Fail-open pass**: conditioning on, pointed at a context topic that exists and is
     empty. ADR-007 says a missing signal must never suppress, so this pass has to reproduce
     the shadow pass episode for episode. Anything else means silence on the context path is
     being read as permission to mute.

     Precisely which half of ADR-007 this covers: the source here is *available and silent*,
     which the policy answers with `no_context`. The other half -- a source that cannot be
     reached at all, answered with `fail_open` -- is covered by unit tests in
     `tests/test_conditioning.py`, because taking the broker away mid-run would also take
     the readings away and there would be nothing left to condition.
  6. Score the passes against the plan and report the pair.

The two passes write to separate Postgres schemas so neither can see or overwrite the
other's episodes, and both replay the same topic from offset zero in their own consumer
group. Running them against separately-generated streams would compare two different runs
and call the difference an effect.

Nothing here reports a false-positive reduction on its own. The output is always the pair,
including the recall columns that a blanket suppressor would fail (ADR-016).

    python evaluate.py --duration 1800 --channels 12
    python evaluate.py --duration 600 --channels 12 --report-json docs/results/paired.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from vigil.evaluation import (
    FailOpenCheck,
    GroundTruth,
    ObservedEpisode,
    PairedComparison,
    compare_fail_open,
    score_pass,
)
from vigil.settings import KafkaSettings, PostgresSettings

REPO = Path(__file__).resolve().parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


def reset_topics(bootstrap: str, kafka: KafkaSettings) -> None:
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap})
    wanted = {
        kafka.readings_topic: kafka.readings_partitions,
        kafka.context_topic: 1,
        # Created and deliberately left empty: the fail-open pass needs a context topic that
        # exists and says nothing, which is what a stalled signal producer looks like.
        f"{kafka.context_topic}.empty": 1,
    }
    for future in admin.delete_topics(list(wanted), operation_timeout=30).values():
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            if "UNKNOWN_TOPIC" not in str(exc).upper():
                print(f"delete topic: {exc}", file=sys.stderr)
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


def run_step(name: str, argv: list[str], env: dict | None = None, timeout: float = 3600) -> str:
    print(f"\n--- {name} ---", flush=True)
    merged = {**os.environ, **(env or {})}
    proc = subprocess.run(
        [str(PYTHON), *argv], cwd=REPO, capture_output=True, text=True, env=merged, timeout=timeout
    )
    tail = "\n".join(proc.stdout.strip().splitlines()[-12:])
    print(tail, flush=True)
    if proc.returncode != 0:
        print(proc.stderr[-2000:], file=sys.stderr)
    return proc.stdout


def conditioning_lines(stdout: str) -> list[str]:
    """The detector's own account of what conditioning did, kept with the numbers.

    The verdict breakdown, the corroboration-evidence counts and the barrier's measured
    delay are the evidence for and against every claim in `docs/EVALUATION.md` section 3.
    Scraping them out of a console afterwards is how a number ends up in a document with
    no run behind it, so they travel in the report.
    """
    keep = ("conditioning:", "corroboration evidence", "verdict barrier:", "context events seen")
    return [line.strip() for line in stdout.splitlines() if line.strip().startswith(keep)]


def make_schema(settings: PostgresSettings, name: str) -> None:
    with psycopg.connect(settings.dsn, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{name}"')


def drop_schema(settings: PostgresSettings, name: str) -> None:
    with psycopg.connect(settings.dsn, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def read_episodes(settings: PostgresSettings, schema: str) -> list[ObservedEpisode]:
    with psycopg.connect(settings.dsn, row_factory=dict_row) as conn:
        conn.execute(f'SET search_path TO "{schema}"')
        rows = conn.execute(
            "SELECT channel, t_start_ms, t_end_ms, status, raised_by, peak_score, attributed_to"
            " FROM episodes ORDER BY t_start_ms"
        ).fetchall()
    return [
        ObservedEpisode(
            channel=r["channel"],
            t_start_ms=r["t_start_ms"],
            t_end_ms=r["t_end_ms"],
            status=r["status"],
            raised_by=r["raised_by"],
            peak_score=float(r["peak_score"]),
            attributed_to=r["attributed_to"],
        )
        for r in rows
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kafka = KafkaSettings.from_env()
    postgres = PostgresSettings.from_env()
    bootstrap = args.bootstrap or kafka.bootstrap
    stamp = uuid.uuid4().hex[:8]
    plan_path = args.plan or Path(f"docs/results/paired-plan-{stamp}.json")

    shadow_schema = f"vigil_shadow_{stamp}"
    conditioned_schema = f"vigil_cond_{stamp}"
    fail_open_schema = f"vigil_failopen_{stamp}"

    print(f"paired evaluation {stamp}: {args.duration:g}s at {args.rate:g} ev/s", flush=True)
    reset_topics(bootstrap, kafka)

    # --- 1. produce one scenario, ground truth written before anything scores it ---
    run_step(
        "producing the scenario",
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
            "--deploys-per-hour",
            str(args.deploys_per_hour),
            "--faults-per-hour",
            str(args.faults_per_hour),
            "--report-interval",
            "60",
            "--write-plan",
            str(plan_path),
        ],
        timeout=args.duration + 300,
    )

    # --- 2. pipeline health onto the context topic ---
    run_step(
        "reconciling (emits pipeline health)",
        [
            "reconciler.py",
            "--from-beginning",
            "--group",
            f"vigil-eval-recon-{stamp}",
            "--stop-after-idle-s",
            "12",
            "--ignore-lag",
            "--no-store",
            "--report-interval",
            "60",
        ],
        timeout=900,
    )

    detector_args = [
        "detector.py",
        "--from-beginning",
        "--stop-after-idle-s",
        "15",
        "--no-foundation-model",
        "--threshold",
        str(args.threshold),
        "--report-interval",
        "60",
    ]

    # --- 3. shadow: the unconditioned baseline ---
    make_schema(postgres, shadow_schema)
    run_step(
        "shadow pass (conditioning OFF)",
        [*detector_args, "--group", f"vigil-eval-shadow-{stamp}"],
        env={"PGOPTIONS": f"-c search_path={shadow_schema}"},
        timeout=1800,
    )

    # --- 4. conditioned: identical records ---
    make_schema(postgres, conditioned_schema)
    conditioned_stdout = run_step(
        "conditioned pass (conditioning ON)",
        [
            *detector_args,
            "--group",
            f"vigil-eval-cond-{stamp}",
            "--conditioning",
            "--min-corroborating-channels",
            str(args.min_corroborating_channels),
            "--min-scope-fraction",
            str(args.min_scope_fraction),
            "--synchrony-ms",
            str(args.synchrony_ms),
            "--verdict-buffer-ms",
            str(args.verdict_buffer_ms),
        ],
        env={"PGOPTIONS": f"-c search_path={conditioned_schema}"},
        timeout=1800,
    )

    # --- 4b. fail-open: conditioning on, with nothing to condition on (ADR-007) ---
    fail_open: FailOpenCheck | None = None
    if args.verify_fail_open:
        make_schema(postgres, fail_open_schema)
        run_step(
            "fail-open pass (conditioning ON, context topic empty)",
            [
                *detector_args,
                "--group",
                f"vigil-eval-failopen-{stamp}",
                "--conditioning",
                "--context-topic",
                f"{kafka.context_topic}.empty",
                "--min-corroborating-channels",
                str(args.min_corroborating_channels),
                "--min-scope-fraction",
                str(args.min_scope_fraction),
                "--synchrony-ms",
                str(args.synchrony_ms),
                "--verdict-buffer-ms",
                str(args.verdict_buffer_ms),
            ],
            env={"PGOPTIONS": f"-c search_path={fail_open_schema}"},
            timeout=1800,
        )

    # --- 5. score the passes ---
    truth = GroundTruth.from_plan(plan_path)
    shadow_episodes = read_episodes(postgres, shadow_schema)
    shadow = score_pass("shadow", shadow_episodes, truth)
    conditioned = score_pass("conditioned", read_episodes(postgres, conditioned_schema), truth)
    if args.verify_fail_open:
        fail_open = compare_fail_open(shadow_episodes, read_episodes(postgres, fail_open_schema))
    comparison = PairedComparison(
        shadow=shadow,
        conditioned=conditioned,
        fp_reduction_target=args.fp_target,
        recall_loss_tolerance=args.recall_tolerance,
    )

    print(f"\n{'=' * 96}", flush=True)
    print("PAIRED RESULT (ADR-016: never a single number)", flush=True)
    print(f"{'=' * 96}", flush=True)
    print(
        f"ground truth: {len(truth.faults)} real faults "
        f"({len(truth.faults_inside_windows())} inside context windows, "
        f"{len(truth.faults_outside_windows())} outside, "
        f"{len(truth.faults_inside_quiet_windows())} inside quiet windows) | "
        f"{len(truth.artifacts)} injected artifacts | {len(truth.windows)} deploy windows",
        flush=True,
    )
    print(flush=True)
    print(comparison.table(), flush=True)
    print(flush=True)
    print(comparison.verdict(), flush=True)
    if fail_open is not None:
        print("")
        print(fail_open.line(), flush=True)

    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(
            json.dumps(
                {
                    "run": stamp,
                    "plan": str(plan_path),
                    "config": {
                        "duration_s": args.duration,
                        "rate": args.rate,
                        "channels": args.channels,
                        "seed": args.seed,
                        "threshold": args.threshold,
                        "min_corroborating_channels": args.min_corroborating_channels,
                        "min_scope_fraction": args.min_scope_fraction,
                        "synchrony_ms": args.synchrony_ms,
                        "verdict_buffer_ms": args.verdict_buffer_ms,
                    },
                    "ground_truth": {
                        "faults": len(truth.faults),
                        "faults_inside_windows": len(truth.faults_inside_windows()),
                        "faults_outside_windows": len(truth.faults_outside_windows()),
                        "faults_inside_quiet_windows": len(truth.faults_inside_quiet_windows()),
                        "artifacts": len(truth.artifacts),
                        "windows": len(truth.windows),
                    },
                    "shadow": asdict(shadow),
                    "conditioned": asdict(conditioned),
                    "fp_reduction": comparison.fp_reduction,
                    "recall_loss": comparison.recall_loss,
                    "recall_loss_inside": comparison.recall_loss_inside,
                    "meets_target": comparison.meets_target,
                    "fail_open": asdict(fail_open) if fail_open else None,
                    "conditioning": conditioning_lines(conditioned_stdout),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nreport written to {args.report_json}", flush=True)

    if not args.keep_schemas:
        drop_schema(postgres, shadow_schema)
        drop_schema(postgres, conditioned_schema)
        drop_schema(postgres, fail_open_schema)

    if fail_open is not None and not fail_open.held:
        return 2
    return 0 if comparison.meets_target else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--duration", type=float, default=1800)
    p.add_argument("--rate", type=float, default=400)
    p.add_argument("--channels", type=int, default=12)
    p.add_argument("--seed", type=int, default=20260906)
    p.add_argument("--deploys-per-hour", type=float, default=40)
    p.add_argument("--faults-per-hour", type=float, default=80)
    p.add_argument("--threshold", type=float, default=8.0)
    p.add_argument("--min-corroborating-channels", type=int, default=2)
    p.add_argument("--min-scope-fraction", type=float, default=0.25)
    p.add_argument("--synchrony-ms", type=int, default=5_000)
    p.add_argument(
        "--verdict-buffer-ms",
        type=int,
        default=30_000,
        help="event-time delay before a conditioning verdict is taken, so the in-scope "
        "siblings that could exonerate an episode have closed first (G-7). 0 reproduces "
        "the racing behaviour v1-v3 measured",
    )
    p.add_argument("--fp-target", type=float, default=0.40, help="NFR-8 half one")
    p.add_argument("--recall-tolerance", type=float, default=0.05, help="NFR-8 half two")
    p.add_argument("--plan", type=Path, default=None)
    p.add_argument("--report-json", type=Path, default=None)
    p.add_argument("--keep-schemas", action="store_true", help="leave the per-pass schemas behind")
    p.add_argument(
        "--no-verify-fail-open",
        dest="verify_fail_open",
        action="store_false",
        help="skip the third pass. On by default: a conditioning policy that suppresses when "
        "its signal is missing is worse than no conditioning, so the check is not optional",
    )
    p.add_argument("--bootstrap", default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
