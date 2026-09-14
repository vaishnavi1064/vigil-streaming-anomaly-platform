# Vigil

Real-time anomaly detection that knows the difference between a broken sensor and a
deploy — on a streaming backbone that can prove it did not lose your data.

![Architecture: ingestion to Kafka to Flink to detection to conditioning, with reconciliation
feeding the conditioning step, and serving, dashboard and agent hanging off
it.](docs/architecture.svg)

---

## Results

Every number below was produced by a command in this repository on the hardware named at the
bottom. Where a target was missed it says so, and the misses are the interesting part.

| What | Measured | Where |
|---|---|---|
| **Exactly-once through Flink, fault-tested** | TaskManager SIGKILLed mid-checkpoint and held down 60 s: 58/58 samples unhealthy, restored from checkpoint 5, recovered in **14.9 s**, and a `read_committed` consumer saw **328 distinct window scores with 0 duplicates** | [CHAOS](docs/CHAOS.md) 2.1 |
| **Zero reconciliation drift over four hours** | **5,749,412 readings** across 12 channels over **240.0 minutes**: drift 0, missing 0, duplicates 0, reordered 0, at all sixteen checkpoints | [CORRECTNESS](docs/CORRECTNESS.md) 4a |
| **Chaos** | **5 fault modes** — broker kill, broker pause, network partition, consumer kill, Flink TaskManager kill — each proving it disrupted something (20/20 samples unserviceable) and each recovering inside a 60 s budget. Worst recovery 25.1 s | [CHAOS](docs/CHAOS.md) |
| **Throughput** | Producer **76,556 ev/s** blast; consumer plateau **170,414 readings/s** at 3–6 consumers over 6 partitions. NFR-4 (20,000/s) met | [SCALE](docs/SCALE.md) |
| **Scaling, honestly** | NFR-5 asked for near-linear and **did not get it**: 6 consumers buy **1.80x**, efficiency falls to 30%, and the plateau is at 3 — half the partition count, so partitions are not the bind | [SCALE](docs/SCALE.md) 2–3 |
| **QLoRA tool-calling planner** | **97.7% exact-match (293/300)** on held-out hard cases, against **20.0%** for the rules handed licences they could not parse and **0.0%** for the rules as deployed. Forbidden actions **1.0% (3/300)** against the baseline's 61.3% | [EVALUATION](docs/EVALUATION.md) 6 |
| **Detection benchmark, including the loss** | On 144 of 200 TSB-AD-M series the **z-score baseline beats** Chronos-Bolt-tiny — median AUC-PR **0.198 vs 0.152**, head-to-head **78 / 56 / 10 ties** — at **141x less compute** (59 s vs 8,312 s). Chronos wins where normal is structured and non-stationary (Exathlon 19–8) and loses on sharp excursions against a flat baseline (SVDB 21–1) | [EVALUATION](docs/EVALUATION.md) 4 |
| **Serving store and event lake** | ClickHouse for readings and window scores; Iceberg on MinIO as the durable record. Both **effectively-once**: the lake sink was restarted with `--from-beginning` and **read 0 records**, because the Kafka offsets live in the Iceberg snapshot that committed the rows. 0 duplicate rows | [EVALUATION](docs/EVALUATION.md) 8 |
| **Reconciliation against the lake** | Ledger and lake agreed on **6/6 channels** — one count from streaming the Kafka log, one from reading Parquet off object storage, sharing no code and no state | [EVALUATION](docs/EVALUATION.md) 8.4 |
| **The core contribution, which missed its target** | Context-conditioned detection measured **seven times**. Best run: **+18.9% false-page reduction at −3.3% recall**. NFR-8 wanted ≥40% reduction at ≈0 recall loss. **Missed, five times, and published each time** | [EVALUATION](docs/EVALUATION.md) 3 |

**697 tests** (78 integration, against real Kafka, Postgres, ClickHouse and Iceberg containers).

---

## The dashboard

Live readings, the detector's episodes drawn on top of them, and throughput ticking — all
read from the same stored data every other panel uses. Inline SVG, no charting library.

![The Vigil dashboard in light mode: KPI tiles, a live stream chart with anomaly markers,
detector comparison, the reconciliation panel showing zero drift, and the episodes
table.](docs/images/dashboard-live-light.png)

A level shift caught as it happens — the teal channel steps up exactly at the dashed onset
rule, inside the shaded episode band:

![Close-up of the live chart: six channels, a shaded episode band, and a red marker on the
channel that moved.](docs/images/dashboard-live-chart-dark.png)

<!-- DEMO GIF PLACEHOLDER: replace this line with the <60s demo recording. -->

