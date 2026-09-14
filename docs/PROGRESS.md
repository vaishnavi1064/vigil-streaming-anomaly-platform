# Progress & handoff notes

> Living log. Together with `git log` and `docs/BLOCKERS.md` this is the complete handoff:
> a fresh session should be able to read this file and resume without re-deriving anything.
> Updated after every step, not at the end.

**Reference hardware** (every measured number in this repo was taken here):
Intel Core i7-12650H, 10 cores / 16 threads, 15.6 GB RAM, NVIDIA RTX 3050 Ti Laptop (4 GB VRAM),
Windows 11, Docker Desktop, Python 3.12.10. **One exception:** the QLoRA planner training and
eval ran on Northeastern Explorer, one V100-SXM2-32GB, because a 7B adapter does not fit in
4 GB of VRAM. Those numbers say so where they appear.

---

## 1. Status board

Phases are from `BUILD.md` section 7; stories from `docs/USER_STORIES.md`.

| Phase | Scope | Gate | Status |
|---|---|---|---|
| 0 | Requirements and design | Plan reviewed, repo scaffolded | **Done** |
| 1 | Thin spine: loadgen -> Kafka -> consumer -> windows -> z-score -> episodes in Postgres, then the foundation-model detector, minimal dashboard | Runs end to end from documented commands; an anomaly appears; foundation model runs alongside the baseline; tests pass | **Done** - gate passed 2026-09-05, see section 6 |
| 2 | Correctness and resilience: Flink, event-time, 2PC exactly-once, reconciliation harness, chaos suite, scale harness | Zero reconciliation drift over a long run; >=3 faults recover with bounded lag; throughput-vs-parallelism curve | **Gate met.** 5/5 faults recovered with proven disruption, including the Flink checkpoint-recovery run; curve produced and the plateau named; **drift 0 over 240.0 minutes and 5,749,412 readings**, meeting NFR-6. The soak's broker audit shows +77 from a shutdown boundary, explained in `docs/CORRECTNESS.md` section 4a (G-14) |
| 3 | The core contribution: context-conditioned detection + ClickHouse + Iceberg | Measured false-positive reduction vs. the unconditioned baseline; fail-open verified | **Gate clauses satisfied; the requirement behind them is not met, and that is now a settled result rather than an open question.** Four measurements published (+60.9%/-36.7%, +9.0%/-10.0%, +6.7%/-6.7%, **+11.1%/-6.7%**); fail-open verified live twice, 73/73 and 76/76. **NFR-8 missed in all four.** The plausibility half works in every run (69 of 76 raised as implausible); the corroboration half is too weak. **ClickHouse and Iceberg are now built and running** (ADR-043 to ADR-046): the serving store holds readings and window scores, the lake holds the durable record on MinIO, and the reconciliation harness audits against it -- ledger and lake agreed on 6/6 channels from two independently derived counts |
| 4 | Explanation and agent (thin) | Flagged anomaly explained; propose -> gate -> sandbox execute; trace persisted | **Gate clauses built; one of them is still unverifiable here.** Closed action set, deterministic gate, sandbox, runbook RAG, full loop -- 98 tests. The explainer now has a real-model backend: Claude reads the rendered chart through the Messages API (ADR-041), selected by `ANTHROPIC_API_KEY`, and the explanation reaches the diagnoser as inert evidence (ADR-042). 40 explainer tests, none of which call Anthropic. **No key exists in this environment**, so "flagged anomaly explained" is proven against a stub and a local fake, not against a model -- NFR-2 and explanation quality stay unmeasured (C-2), `docs/EVALUATION.md` section 7 |
| 5 | Evaluation and CI | Honest benchmark incl. losses; DeepEval gate fails the build on regression | **Gate met, with one substitution, and the fine-tune is now done.** 200-series benchmark run and published including the loss: the foundation model is beaten by the z-score baseline at 141x the cost, and where it does win is named. **The QLoRA planner trained on the Explorer cluster and beat the baseline: 97.7% exact-match against 20.0% for the generous rules and 0.0% for the rules as deployed, with forbidden actions down from 61.3% to 1.0%** (`docs/EVALUATION.md` section 6). Quality gate runs in CI and fails on floors or baseline drift -- but it is structural, not LLM-judged, since Ragas/DeepEval need a key (C-2) |
| 6 | Production wrapper and polish | One-command bring-up; README + diagram + demo | **Deployment layer built and statically validated; not applied to a cluster.** One-command bring-up still works unchanged (`docker compose up -d`, four services healthy in 7.3 s). `k8s/` has 25 resources, `terraform/` owns namespace + quota + limits + Secret, `monitoring/` has 10 alert rules and a 9-panel dashboard, and an app image builds at 900 MB. 10 validation checks, 0 failures (`python scripts/validate_deploy.py`). **No pod has ever been scheduled** -- ADR-047 to ADR-049, `docs/DEPLOYMENT.md`, `docs/EVALUATION.md` section 9. README polish and the <60 s demo remain |

