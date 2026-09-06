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
| 1 | Thin spine: loadgen -> Kafka -> consumer -> windows -> z-score -> episodes in Postgres, then the foundation-model detector, minimal dashboard | Runs end to end from documented commands; an anomaly appears; foundation model runs alongside the baseline; tests pass | **Done** - gate passed 2026-09-05, see section 6 |
| 2 | Correctness and resilience: Flink, event-time, 2PC exactly-once, reconciliation harness, chaos suite, scale harness | Zero reconciliation drift over a long run; >=3 faults recover with bounded lag; throughput-vs-parallelism curve | **In progress** |
| 3 | The core contribution: context-conditioned detection + ClickHouse + Iceberg | Measured false-positive reduction vs. the unconditioned baseline; fail-open verified | Not started |
| 4 | Explanation and agent (thin) | Flagged anomaly explained; propose -> gate -> sandbox execute; trace persisted | Not started |
| 5 | Evaluation and CI | Honest benchmark incl. losses; DeepEval gate fails the build on regression | Not started |
| 6 | Production wrapper and polish | One-command bring-up; README + diagram + demo | Not started |

### Story board

| Story | Title | Status |
|---|---|---|
| A1 | Durable ingestion (gap-detect + backfill) | **Done, scope corrected** - live MQTT source + gap detection. Backfill is impossible on this feed; edge guarantee restated as at-most-once (ADR-011) |
| A2 | Exactly-once processing (Flink, 2PC) | **Written, not yet run** - PyFlink job with event-time watermarks, RocksDB, incremental + unaligned checkpoints, transactional Kafka sink. 14 parity tests prove it computes what the Python detector computes. Image built; job not yet submitted |
| B1 | Reconciliation harness | **Done** - per-channel sequence identity + independent broker-offset audit. Measured drift 0 over 252,000 readings. 42 tests |
| B2 | Pipeline-health signal | **Done** - per-window `PipelineHealth` on `ops.context` as `kind=pipeline`, same wire and schema as a deploy marker (ADR-003). Emitted for clean windows too, so 'clean' is distinguishable from 'no signal' |
| C1 | Z-score baseline detector | **Done** - Welford, decayed reference, mean + dispersion, 18 tests |
| C2 | Foundation-model detector | **Done** - Chronos-Bolt-tiny zero-shot, batched off the critical path, 16 tests |
| D1 | Reconciliation-gated detection | **Built, measuring** - pipeline health consumed through `ContextSignalSource`; attribution requires a plausible mechanism (actual loss/duplication), not mere overlap |
| D2 | Deploy-marker conditioning | **Built, measuring** - scope enforcement + corroboration across scope. An isolated excursion during a deploy stays real |
| D3 | Pluggable conditioning interface | **Done** - `ContextSignalSource` with static/Kafka/composite implementations; pipeline and deploy signals share one wire, one schema, one interface |
| E1 | Explained anomaly (VLM, flagged windows only) | Not started |
| F1 | Safety-gated remediation | Not started |
| G1 | Live dashboard | **Partial** - episodes, per-detector comparison, latency vs. budget. Reconciliation panel absent until Phase 2, and the page says why |
| H1 | Throughput harness | **Partial** - loadgen measures producer-side throughput (76,556 ev/s blast). Consumer-side and the parallelism curve are Phase 2 |
| H2 | Chaos suite | **Done** - 4 fault modes, all broke 20/20 serviceability samples, all recovered within the 60s budget, drift 0 verified by independent replay. `docs/CHAOS.md`. 18 unit tests |
| I1 | Honest detection benchmark | Not started - corpus downloaded, metrics chosen (ADR-013) |
| I2 | CI quality gate | Not started |

---

## 2. Work log (newest first)