Dark mode: [full page](docs/images/dashboard-live-dark.png) ·
[chart close-up](docs/images/dashboard-live-chart-light.png)

---

## Quickstart

```bash
git clone <this repo> && cd vigil
cp .env.example .env          # fill in the blanks; nothing has a default
python -m venv .venv && .venv/Scripts/pip install -e ".[dev,explain,lake]"

docker compose up -d          # Kafka, Postgres, ClickHouse, MinIO. 4/4 healthy in ~8 s
```

Then the 60-second version — produce, detect, reconcile, remediate, and see it:

```bash
python demo.py                                       # one command, end to end
```

Or drive it yourself, one process per concern:

```bash
python loadgen.py --rate 350 --duration 300 --channels 6 --scenario   # synthetic source
python detector.py --conditioning                                     # windows, scores, episodes
python warehouse.py --no-scores                                       # readings -> ClickHouse
python lake.py                                                        # readings -> Iceberg
python reconciler.py --audit-lake                                     # prove nothing was lost
python -m uvicorn vigil.api:app --port 8000                           # dashboard
```

`docker compose up` does **not** start Flink — its 2.5 GB does not fit beside the rest on an
8 GB Docker VM, which is the one combination this stack cannot run at once. Bring it up
deliberately with `docker compose --profile flink up -d --build`.

Everything else:

```bash
python -m pytest                        # 697 tests; integration ones need compose up
python -m pytest -m "not integration"   # unit only
python chaos.py                         # the fault suite
python evaluate.py                      # the paired conditioning measurement
python benchmark.py                     # detector vs baseline on TSB-AD-M
python scripts/validate_deploy.py       # k8s, terraform and monitoring configs
```

---

## How it works

**Ingestion.** A synthetic generator or a live public MQTT solar-fleet feed. Every reading
carries a per-channel sequence number assigned at the boundary — that number is the identity
everything downstream checks invariants against, because counting messages cannot tell
"processed a million events" from "processed one event a million times". The feed publishes
no history API, so the edge guarantee is **at-most-once** and says so (ADR-011).

**Kafka.** Keyed by channel, so a channel's readings land in one partition and stay ordered.
Topics are created explicitly; auto-create is off, because it turns a topic-name typo into a
silently empty stream.

**Flink.** Event-time windows with per-channel watermarks, RocksDB state, and **exactly-once
via two-phase commit** — source offsets in the checkpoint, sink writes in a Kafka transaction
committed on checkpoint completion. Verified by killing the TaskManager mid-checkpoint and
counting duplicates on the other side: zero.

**Detection.** Two detectors over the same windows: a rolling z-score on the hot path
(**0.493 ms p99** against a 250 ms budget) and a zero-shot Chronos-Bolt foundation model
off it (34.8 ms p99, not bound by that budget). Consecutive flagged
windows merge into one episode, because an operator is paged once per incident.

**Conditioning — the contribution.** The reconciliation harness emits a per-window pipeline
health signal, and deploy markers arrive on the same wire. An episode overlapping a proven
disturbance is *attributed* to it rather than paged. The policy can only ever refuse: it
removes attributions and never adds them, and it fails open when the signal is missing
(ADR-007), because missing context must not hide a real anomaly.

**Agent.** Diagnoser → Planner → Safety Gate → Executor on a sandbox, grounded in runbook
retrieval. The gate never reads the planner's rationale and has the last word. The planner
is a QLoRA fine-tune of Qwen2.5-7B; the gate is unchanged either way, which is the point of
putting the authority in the gate.

**Storage.** Postgres holds episodes and app state, ClickHouse serves readings and window
scores, Iceberg on MinIO is the durable record and what reconciliation audits against.
Nothing is mirrored between them, so no query has to decide which copy to believe.

---

## What makes this senior

Not the stack. The stack is a list anyone can copy. These are the parts that took judgement.

**A five-run investigation that ended in a negative result, published.** The core claim —
that conditioning on pipeline health cuts false pages — was measured seven times and
**missed its target every time**. The path is in [EVALUATION](docs/EVALUATION.md) section 3:

- **v1** suppressed by co-occurrence: +60.9% reduction, −36.7% recall. It scored well by
  muting real faults during deploys, which is the exact cheat the generator schedules a
  quiet-deploy population to catch. It got caught.
- **v2** required synchrony, and the test was **invalid**: episode start times are window
  boundaries quantised to a 10 s slide, so a 5 s tolerance could only ever match an exact
  tie. Recorded as a blocker against my own result.
- **v3** fixed the onset (ADR-035) and the numbers moved the right way — and still missed.
- **v4** found the root cause was **distributed-systems, not statistical**: the corroboration
  test was deciding before its evidence arrived (G-7), and recording a verdict it had not
  reached (G-16), which is why four published runs all reported `isolated=0`.
