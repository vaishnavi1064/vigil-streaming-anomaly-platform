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
| **Detection latency** | Hot-path rolling z-score **0.493 ms p99** over 352 windows against a 250 ms budget — clear by roughly 500x, so NFR-1 is met and is plainly not what binds this design. Chronos-Bolt-tiny runs off that path at 34.8 ms p99 | [EVALUATION](docs/EVALUATION.md) 5.1b |
| **Throughput** | Producer **76,556 ev/s** blast; consumer plateau **170,414 readings/s** at 3–6 consumers over 6 partitions. NFR-4 (20,000/s) met | [SCALE](docs/SCALE.md) |
| **Scaling, honestly** | NFR-5 asked for near-linear and **did not get it**: 6 consumers buy **1.80x**, efficiency falls to 30%, and the plateau is at 3 — half the partition count, so partitions are not the bind | [SCALE](docs/SCALE.md) 2–3 |
| **QLoRA tool-calling planner** | **97.7% exact-match (293/300)** on held-out hard cases, against **20.0%** for the rules handed licences they could not parse and **0.0%** for the rules as deployed. Forbidden actions **1.0% (3/300)** against the baseline's 61.3% | [EVALUATION](docs/EVALUATION.md) 6 |
| **Detection benchmark, including the loss** | On 144 of 200 TSB-AD-M series the **z-score baseline beats** Chronos-Bolt-tiny — median AUC-PR **0.198 vs 0.152**, head-to-head **78 / 56 / 10 ties** — at **141x less compute** (59 s vs 8,312 s). Chronos wins where normal is structured and non-stationary (Exathlon 19–8) and loses on sharp excursions against a flat baseline (SVDB 21–1) | [EVALUATION](docs/EVALUATION.md) 4 |
| **Serving store and event lake** | ClickHouse for readings and window scores; Iceberg on MinIO as the durable record. Both **effectively-once**: the lake sink was restarted with `--from-beginning` and **read 0 records**, because the Kafka offsets live in the Iceberg snapshot that committed the rows. 0 duplicate rows | [EVALUATION](docs/EVALUATION.md) 8 |
| **Reconciliation against the lake** | Ledger and lake agreed on **6/6 channels** — one count from streaming the Kafka log, one from reading Parquet off object storage, sharing no code and no state | [EVALUATION](docs/EVALUATION.md) 8.4 |
| **The core contribution, which still misses its target** | Context-conditioned detection measured **eleven times**. Best *pair*: **+35.4% false-page reduction at −3.3% recall** (24 channels). NFR-8 wanted ≥40% reduction at ≈0 recall loss, and the two halves have now been cleared **separately and never together** — a later run reaches +63.6% and destroys recall doing it. **Missed all eleven times, and published each time** | [EVALUATION](docs/EVALUATION.md) 3 |

**752 tests** (78 integration, against real Kafka, Postgres, ClickHouse and Iceberg containers).

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
python -m pytest                        # 752 tests; integration ones need compose up
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

Not the stack. The stack is a list anyone can copy. Two things here are not.

### 1. Correctness that is demonstrated, not asserted

Most streaming projects claim exactly-once. This one was made to prove it while being broken.

- **Exactly-once, fault-tested.** The Flink TaskManager was SIGKILLed mid-checkpoint and held
  down for 60 s. 58/58 health samples went unserviceable, the job restored from checkpoint 5
  and recovered in **14.9 s**, and a `read_committed` consumer then counted **328 distinct
  window scores with 0 duplicates**. The first attempt at this test *passed in 0.1 s* against a
  job that had never redeployed — Flink reports a dead job healthy for about 50 s — so the
  check now requires the vertices to have actually moved before it will believe a recovery.
- **Zero drift over four hours.** **5,749,412 readings**, 240.0 minutes, drift 0 / missing 0 /
  duplicates 0 / reordered 0 at all sixteen checkpoints. Identity is a per-channel sequence
  number assigned at the edge, because counting messages cannot tell "processed a million
  events" from "processed one event a million times".