| When | Commit | What |
|---|---|---|
| 2026-09-05 | `67630bb` | **Phase 1 gate passed.** Wrote `docs/CORRECTNESS.md` (guarantee per boundary + what is not covered), filled `docs/EVALUATION.md` sections 5.1a-5.1d with measured numbers, wrote `README.md`. |
| 2026-09-05 | `4e55212` | **Phase 2 started.** Reconciliation harness: per-channel sequence identity, independent broker-offset audit, per-window health signal on the context topic. Chaos suite: 4 fault modes with recovery verified by independent replay. Scale harness: parallelism sweep over a fixed pre-filled backlog. |
| 2026-09-06 | `f011835` | **Chaos verified** (4/4, disruption proven), `docs/CHAOS.md`, the paired evaluation harness (`evaluate.py`), and conditioning wired into the detector behind `--conditioning`. |
| 2026-09-06 | `28e09ac` | **The core contribution.** `ContextSignalSource` interface + the conditioning policy: scope enforcement, corroboration across scope, and mechanism plausibility. 34 tests organised around the ways it can go wrong. |
| 2026-09-06 | `ff8f5de` | Chaos scenarios now have to prove they disrupted something. Flink job + container image. |
| 2026-09-05 | `67630bb` | Minimal dashboard + FastAPI backend. Validated palette (all-pairs CVD/contrast pass in both modes), status as glyph+word, screenshot-verified in light and dark. Reconciliation panel deliberately absent with an on-page explanation. 11 API tests. |
| 2026-09-05 | `0837598` | Zero-shot Chronos-Bolt detector running alongside the baseline, batched off the critical path via a bounded-queue worker thread. Measured the whole Bolt family on this CPU. Fixed a context-leakage bug and a threading race. 27 tests. |
| 2026-09-05 | `d7a8f04`, `396a670` | Detection spine: event-time sliding windows (per-channel watermarks, lateness, thin-window drop), z-score baseline over Welford with a decayed reference, episode merging, Postgres schema + idempotent sink. 54 tests including 18 against real Postgres. |
| 2026-09-05 | `8112c7a`, `21021dc` | Adversarial scenario generator + `ops.context` topic + paired success metric (ADR-015/016/017). Revised NFR-1 and NFR-8 in `docs/REQUIREMENTS.md`. Created `docs/EVALUATION.md`. Found two real generator bugs. 25 tests. |
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
| 6 | Scheduled spikes never appeared in the signal, though the ground-truth plan listed them | A spike has zero duration, so the containment check `start <= t <= end` essentially never matched a discrete sample grid point. The evaluation would have expected detections the data never contained. | Fire a scheduled episode on the first sample at or after its start, without requiring containment. |
| 7 | Scheduled episodes still produced no labelled samples after fix 6 | The expiry check ran immediately after activation and cancelled the episode in the same call that started it, because an instantaneous spike does not cover the grid point it lands on. | Skip the expiry check for a just-activated episode. |
| 8 | `chronos.predict_quantiles()` raised `missing 1 required positional argument: 'inputs'` | The Chronos-Bolt pipeline names its first parameter `inputs`, not `context`. | Used `inputs=`. |
| 9 | The foundation detector scored zero windows: everything came back cold | The leakage guard *rejected* any window whose channel history already ran past its start. Windows overlap by 20 s at the default geometry, so that is always true and nothing was ever scorable. | Slice the history at the window's start instead of rejecting, and keep deque headroom beyond the context length so slicing does not starve it. |
| 10 | Dashboard rendered blank with `Cannot read properties of undefined (reading 'firstChild')` | `Node.append()` returns undefined, so `table.append(el("thead")).firstChild` threw. Caught only by screenshotting the page; no unit test would have found it. | Build the `thead` as a named variable. |
| 11 | The dashboard fix appeared to have no effect | The HTML is a module-level constant, so the running uvicorn process still held the old string. | Restarted the server. Worth remembering before debugging any future template change. |
| 12 | `docker exec ... kafka-topics.sh` resolved to a Windows path | Git Bash rewrites POSIX-looking arguments. | `MSYS_NO_PATHCONV=1` prefix. |
| 13 | The reconciliation harness emitted 187,631 health windows for a 7-minute run, nearly all with a single reading | Windows were closed against the **maximum** event time seen. Kafka serves partitions in bursts, so one partition raced minutes ahead and closed windows the others had not reached; each later reading then re-opened and re-emitted the same window. | Close windows against the **minimum** event time across sources, and never re-open a window that has already been emitted. |
| 14 | After that fix, 51% of readings still arrived after their window closed | Sources were discovered lazily, on first delivery. A partition Kafka had not yet served was indistinguishable from one that did not exist, so the minimum was taken over a subset. | Register every partition on assignment via the rebalance callback; a registered-but-silent source blocks the watermark entirely. Plus an idleness timeout so an empty partition cannot stall forever. Result: 0% late. |
| 16 | The chaos suite reported 4/4 passes, three of which meant nothing | Only `broker-kill` actually disrupted anything. The producer logged no errors during the broker pause or the network partition, and both reported 0.0s recovery. Injecting a fault does not imply it bit. | Sample serviceability once a second throughout the hold; a scenario with zero unhealthy samples now fails. |
| 17 | The network-partition health check reported healthy throughout an applied partition | It tested only the Kafka client path, and Docker's published-port proxy keeps answering the TCP handshake after a container leaves its network. | Inspect the container's network attachments directly as well. |
| 18 | `consumer-kill` measured 0.0s recovery | Its health check returned true the instant the process exited, so it measured how long it took to notice a dead process was dead. | Restart the detector through a caller-supplied factory and define recovery as consumer-group lag returning to near zero. Real figure: 25.1s. |
| 19 | `pip3 install --break-system-packages` failed in the Flink image | The base image is Ubuntu 22.04, whose pip predates that flag -- and it is not externally-managed, so the flag was unnecessary. | Dropped the flag. |
| 20 | Conditioning reported `no_context` for an out-of-scope deploy | The signal *source* was filtering by scope, so an out-of-scope deploy was indistinguishable from no deploy at all. | Sources filter on time; the policy owns scope, so it can tell an operator "there was a deploy but it did not touch this channel". |
| 15 | Every window of a replay graded `critical` | Lag is measured against the wall clock, so replaying a topic recorded minutes ago honestly reports minutes of lag. True, but it is a fact about the data's age, not a live disturbance -- and it would have made conditioning suppress everything. | Added `--ignore-lag` for replays and benchmarks; live runs still grade on lag. The measurement stays in the record either way; only the severity judgement changes. |

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
| 015 | Schedule the evaluation adversarially so blanket suppression fails visibly | 1 |
| 016 | Report false-positive reduction and recall as a pair, never a single number | 1 |
| 017 | The latency budget binds the hot path, not the foundation model | 1 |
| 018 | Build the reconciliation harness before Flink | 2 |
| 019 | Watermarks take the minimum across sources, with an idleness timeout | 2 |
| 020 | Lag grading is separable from loss grading | 2 |
| 021 | A chaos scenario must prove it disrupted something | 2 |
| 022 | Run Flink as containers with PyFlink, not as a host process or a Java job | 2 |