- **v4a** was the control — same policy, evidence actually present — and made things *worse*
  in the expected direction, which is what ADR-038 was built to answer.

Then the part I am most willing to defend: **B-6 showed the 40% target was arithmetically
unreachable on that run.** 75 of 112 false pages overlapped no injected artifact at all, so
the ceiling was 37/112 = **33%** even for a perfect discriminator. The obvious move was to
redefine the denominator to the attributable subset, which would have made the headline pass.
I wrote the option down, recommended nothing, and **left the target as missed** — changing a
metric after five failures to hit it needs a better reason than "the old one was
unflattering".

**Honest benchmarking against my own thesis.** The project is built around a foundation-model
detector. The benchmark says a rolling z-score beats it on this corpus at 141x less compute,
and that finding is in the README above rather than buried. Where Chronos *does* win is named
too, because "I measured where the trendy method is the wrong tool" is a stronger result than
an unexamined win.

**Refusing to fake the deployment layer.** Phase 6 produced validated Kubernetes manifests,
Terraform and monitoring — and three deliberate absences:

- **No agent Deployment.** There is no long-running agent in this repo; `RemediationAgent` is
  a library. Writing a daemon to deploy would be building a new component under the heading
  of deploying an existing one. What ships is a CronJob for the quality gate, which is real.
- **No cloud provider in Terraform.** An `aws_eks_cluster` block would be the most
  impressive-looking file here and the least honest — nothing in it would ever have run.
- **No probes on the four consumers.** `exec: true` is decoration and probing Kafka turns a
  broker outage into a crash-loop. The gap is documented in three places instead.

**Bugs found by running it, not by reading it.** The ClickHouse rollup silently double-counted
a replayed batch, because a materialized view never sees the `ReplacingMergeTree` dedupe that
happens later at merge time. And `(channel, seq)` turned out not to be unique across producer
restarts — which would have had ClickHouse delete a real reading at merge time and report it
as a successful dedupe. Both are [ADR-046](docs/DECISIONS.md) and both narrowed a claim that
`CORRECTNESS.md` had been stating without qualification.

---

## What is not true yet

- **NFR-8 is missed.** Seven measurements, best +18.9% / −3.3% against a 40% / ≈0 target.
- **The Kubernetes layer has never run.** 25 resources, all schema-valid, **zero pods ever
  scheduled**. [DEPLOYMENT](docs/DEPLOYMENT.md) section 6 lists what that leaves unproven.
- **The VLM explainer has never called a model.** Built against Claude with 40 tests, none of
  which touch the network, because no `ANTHROPIC_API_KEY` exists here. NFR-2's 5 s budget is
  unmeasured.
- **CI has never run on a runner.** No remote; every step passes locally.
- **Single broker, replication factor 1.** No leader election, no ISR shrink. Recovery times
  do not project to a cluster.

The full list is [BLOCKERS](docs/BLOCKERS.md) — 16 known gaps, kept because a document that
omits them would flatter itself.

---

## Documentation

| Document | What it holds |
|---|---|
| [PROJECT_PLAN](docs/PROJECT_PLAN.md) | The spec this was built against |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Components, data flow, both deployment topologies |
| [CORRECTNESS](docs/CORRECTNESS.md) | The guarantee per boundary, and what is *not* covered |
| [EVALUATION](docs/EVALUATION.md) | Every measurement, including the losses |
| [CHAOS](docs/CHAOS.md) | Fault injection and recovery evidence |
| [SCALE](docs/SCALE.md) | Throughput and the parallelism curve |
| [DECISIONS](docs/DECISIONS.md) | 49 ADRs: what, why, what was rejected |
| [BLOCKERS](docs/BLOCKERS.md) | Open questions, deferrals, and every known gap |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | How to apply the K8s layer, and what is unverified |
| [PROGRESS](docs/PROGRESS.md) | Status board and a dated work log |

---

## Honesty rules held throughout

- A number appears only after a command produced it, with the hardware named.
- A missed target is reported as missed, never quietly re-scoped afterwards.
- Losses get the same prominence as wins.
- "Not yet measured" is used rather than an estimate.
- If a thing is not built, it is absent — not stubbed and described as working.

**Reference hardware for every number here:** Intel Core i7-12650H (10 cores / 16 threads),
15.6 GB RAM, RTX 3050 Ti Laptop (4 GB VRAM), Windows 11, Docker Desktop with 8.1 GB allocated,
Python 3.12. One exception, named where it appears: the QLoRA planner trained and was scored
on a Northeastern Explorer V100-SXM2-32GB, because a 7B adapter does not fit in 4 GB.