### Story board

| Story | Title | Status |
|---|---|---|
| A1 | Durable ingestion (gap-detect + backfill) | **Done, scope corrected** - live MQTT source + gap detection. Backfill is impossible on this feed; edge guarantee restated as at-most-once (ADR-011) |
| A2 | Exactly-once processing (Flink, 2PC) | **Done, and now fault-tested.** Killed mid-checkpoint, restored from checkpoint 5, recovered in 14.9 s, and a read_committed consumer saw 328 distinct window scores with **0 duplicates**. Parity after the crash: 328/328, max difference 7.7e-12. Single kill, single TaskManager (see CHAOS section 5) |
| B1 | Reconciliation harness | **Done** - per-channel sequence identity + independent broker-offset audit. **Drift 0 over 5,749,412 readings and 4 hours** (NFR-6), and over 360,000 in the shorter drain-based runs where the offset audit is also exactly +0. 42 tests |
| B2 | Pipeline-health signal | **Done** - per-window `PipelineHealth` on `ops.context` as `kind=pipeline`, same wire and schema as a deploy marker (ADR-003). Emitted for clean windows too, so 'clean' is distinguishable from 'no signal' |
| C1 | Z-score baseline detector | **Done** - Welford, decayed reference, mean + dispersion, 18 tests |
| C2 | Foundation-model detector | **Done** - Chronos-Bolt-tiny zero-shot, batched off the critical path, 16 tests |
| D1 | Reconciliation-gated detection | **Done and measured** - the mechanism check works: 40 episodes overlapping a lossless pipeline event were correctly raised as `implausible`, not attributed |
| D2 | Deploy-marker conditioning | **Measured four times, all published, NFR-8 missed in all four.** v1 (co-occurrence) +60.9%/-36.7%; v2 (synchrony on quantised starts) +9.0%/-10.0%; low density +6.7%/-6.7%; **v3 (synchrony on true onsets) +11.1%/-6.7%**. The onset fix (ADR-035) moved both numbers the right way and did not reach the 40% target. The hypothesis is now tested rather than mismeasured: corroboration never fires often enough, mostly because exonerating siblings have not closed yet (G-7). B-4 closed, no fifth attempt planned |
| S1 | Serving store (ClickHouse) | **Done and measured.** Readings and per-window scores in `ReplacingMergeTree`, plus an `AggregatingMergeTree` per-minute rollup the dashboard reads instead of scanning raw rows. 8,000 readings through loadgen -> Kafka -> `warehouse.py` -> ClickHouse, 0 undecodable, mean write 262 ms/batch, every channel's sequence span matching its reading count exactly. Episodes deliberately **not** mirrored (ADR-045). API: `/metrics`, `/metrics/{channel}`, `/scores` new; `/episodes` and `/detectors` still Postgres; `/health` reports both. 19 integration tests |
| S2 | Event lake (Iceberg on MinIO) | **Done and measured.** Parquet partitioned by event day, JDBC catalog in Postgres (ADR-043), effectively-once via offsets in the snapshot summary (ADR-044). Restarted with `--from-beginning` and read **0 records** because the snapshot's offsets won; 3,600 further readings resumed exactly at the boundary; **0 duplicate rows** over 2 snapshots. `python lake.py --verify` re-checks against the stored bytes and exits non-zero on a gap or duplicate. 18 integration tests |
| P1 | Container image | **Done.** One image for all five entry points rather than five, because they are five ways into one package that shares a wire format and a ledger. Built and exercised: 900 MB, every CLI entrypoint parses, runs as non-root uid 10001. torch and the training extras deliberately excluded so a dashboard rollout does not pay for them |
| P2 | Kubernetes manifests | **Built and validated, never applied.** 25 resources: StatefulSets for the four stores with volumeClaimTemplates, a 2-replica API Deployment, four single-replica consumer Deployments, Jobs for topics and bucket, and Flink plus an agent-quality CronJob outside the default apply set. `kubeconform -strict` at k8s 1.31.0: 25/25 valid; `kubectl kustomize` renders 19. **No pod has ever been scheduled** (ADR-048 for why four workloads have no probes) |
| P3 | Terraform | **Built and validated, never applied.** Namespace, ResourceQuota, LimitRange and the Secret with generated passwords -- not a second copy of the manifests (ADR-047). `terraform validate` succeeds and `fmt -check` is clean; the provider has never authenticated to a cluster. No cloud provider block, deliberately |
| P4 | Observability | **Configured and validated; nothing has scraped.** `/metrics` on the API exports 9 gauges read from the stores at scrape time, and series are absent rather than 0 when unmeasured. 10 alert rules (`promtool`), Alertmanager routing with inhibit rules (`amtool`), a 9-panel Grafana dashboard whose every expression is valid PromQL over a metric that exists. Consumer-group lag cannot fire -- no Kafka exporter is deployed -- and is labelled as such rather than deleted (ADR-049) |
| S3 | Reconciliation against the lake | **Done and measured.** `python reconciler.py --audit-lake` checks the stored table's identity invariants and compares it channel by channel against the live ledger. **6/6 channels agreed** -- one count from streaming the Kafka log, one from reading Parquet off object storage, sharing no code and no state. It also separates the two causes of a repeat: `duplicate_rows` is a sink fault, `reused_seq` is the producer restarting (ADR-046) |
| D3 | Pluggable conditioning interface | **Done** - `ContextSignalSource` with static/Kafka/composite implementations; pipeline and deploy signals share one wire, one schema, one interface |
| E1 | Explained anomaly (VLM, flagged windows only) | **Built against a real model, still unverifiable without a key.** Window rendered to a PNG with the flagged span shaded and sent to **Claude** (`claude-sonnet-5`) as an image block through Anthropic's Messages API, with the OpenAI-compatible endpoint kept as the second backend behind one `WindowExplainer` contract (ADR-041); key read from `ANTHROPIC_API_KEY` only. Attached to the episode and carried into the diagnosis as evidence that cannot move the symptom or the retrieval query (ADR-042). Bounded off-path queue, so a slow model costs detection nothing; every failure -- refusal, empty answer, transport error, unrenderable window, missing key -- is an absence with a stated reason and never text without a model behind it. **40 tests, none of which call Anthropic.** Measured without a key: render **54 ms p50**, 28-32 KB PNG, and **one call per 15 window scorings** (141 of 2,112 on the v4w run). Unmeasured, and named as such: NFR-2's 5 s budget and whether the explanations are any good (C-2) |
| F1 | Safety-gated remediation | **Done** - closed action set, deterministic gate that never reads the rationale, sandbox with a structural interlock, BM25 runbook grounding. 98 tests |
| G1 | Live dashboard | **Done, and now actually live.** Episodes, per-detector comparison, latency vs. budget, and the reconciliation panel story G1 calls Must: ledger drift beside the independent broker-offset audit, per-window health with grades. **Plus a live stream chart** -- inline SVG, no library -- showing readings arriving at one point per second per channel with detected episodes drawn over them and a throughput counter. Still refuses to render numbers when no run has happened. Screenshot-verified light and dark at 2x with zero JS console errors; images in `docs/images/`. 19 API tests |
| H1 | Throughput harness | **Done** - producer 76,556 ev/s blast; consumer-side curve measured over a 3.9 M backlog: 94,495/s at one consumer, plateau **170,414/s at 3-6 consumers** over 6 partitions. NFR-4 met (20,000 target); NFR-5's near-linear claim **not** met, 1.80x at 6 consumers. `docs/SCALE.md` |
| H2 | Chaos suite | **Done** - 5 fault modes including the Flink checkpoint-recovery scenario, all broke 20/20 serviceability samples, all recovered within the 60s budget, drift 0 verified by independent replay. `docs/CHAOS.md`. 18 unit tests |
| I1 | Honest detection benchmark | **Done, and the foundation model lost.** 144 of 200 series scored (56 dropped by truncation, stated): zscore median AUC-PR 0.198 against chronos-bolt-tiny 0.152, head to head 78/56/10, at **141x less compute**. The regime boundary is named: the model wins where normal is structured and non-stationary (Exathlon 19-8) and loses where an anomaly is a sharp excursion against a flat baseline (SVDB 21-1) |
| I2 | CI quality gate | **Done for what is measurable without a judge.** CI runs lint, format, unit, integration, a secret scan, repo-standards checks, and `agent_quality.py`: grounding, gate approval, sandbox containment, safety compliance and abstention as rates over 400 episodes, failing on a broken floor or on drift below a committed baseline. 11 tests, most of which break the agent on purpose. Ragas/DeepEval need an LLM judge and a key (C-2) |
| I3 | Tool-calling fine-tune | **Done, and the model won.** Trained on Northeastern Explorer (V100-SXM2-32GB, QLoRA 4-bit, fp16, 75 steps, 34.6 min, final loss 0.034) and scored on the same 300 held-out cases as the rules: **97.7% exact-match (293/300) against 20.0% for the generous baseline and 0.0% for the rules as deployed**, forbidden actions 1.0% against 61.3%, 0 schema-invalid and 0 ungrounded across 300 plans. slow_drift -- the one symptom the rules get right -- stays 60/60, so the model learned the teacher's policy rather than the complement of the baseline. Not zero-harm (3 forbidden proposals, gate-invisible) and not yet wired into the running agent. `docs/EVALUATION.md` section 6 |