---

## 5. Next up

**In flight right now:** `python evaluate.py --duration 900 --rate 400 --channels 12` -- the
paired shadow-vs-conditioned measurement for NFR-8. This is the number the project exists to
produce.

**Then, in order:**

1. Record the paired result in `docs/EVALUATION.md` section 3.4, whatever it says. If
   conditioning does not clear the 40%/~0% bar, that is the finding and it gets published
   with the reasoning, not tuned until it passes.
2. Run the scale sweep (`python scale.py --fill 300000 --parallelism 1,2,3,4,6,8`), write
   `docs/SCALE.md` with the curve and where it plateaus.
3. Submit the Flink job (`docker compose --profile flink up -d`, then `flink run -py
   /opt/vigil/scoring_job.py`), reconcile its output against the Python detector's on the
   same readings, and add a Flink checkpoint-recovery chaos scenario.
4. Start the multi-hour soak for NFR-6 (>= 4 hours, drift 0). It must run after chaos and
   scale, both of which deliberately break or saturate the stack.
5. Phase 3 remainder: ClickHouse for serving, Iceberg as the durable lake.

## 6. Phase 1 gate evidence

Gate (BUILD.md section 7): *runs end to end from documented commands; an anomaly appears;
the foundation model runs alongside the baseline; tests pass.*

| Gate clause | Evidence |
|---|---|
| Runs end to end from documented commands | 7-minute live run, commands recorded in `docs/EVALUATION.md` section 5.1a. 252,000 readings produced, **252,000 consumed**, 0 delivery failures, 0 late readings. |
| An anomaly appears | 213 windows flagged, merged into **24 episodes** in Postgres, carrying injected ground truth (level_shift 3, spike 5, variance_burst 2) and visible on the dashboard. |
| The foundation model runs alongside the baseline | Both detectors scored the same stream and raised episodes independently: `zscore` 19, `chronos-bolt-tiny` 5. 360 windows submitted to the model, 296 scored, **0 dropped**. |
| Tests pass | **202 passing**, including integration tests against real Kafka and real Postgres. Lint clean. |
| Latency within budget | Hot path p99 **0.493 ms** against the 250 ms NFR-1 budget. |

Deliberately *not* claimed by this gate: end-to-end throughput (producer and consumer were
measured separately), and zero reconciliation drift (needs the Phase 2 harness over a
multi-hour run).
