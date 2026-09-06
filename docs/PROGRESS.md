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
| A1 | Durable ingestion (gap-detect + backfill) | **Done, scope corrected** - live MQTT source + gap detection. Backfill is impossible on this feed; edge guarantee restated as at-most-once (ADR-011) |
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
| H1 | Throughput harness | **Partial** - loadgen measures producer-side throughput (76,556 ev/s blast). Consumer-side and the parallelism curve are Phase 2 |
| H2 | Chaos suite | Not started |
| I1 | Honest detection benchmark | Not started |
| I2 | CI quality gate | Not started |

---

## 2. Work log (newest first)

| When | Commit | What |
|---|---|---|
| 2026-09-05 | `8326097` | Recorded ADR-009..014: JSON wire format, the live-source choice, the at-most-once edge correction, the per-(entity, metric) fan-out and residual signal, TSB-AD-M with non-point-adjusted metrics, and keeping the synthetic harness. |
| 2026-09-05 | `8326097` | **Live source wired in.** Added `scripts/fetch_tsb_ad.py` and pulled TSB-AD-M (200 labelled series, 2.4 GB, sha256 `7de86ac2...`). Built the ingestion boundary: `ReadingSource` interface, `SequenceAssigner`, `IngestGapWatch` (per-channel learned cadence), `ReadingPublisher` (shared Kafka path), `SolarFleetSource` (paho MQTT), `SyntheticFleetSource`. Added `mqtt_bridge.py`; rewrote `loadgen.py` onto the shared path. 44 new tests. Measured live: 45 s run, 1,387 MQTT messages -> 11,089 readings across 336 channels, 0 failures, 0 gaps, ~242 readings/s. |
| 2026-09-05 | `7b36870` | Synthetic source with AR(1) noise and labelled injected episodes, the `Reading` wire codec, and the loadgen throughput harness. Measured: 76,556 events/s blast-mode single-process, 0 delivery failures; 2,000 ev/s target mode held to 1,999. |
| 2026-09-05 | `2874dde` | Added `docs/PROGRESS.md` as the resumable handoff record. |
| 2026-09-05 | `af720f1` | Scaffolded the local stack: `docker-compose.yml` with single-node Kafka in KRaft mode and Postgres, replication factor 1 throughout, topic auto-create off with an explicit one-shot `topics` service, all credentials required via `.env` with `${VAR:?...}`. Added `.env.example`, `.gitignore`, `pyproject.toml` (Python 3.12, ruff + pytest config), `src/vigil` package skeleton, empty `docs/BLOCKERS.md`. |
| 2026-09-05 | (pre-git) | Read `BUILD.md`, `CLAUDE.md`, and all of `docs/`. Surveyed the machine: Python 3.12.10 present, Docker Desktop installed (daemon was stopped, started it), Java 17, Node 24, 10-core/16-thread CPU, 15.6 GB RAM, 4 GB VRAM. |

---

## 3. Error log

Every error, test failure, and dead end, with its root cause and fix. Kept so the same wall
is not hit twice.

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `docker info` failed: cannot connect to `npipe:////./pipe/dockerDesktopLinuxEngine` | Docker Desktop was installed but not running; it also lives under `%LOCALAPPDATA%\Programs\DockerDesktop`, not the usual `C:\Program Files\Docker`. | Located the real install path and launched `Docker Desktop.exe`. |
| 2 | `docker exec vigil-kafka /opt/kafka/bin/kafka-topics.sh ...` resolved to `D:/Git/opt/kafka/...` | Git Bash on Windows rewrites arguments that look like absolute POSIX paths before passing them to the container. | Prefix container commands with `MSYS_NO_PATHCONV=1`. |
| 3 | Two `tests/test_ingest_source.py` cases failed on off-by-one values | My test helper `steady()` returned one cadence *past* the last observation, and a hand-counted total was wrong. Production code was correct in both cases. | Fixed the helper to return the last observed timestamp and corrected the expected total. |
| 4 | Bash heredocs containing apostrophes terminated early (`unexpected EOF while looking for matching '`) | The heredoc body is not being passed through literally by this shell wrapper, so quotes inside it are still parsed. | Write multi-line Python to a scratchpad file and execute it, or use the Write tool, instead of piping heredocs. |
| 5 | `kafka.tools.GetOffsetShell` via `kafka-run-class.sh` produced no output | That entry point moved in Kafka 3.9; the supported wrapper is `kafka-get-offsets.sh`. | Used `kafka-get-offsets.sh`. |

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
| 009 | JSON on the wire, no schema registry | 1 |
| 010 | The public TDengine solar-fleet MQTT feed as the live source | 1 |
| 011 | Edge guarantee is at-most-once, gap detection without backfill | 1 |
| 012 | Fan out per (entity, metric); detect on the expected-vs-actual residual | 1 |
| 013 | TSB-AD-M benchmark; threshold-independent metrics, never point-adjusted F1 | 1 |
| 014 | Keep the synthetic generator as the harness, with AR(1) noise | 1 |

---

## 5. Next up

**Exact next action:** build the windowing + z-score baseline detector (story C1) as a Kafka
consumer over the readings topic, then persist detected episodes to Postgres. Specifically:

1. `src/vigil/windows.py` - event-time sliding windows keyed by channel, with a bounded
   out-of-order grace period. Pure and unit-testable; Flink takes this over in Phase 2.
2. `src/vigil/detectors/zscore.py` - rolling z-score over per-channel state via Welford's
   algorithm, scoring each window. Unit-test against a known series.
3. `src/vigil/episodes.py` + a Postgres schema migration - the `episodes`, `context_events`
   and `agent_actions` tables from ARCHITECTURE.md section 6. Raw readings never land here.
4. `detector.py` - the consumer process tying those together end to end.
5. Then the zero-shot foundation-model detector alongside the baseline, and the minimal
   dashboard, to close the Phase 1 gate.

**State of the world for a fresh session:** `docker compose up -d --wait` brings up Kafka
(KRaft, 6 partitions on `sensor.readings`) and Postgres. `.venv` is a Python 3.12.10
virtualenv with the project installed editable. `Datasets/TSB-AD-M/` holds 200 labelled
series. Both sources work: `python loadgen.py --rate 2000 --duration 10` and
`python mqtt_bridge.py --topic inverters --duration 45`.