---

## 2. Work log (newest first)

| When | Commit | What |
|---|---|---|
| 2026-09-13 | (this commit) | **Dashboard got live charts, screenshots, and the README became the front door.** A live SVG stream chart polling `/stream` every 2 s: six channels averaged to one point per second, episodes drawn as a merged band with markers on the channel that moved, and a throughput counter whose liveness dot tracks whether the newest event time changed rather than the clock. Two rendering flaws found by looking at it: episodes starting before the window drew a band with no marker, and ten concurrent bands stacked their opacity until the chart read as one continuous anomaly. The series palette went from two colours to six, spaced for deuteranopia and protanopia, all twelve values checked at WCAG 1.4.11 (worst 3.12:1). Screenshots needed two resets to be honest -- the first showed drift -23,667 because the topic held four loadgen runs and each restarts its sequence (ADR-046), so the stack was wiped and one clean producer lifetime captured instead: **ZERO DRIFT, 147,000 readings**. README rewritten results-first; `architecture.svg` had four stale labels corrected, including 'gap-fill + backfill' for a backfill that ADR-011 established is impossible and 'React + FastAPI' for a React app that was never built. |
| 2026-09-13 | (this commit) | **Phase 6 deployment layer: manifests, IaC and monitoring, all statically validated and none of it applied.** An app image first, because the manifests needed something real to reference -- 900 MB, every entrypoint parses, non-root. Then 25 Kubernetes resources mirroring the compose topology rather than an idealised one, Terraform for what surrounds them, and a monitoring stack where every panel and rule queries a metric that actually exists (cross-checked: 9 referenced, 9 exported, 0 phantom). **Three things this refused to fake.** There is no long-running agent in this repo, so there is no agent Deployment -- what ships is a CronJob for the quality gate, which is a real entrypoint. The four consumers get no probes, because `exec: true` is decoration and probing Kafka turns a broker outage into a crash-loop. And no cloud provider block, which would have been the most impressive-looking file here and the least honest. Also found: `kubectl --dry-run=client` cannot validate offline at all, so kubeconform did the work and EVALUATION 9.2 says so. `python scripts/validate_deploy.py` -- 10 passed, 0 failed, 0 skipped. |
| 2026-09-13 | (this commit) | **Phase 3's storage half built: ClickHouse and Iceberg, both with real data through them.** ClickHouse serves readings, window scores and a per-minute rollup; Iceberg on MinIO holds the durable record with a JDBC catalog in the Postgres that was already running. The lake sink puts its Kafka offsets in the Iceberg snapshot summary, so position and rows commit atomically -- restarted with `--from-beginning` it read 0 records and stayed at 0 duplicates. The reconciler now audits against the lake and the two agreed on 6/6 channels. **Two defects found by running it rather than by reading it.** The rollup materialized view counted rows written, not distinct readings, so a replayed batch double-counted -- `uniqExact` fixed it and a test pins it. And `(channel, seq)` turned out not to be unique across producer restarts: two genuinely different readings collided on seq=5, which would have made ClickHouse silently delete the older one at merge time. Identity is now `(channel, seq, event_ts)` (ADR-046), which also narrows an unconditional claim in CORRECTNESS.md section 2. 37 new integration tests against the real containers. |
| 2026-09-13 | (this commit) | **The explainer got a real model behind it (E-1).** `ClaudeExplainer` sends the rendered window to Anthropic's Messages API as an image block with the episode's numbers beside it; the OpenAI-compatible endpoint B-1 specified stays as the second backend, and both sit behind one `WindowExplainer` contract so the never-raises promise and the counters are written once -- the 18 existing contract tests pass against it unchanged. Three things the wire actually required: no `temperature` (current Sonnet 400s on sampling parameters), `source.base64` rather than `image_url`, and `stop_reason` read before `content` so a refusal is a stated absence rather than an empty answer. The explanation now reaches the diagnoser, as **evidence only** -- it cannot move the symptom or the retrieval query (ADR-042), because section 6.3 had just documented that the gate does not check appropriateness to the symptom. 22 new tests, none of which call Anthropic. **Still no key in this environment**, so what is measured is the render (54 ms p50, 28-32 KB) and the firing rate (141 calls per 2,112 window scorings); NFR-2 and explanation quality are reported as unmeasured in `docs/EVALUATION.md` section 7. |
| 2026-09-13 | (this commit) | **B-3 answered: the QLoRA planner beat the deterministic one.** Trained on Northeastern Explorer (V100-SXM2-32GB, NF4 + fp16, LoRA r=32, 1 epoch, 75 steps, 34.6 min, final loss 0.034, no NaN) and scored on the same 300 held-out cases as both baselines: **97.7% exact-match (293/300) against 20.0% given licences and 0.0% as deployed**, forbidden actions **1.0% against 61.3%**, and 0 schema-invalid / 0 unknown verbs / 0 ungrounded actions across 300 plans. The win is on the four symptoms the rules score 0/60 on; `slow_drift`, the one they get right, stays 60/60, which is the control against a model that learned to disagree rather than to plan. Three forbidden proposals remain and the safety gate rejected none of them -- symptom appropriateness is outside what it checks. Published as `docs/EVALUATION.md` section 6 with the limits attached: hand-written teacher ceiling, one generator for both splits, one seed, and 15.8 s p50 per plan. ADR-039, ADR-040. |
| 2026-09-05 | `67630bb` | **Phase 1 gate passed.** Wrote `docs/CORRECTNESS.md` (guarantee per boundary + what is not covered), filled `docs/EVALUATION.md` sections 5.1a-5.1d with measured numbers, wrote `README.md`. |
| 2026-09-05 | `4e55212` | **Phase 2 started.** Reconciliation harness: per-channel sequence identity, independent broker-offset audit, per-window health signal on the context topic. Chaos suite: 4 fault modes with recovery verified by independent replay. Scale harness: parallelism sweep over a fixed pre-filled backlog. |
| 2026-09-06 | `f011835` | **Chaos verified** (4/4, disruption proven), `docs/CHAOS.md`, the paired evaluation harness (`evaluate.py`), and conditioning wired into the detector behind `--conditioning`. |
| 2026-09-06 | (this commit) | **VLM explainer built** (`src/vigil/explain/`), which `docs/BLOCKERS.md` had already claimed was built. Renders the flagged window as a plot, sends it to a hosted endpoint, attaches the result to the episode, and degrades to a recorded absence on every failure path. Wired into the detector behind a bounded queue. 18 tests. |
| 2026-09-07 | (this commit) | **v3 measured: NFR-8 missed a fourth time, and the hypothesis is now tested rather than mismeasured.** +11.1% false-page reduction against a 40% target, -6.7% recall against a 5% tolerance, on the same seed and density as v1/v2 with only the timestamp resolution changed. The onset fix bought +2.1 points and 3.3 points less recall loss. Fail-open held 76/76. All four runs are in `docs/EVALUATION.md` sections 3.4-3.7; B-4 closed with no fifth attempt planned. |
| 2026-09-07 | (this commit) | **NFR-6 met: the 4-hour soak passed.** 5,749,412 readings, 240.0 minutes, drift 0 at every one of sixteen checkpoints, 480 health windows none disturbed. The broker audit's +77 is a shutdown boundary -- the consumer's fixed clock expired while the producer was still producing, confirmed by the log end reaching 5,760,000 afterwards -- and is recorded as G-14 rather than rounded away. |
| 2026-09-07 | `66eb56a`, `07ef221` | **The fine-tune task reshaped so a model can win, and the QLoRA pipeline built.** Hard set withholds the symptom and gives per-window evidence; five shapes `Diagnoser` misclassifies; retrieval returns two distractors; held-out licences are prose on disjoint vocabulary. Measured baseline: **0% exact as deployed, 20% given licences, 61% forbidden**. Dry run passes against the real Qwen tokenizer (prompts max 1,261 tokens, 225 steps). Training not run -- ADR-036, B-3 has the cluster command. |
| 2026-09-07 | `7984331` | **Episodes carry a true onset**, fixing the defect that made v1 and v2 of the conditioning measurement ask a bucket-coincidence question (ADR-035). All 35 episodes in a 420 s scenario now sit off the 10 s grid, spread 0-27 s inside their window. |
| 2026-09-06 | (this commit) | **Flink exactly-once fault-tested, closing the biggest correctness gap.** TaskManager SIGKILLed mid-checkpoint, held down 60 s: 58/58 samples unhealthy, restored from checkpoint 5, recovered 14.9 s, drift 0, and **328 committed window scores with 0 duplicates**. The first attempt reported 0.1 s and PASS for a job that had not redeployed -- Flink's heartbeat timeout means a dead job reports healthy for ~50 s -- and the health check now requires the vertices to have actually moved. |
| 2026-09-06 | (this commit) | **200-series benchmark run: the foundation model lost.** zscore median AUC-PR 0.198 vs chronos-bolt-tiny 0.152, 78/56/10 head to head, 59 s vs 8,312 s of compute. Regime boundary measured both ways -- by dataset family and by anomaly density -- and the model's win rate falls from 48% on rare anomalies to 18% when more than a tenth of windows are anomalous. |
| 2026-09-06 | (this commit) | **One-command demo** (`demo.py`): produce, pause the broker mid-stream, reconcile, detect, remediate; 7/7 claims held. Found and fixed two of its own defects first. |
| 2026-09-06 | (this commit) | **Agent quality gate built and wired into CI.** Structural rates only, since the judged metrics need a key: 400 episodes, 1,267 actions, 100% grounded / gate-approved / sandboxed, 97.1% citing a runbook. Fails on a broken floor or on drift below the committed baseline -- and the baseline is what catches a planner that escalates everything, which breaks no floor at all. |
| 2026-09-06 | (this commit) | **Fail-open verified end to end and the reconciliation panel built.** The low-density sensitivity run doubles as the fail-open check: 73 of 73 episodes identical to the unconditioned pass with `no_context=73`. Sensitivity itself: at 20 deploys/hour conditioning neither blankets (quiet-window and outside-window recall untouched) nor helps much (+6.7% FP reduction), so density is not what makes NFR-8 miss. |
| 2026-09-06 | (this commit) | **Scale sweep run and `docs/SCALE.md` written.** Plateau 170,414 readings/s at 3-6 consumers over 6 partitions; NFR-4 met, NFR-5's near-linear claim not met at 1.80x. Multi-consumer drains read up to 2,100 records *more* than were produced -- rebalance re-delivery, a measured statement of why this path is at-least-once. |
| 2026-09-06 | `95aa6d4` | **v2 measured and published: NFR-8 missed again (+9.0% FP reduction, -10.0% recall), and the test itself was invalid.** Episode start times are the start of the window that flagged them, and windows slide by 10 s, so a 5 s synchrony tolerance could only ever match an exact tie. Ground truth for the same run puts consecutive in-scope artifact onsets a median 1.8 s apart. Recorded as B-4. |
| 2026-09-06 | `1c3a330` | Fail-open is now a third pass in the paired harness rather than an argument: conditioning on, context topic empty, required to reproduce the unconditioned pass episode for episode. |
| 2026-09-06 | `62eee9e`, `25a409b` | Tool-calling training set committed with tests. Its abstention population was empty as first written and is now constructed deliberately. Counting distinct targets over 1,200 examples gives five, which reopens the fine-tune question as B-3. |
| 2026-09-06 | `0c43105` | The agent no longer falls silent: a planner that proposes nothing, or retrieval that licenses nothing applicable, now produces a gated escalation instead of an empty run. |
| 2026-09-06 | `8fe97e3` | **Phase 3 measured, twice.** v1 conditioning failed NFR-8 honestly (+60.9% FP reduction, -36.7% recall, -100% in quiet windows); diagnosed as corroboration-by-coincidence; refined to require synchrony; re-measuring. Published both. ADR-024..029. |
| 2026-09-06 | `e9647ef`, `7b42f18`, `1502832` | **Phase 4 agent.** Closed typed action set, deterministic safety gate that never reads the rationale, sandbox with a structural interlock, BM25 runbook retrieval with per-passage action licences, full Diagnoser -> Planner -> Gate -> Executor loop. 98 tests. |
| 2026-09-06 | `32479f7`, `b714efb` | Flink job submitted and verified: 1,056 windows scored, differences of exactly 0.0000 against the Python detector. Flink checkpoint-recovery chaos scenario added. |
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
| 21 | The conditioned pass crashed with `ForeignKeyViolation` on `episodes_attributed_to_fkey` | The detector attributed an episode to `deploy-0001` without ever inserting that event into `context_events`. The FK -- added deliberately and covered by a test -- refused the write. Only 10 episodes landed before the process died, which made the run look like catastrophic over-suppression. | An `Attribution` now carries the event, not just its id, so the caller can persist what the episode points at first. |
| 22 | The evaluation read 0 faults inside context windows when the scenario had scheduled 13 | `_write_plan` anchored fault times to wall clock but wrote deploy windows stream-relative, so they could never overlap. The population that exists to catch blanket suppression was invisible while the run still printed a confident table. | Anchor the deploy event the same way. Regression test asserts the loader recovers exactly the count the scenario scheduled. |
| 23 | The tolerant AUC metric scored a **perfect** detector 0.48 | Tolerance was applied by dilating labels, so a detector firing exactly on the labelled points was penalised for not covering the buffer -- a detector that smeared its output would have scored higher. | Removed rather than approximated (ADR-023). Detection latency replaces it. Caught by a test asserting a perfect detector scores 1.0. |
| 24 | The TSB-AD benchmark measured F1 = 0.000 on every series | Window scores were spread across their points, creating plateaus of identical values; a point-level alarm budget then picked arbitrary points from inside one. | Score at window resolution against window labels. |
| 25 | Detection delay reported 0 for a detector that never fired | It re-derived its alarm set by thresholding with `>=`, so a constant-scoring detector alarmed on every point. | Share one alarmed mask between the budgeted score and the delay measurement. |
| 26 | Conditioning v1 returned `isolated` zero times out of 79 episodes and suppressed every real fault in a quiet deploy window | Corroboration asked whether in-scope siblings were flagged anywhere in the same 30 s window. At realistic density two are, by coincidence, so the criterion stopped discriminating. | Require **synchrony**: siblings must have started within ~5 s, because a deploy artifact is simultaneous across its scope and independent faults are not. Both measurements published. |
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
| 023 | Drop VUS-PR rather than ship a version that punishes precision | 3 |
| 024 | Corroboration across scope as the discriminator, and why it had to be synchrony | 3 |
| 025 | A scope of one explains nothing | 3 |
| 026 | The agent emits typed actions, never commands | 4 |
| 027 | The gate never reads the agent's rationale | 4 |
| 028 | An attributed episode gets no remediation | 4 |
| 029 | BM25 for runbook retrieval, not embeddings | 4 |
| 030 | An agent that can propose nothing escalates instead of falling silent | 4 |
| 031 | The training set's abstention population is constructed, and says so | 5 |
| 032 | The CI quality gate measures structure, not judgement, and says which | 5 |
| 033 | A chaos scenario must prove recovery happened, not just that health returned | 2 |
| 034 | The explainer renders a picture, and writes nothing when it has no model | 4 |
| 035 | The episode carries the time it began, not the boundary that noticed it | 3 |
| 036 | The fine-tune's task is reshaped rather than dropped, with the teacher named as the ceiling | 5 |
| 037 | A conditioning verdict waits behind an event-time barrier, and the evidence is recorded when it exists | 3 |
| 038 | Attribution requires the shape of a blast radius, not only the timing of one | 3 |
| 043 | The Iceberg catalog is a JDBC catalog in the application Postgres, not a REST service | 3 |
| 044 | The lake sink's offsets live in the Iceberg snapshot, not in Kafka | 3 |
| 045 | ClickHouse serves readings and window scores; episodes are not mirrored into it | 3 |
| 046 | A reading is identified by (channel, seq, event_ts), because the producer's sequence restarts | 3 |
| 047 | Terraform owns what surrounds the manifests, not a second copy of them | 6 |
| 048 | The consumers get no liveness or readiness probe, and the manifest says why | 6 |
| 049 | Monitoring ships only what it can actually scrape, and names the rest | 6 |
| 041 | Claude reads the chart, and the presence of its key is the only thing that selects it | 4 |
| 042 | The explanation is evidence; it may not move the symptom or the query | 4 |
| 039 | Completion-only masking via collator, fp16 on V100, and a memory-safe resumable eval | 5 |
| 040 | Out-of-set symptoms and prose licences are what made the fine-tune winnable | 5 |

