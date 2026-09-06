# Progress & handoff notes

> Living log. Together with `git log` and `docs/BLOCKERS.md` this is the complete handoff:
> a fresh session should be able to read this file and resume without re-deriving anything.
> Updated after every step, not at the end.

**Reference hardware** (every measured number in this repo was taken here):
Intel Core i7-12650H, 10 cores / 16 threads, 15.6 GB RAM, NVIDIA RTX 3050 Ti Laptop (4 GB VRAM),
Windows 11, Docker Desktop, Python 3.12.10.

---

## 1. Status board

Phases are from `BUILD.md` section 7; stories from `docs/USER_STORIES.md`.

| Phase | Scope | Gate | Status |
|---|---|---|---|
| 0 | Requirements and design | Plan reviewed, repo scaffolded | **Done** |
| 1 | Thin spine: loadgen -> Kafka -> consumer -> windows -> z-score -> episodes in Postgres, then the foundation-model detector, minimal dashboard | Runs end to end from documented commands; an anomaly appears; foundation model runs alongside the baseline; tests pass | **In progress** |
| 2 | Correctness and resilience: Flink, event-time, 2PC exactly-once, reconciliation harness, chaos suite, scale harness | Zero reconciliation drift over a long run; >=3 faults recover with bounded lag; throughput-vs-parallelism curve | Not started |
| 3 | The core contribution: context-conditioned detection + ClickHouse + Iceberg | Measured false-positive reduction vs. the unconditioned baseline; fail-open verified | Not started |
| 4 | Explanation and agent (thin) | Flagged anomaly explained; propose -> gate -> sandbox execute; trace persisted | Not started |
| 5 | Evaluation and CI | Honest benchmark incl. losses; DeepEval gate fails the build on regression | Not started |
| 6 | Production wrapper and polish | One-command bring-up; README + diagram + demo | Not started |

### Story board

| Story | Title | Status |
|---|---|---|
| A1 | Durable ingestion (gap-detect + backfill) | Not started |
| A2 | Exactly-once processing (Flink, 2PC) | Not started |
| B1 | Reconciliation harness | Not started |
| B2 | Pipeline-health signal | Not started |
| C1 | Z-score baseline detector | Not started |
| C2 | Foundation-model detector | Not started |
| D1 | Reconciliation-gated detection | Not started |
| D2 | Deploy-marker conditioning | Not started |
| D3 | Pluggable conditioning interface | Not started |
| E1 | Explained anomaly (VLM, flagged windows only) | Not started |
| F1 | Safety-gated remediation | Not started |
| G1 | Live dashboard | Not started |
| H1 | Throughput harness | Not started |
| H2 | Chaos suite | Not started |
| I1 | Honest detection benchmark | Not started |
| I2 | CI quality gate | Not started |

---

## 2. Work log (newest first)

| When | Commit | What |
|---|---|---|
| 2026-09-05 | `af720f1` | Scaffolded the local stack: `docker-compose.yml` with single-node Kafka in KRaft mode and Postgres, replication factor 1 throughout, topic auto-create off with an explicit one-shot `topics` service, all credentials required via `.env` with `${VAR:?...}`. Added `.env.example`, `.gitignore`, `pyproject.toml` (Python 3.12, ruff + pytest config), `src/vigil` package skeleton, empty `docs/BLOCKERS.md`. |
| 2026-09-05 | (pre-git) | Read `BUILD.md`, `CLAUDE.md`, and all of `docs/`. Surveyed the machine: Python 3.12.10 present, Docker Desktop installed (daemon was stopped, started it), Java 17, Node 24, 10-core/16-thread CPU, 15.6 GB RAM, 4 GB VRAM. |

---

## 3. Error log

Every error, test failure, and dead end, with its root cause and fix. Kept so the same wall
is not hit twice.

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `docker info` failed: cannot connect to `npipe:////./pipe/dockerDesktopLinuxEngine` | Docker Desktop was installed but not running; it also lives under `%LOCALAPPDATA%\Programs\DockerDesktop`, not the usual `C:\Program Files\Docker`. | Located the real install path and launched `Docker Desktop.exe`. |

---

## 4. Decisions

ADRs live in `docs/DECISIONS.md`. Design-phase ADR-001..008 predate this build.

| ADR | Decision | Phase |
|---|---|---|
| 001 | Zero-shot time-series foundation model as the detection spine | design |
| 002 | Condition detection on reconciliation signals (the core mechanism) | design |
| 003 | Generalize conditioning to deploy markers via a pluggable interface | design |
| 004 | VLM explanation on flagged windows only | design |
| 005 | Apache Flink for stateful exactly-once processing | design |
| 006 | Storage split: Postgres (episodes) / ClickHouse (serving) / Iceberg (truth) | design |
| 007 | Conditioning fails open when the health signal is unavailable | design |
| 008 | Solve context-conditioned detection deeply; defer alert-correlation and drift to v2 | design |

---

## 5. Next up

**Exact next action:** write `loadgen.py` -- the synthetic multi-channel producer to the
readings topic, with a target-rate mode and a `--rate 0` blast mode that reports sustained
events/sec. Then bring the compose stack up and verify the producer reaches Kafka.