- **Five fault modes, each proven to have hurt.** Broker kill, broker pause, network partition,
  consumer kill, Flink TaskManager kill — each with 20/20 samples unserviceable during the
  fault and each recovering inside a 60 s budget, worst case 25.1 s. A chaos test that cannot
  show it disrupted anything is a test of nothing.
- **Two audits that share no code.** The reconciliation ledger streams the Kafka log; the lake
  audit reads Parquet off object storage. They agreed on **6/6 channels**.

**The bug that makes the point.** `(channel, seq)` turned out not to be unique across producer
restarts, so two genuinely different readings collided — and ClickHouse's `ReplacingMergeTree`
would have deleted the older one at merge time and reported it as a successful deduplication.
**Silent data loss wearing the costume of a correctness feature**, found by running the thing
against real data rather than by reading it. Identity is now `(channel, seq, event_ts)`
([ADR-046](docs/DECISIONS.md)), and it narrowed a claim `CORRECTNESS.md` had been making
without qualification. The ClickHouse rollup double-counting a replayed batch was found the
same way.

### 2. A research-grade investigation that ends in an honest negative

The core claim — that conditioning a detector on operational context cuts false pages — was
measured **eleven times across seven mechanisms**, and **missed its target every time**. That
is the headline, and the reason it is a strength rather than an apology is that the target is
hard for reasons the literature already documents.

**Unsupervised false-positive reduction on unlabelled streaming data is an open problem.**
Published methods buy their reduction with one of four things, and this system has ruled out
all four by premise:

| What the method needs | Representative work |
|---|---|
| Labelled true and false positives | FADFPM, a two-stage classifier re-judging the detector's own output ([Information Fusion 100, 2023](https://www.sciencedirect.com/science/article/pii/S1566253523002737)) |
| Certified anomaly-free training data | FAI ([Qiu et al., *Sensors*, 2023](https://pmc.ncbi.nlm.nih.gov/articles/PMC10708712/)) |
| A human confirming alerts | Active Anomaly Discovery ([Das et al.](https://www.semanticscholar.org/paper/Incorporating-Expert-Feedback-into-Active-Anomaly-Das-Wong/54d9848e84807c15b49e77b5fac72e48dcf01059)) |
| A recall penalty for requiring agreement | ReRe, whose dual-LSTM exists to cut RePAD's false positives ([Lee et al.](https://arxiv.org/pdf/2004.02319)) |

So I implemented the label-free mechanisms the field actually uses — seven of them, in three
families — and **reproduced the field's known limitation rather than a clean solution**:

- **External-cause conditioning** (timing, synchrony, watermarked evidence, topology and blast
  radius). Best pair **+18.9% / −3.3%**. B-6 then showed the 40% target was *arithmetically
  unreachable* on one run — 75 of 112 false pages overlapped no injected excursion at all, a
  ceiling of 33% before the policy decided anything.
- **Detector agreement** — ReRe's mechanism. Best pair **+35.4% / −3.3%**, and the closest
  anything came. It also reached **52% of the pages the arithmetic had excluded**, so the
  denominator never had to move.
- **Temporal persistence.** **+50.0%** and **+63.6%** — it cleared the bar, and it cleared it
  by suppressing real anomalies.

**Two of the seven mechanisms cleared the reduction target. Both did it by suppressing real
anomalies, and the adversarial benchmark caught both** — the generator schedules faults inside
*quiet* deploy windows, where there is no artifact to attribute anything to, so a blanket
suppressor loses exactly that population and cannot hide it. v1 lost all of it; the persistence
run broke it 7/7 to 6/7. A benchmark that could not tell correct attribution from blanket
suppression would have called both a success.

**One finding departs from the field's framing.** The expected cost of agreement-based
suppression is a recall penalty — the familiar unanimous-versus-majority voting trade. **It did
not appear here.** Agreement cost **+0.0%** incident recall at 12 channels and *halved* the
recall loss at 24, because it also ran protectively and pulled back ten deploy attributions of
which seven overlapped real faults. What bound it instead was **shared failure modes, measured
rather than assumed**: the two detectors agreed on **77 of 145** episodes, and the surviving
false pages are ones *both* saw — both correct that the signal moved, neither able to see that
it moved for no reason. That says what a third detector would have to be unlike.

**What I refused.** Redefining NFR-8's denominator to the attributable subset would have turned
the miss into a pass in one edit; the option is written down, no recommendation was offered,
and the target stands as missed. The two-stage labelled classifier has the best published
record on this exact problem and was **not** built, because it converts a zero-label streaming
system into a supervised one and the zero-label premise *is* the system.

**And the measurement discipline that makes the rest of it readable.** A late run showed that
the same policy on the same seed scores **+11.1% and +18.9%** in two runs — deploy timing is
anchored to wall clock, so window boundaries fall differently and the run-to-run spread is
about **8 points** of false-positive reduction (G-19). Every v6 result is therefore reported as
a **within-run ablation** on byte-identical records, and never as a single number: always a
pair, reduction beside recall, broken out inside context windows, outside them, and in the
quiet windows. Full treatment with citations in [EVALUATION](docs/EVALUATION.md) section 3.17;
the run-by-run path is sections 3.4 to 3.16.

### The rest of the judgement calls

**Honest benchmarking against my own thesis.** The project is built around a foundation-model
detector. The benchmark says a rolling z-score beats it on this corpus at 141x less compute,
and that finding is in the results table above rather than buried. Where Chronos *does* win is
named too, because "I measured where the trendy method is the wrong tool" is a stronger result
than an unexamined win.

**Refusing to fake the deployment layer.** Phase 6 produced validated Kubernetes manifests,
Terraform and monitoring — and three deliberate absences:

- **No agent Deployment.** There is no long-running agent in this repo; `RemediationAgent` is
  a library. Writing a daemon to deploy would be building a new component under the heading
  of deploying an existing one. What ships is a CronJob for the quality gate, which is real.
- **No cloud provider in Terraform.** An `aws_eks_cluster` block would be the most
  impressive-looking file here and the least honest — nothing in it would ever have run.
- **No probes on the four consumers.** `exec: true` is decoration and probing Kafka turns a
  broker outage into a crash-loop. The gap is documented in three places instead.

**Recording defects against my own results.** v2's synchrony test was *invalid* — episode
start times were window boundaries quantised to a 10 s slide, so a 5 s tolerance could only
ever match an exact tie — and that was filed as a blocker against a number I had already
published, not quietly fixed. The same happened to G-16, where four published runs reported
`isolated=0` because the verdict field could not carry the conclusion it was read as carrying.

---

## What is not true yet

- **NFR-8 is missed.** Eleven measurements, best pair **+35.4% / −3.3%** against a 40% / ≈0
  target. The two halves have been cleared separately and never together. For the best pair the
  remaining gap is about six false pages, and the reason they survive is stated: **both**
  detectors see them, both are right that the signal moved, and neither can see that it moved
  for no reason.
- **Cross-run comparisons in this repo carry about 8 points of noise.** Measured, not assumed:
  the same policy on the same seed scores +11.1% and +18.9% in two runs, because deploy timing
  is anchored to wall clock (G-19). Every v6 claim is stated as a within-run ablation for that
  reason.
- **The Kubernetes layer has never run.** 25 resources, all schema-valid, **zero pods ever
  scheduled**. [DEPLOYMENT](docs/DEPLOYMENT.md) section 6 lists what that leaves unproven.
- **The VLM explainer has never called a model.** Built against Claude with 40 tests, none of
  which touch the network, because no `ANTHROPIC_API_KEY` exists here. NFR-2's 5 s budget is
  unmeasured.
- **CI has never run on a runner.** No remote; every step passes locally.
- **Single broker, replication factor 1.** No leader election, no ISR shrink. Recovery times
  do not project to a cluster.

The full list is [BLOCKERS](docs/BLOCKERS.md) — 19 recorded gaps, 16 still open, kept because a
document that omits them would flatter itself.

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
| [DECISIONS](docs/DECISIONS.md) | 53 ADRs: what, why, what was rejected |
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