---

## 5. Next up

**B-3 is done and the fine-tune won.** Trained on Northeastern Explorer and scored against both
baselines on the 300 held-out hard cases: **97.7% exact-match against 20.0% and 0.0%**, forbidden
actions **1.0% against 61.3%**, and `slow_drift` -- the control symptom the rules get right --
held at 60/60. `docs/EVALUATION.md` section 6, ADR-039 and ADR-040. What remains is deployment,
not training: the adapter is not wired into `RemediationAgent`, so the rules still plan.

**Waiting on the architect:** B-5 needs a decision on whether NFR-3 is
restated or the window geometry changes. **B-6 is new and is the one that matters**: on the
v4w run only 37 of 112 false pages overlap an injected excursion, so the 40% reduction target
had a ceiling of 33% before the policy decided anything. Redefining the denominator, fixing
the detector, or leaving NFR-8 as written is a judgement about what the number is for.
C-2 still needs a key: the explainer now calls Claude when `ANTHROPIC_API_KEY` is set (ADR-041),
and nothing in this environment sets it, so NFR-2's 5 s budget and the quality of the explanations
remain the two things that cannot be measured here.

**B-4 is answered by v4, and the answer is that the root cause was distributed-systems
rather than statistical.** The corroboration test was deciding before its evidence arrived
(G-7, ADR-037) and recording a verdict it had not reached (G-16). With both fixed, the
timing criterion was tested on complete data for the first time and attributed real faults
(+27.8% / -16.7%), because a seizing pump is synchronous inside its own scope. The
blast-radius discriminator (ADR-038) then traded reduction for recall exactly as designed:
on byte-identical records at 24 channels it halved the recall loss, 6.7% to 3.3%, for 4.4
points of reduction. **NFR-8 missed for the fifth time -- +18.9% / -3.3% at 12 channels,
+3.6% / -3.3% at 24 -- with the recall half met for the first time.** Seven measurements are
published side by side in `docs/EVALUATION.md` sections 3.4 to 3.11.

**In flight:** nothing.

**Then, in rough priority order:**

1. Serve the adapter and wire it behind the `Planner` protocol, which is what reverses D-6.
   The gate, the sandbox and the runbook grounding are unchanged either way; what needs
   measuring is served latency, since 15.8 s p50 on 4-bit sequential generation is not it.
2. Re-run the benchmark without truncation, so the 56 late-onset series are not excluded
   (est. 4+ h of CPU).
3. Multi-hour soak for NFR-6. Must run after chaos and scale.
4. Run the explainer against Claude once a key exists: NFR-2, the token cost per explanation, and
   a judge for explanation quality are all one key away and all currently unmeasured (C-2).
5. Apply the deployment layer to a real cluster. Everything in `k8s/` and `terraform/` is
   validated and none of it has run; `docs/EVALUATION.md` section 9.3 is the list of what
   that leaves unproven. Needs a machine with ~16 GB free, which the reference laptop is not.
6. README polish and the <60 s demo -- the remaining half of the Phase 6 gate.
7. The React dashboard. ClickHouse and Iceberg are done.

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
