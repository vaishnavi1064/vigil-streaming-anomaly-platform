# Evaluation

> Methodology first, results as they are measured. Nothing in this file is a projection: a
> number appears here only after it has been produced by a command recorded alongside it.
> Sections marked **not yet measured** are honest placeholders, not omissions.

**Reference hardware for every number in this file:** Intel Core i7-12650H (10 cores /
16 threads), 15.6 GB RAM, NVIDIA RTX 3050 Ti Laptop (4 GB VRAM), Windows 11, Docker Desktop
with 16 CPU / 8.1 GB allocated to the VM, Python 3.12.10.

---

## 1. What is being evaluated, and against what

Three separate questions, deliberately not blended into one score:

| # | Question | Data | Compared against |
|---|---|---|---|
| Q1 | Does context-conditioned detection reduce false positives without losing real anomalies? | Adversarial scenario runs (section 3) + chaos-injected pipeline faults | The **unconditioned** baseline on identical data, via a shadow pass |
| Q2 | Is the foundation-model detector actually better than a cheap baseline? | TSB-AD-M, 200 labelled multivariate series | Rolling z-score baseline |
| Q3 | What does detection cost in latency and throughput? | Synthetic load harness | The NFR budgets in `REQUIREMENTS.md` |

Q1 is the core contribution. Q2 is the honest-benchmark obligation. Q3 is the engineering budget.

---

## 2. Metrics, and the ones deliberately refused

**Used.**
- Point-wise **AUC-PR** — anomalies are well under 1% of points, so precision-recall is the
  informative curve.
- **VUS-PR / VUS-ROC** — threshold-independent and tolerant of small boundary offsets, which is
  what a windowed detector actually produces.
- **Precision / recall / F1 at a matched alarm budget** — the only fair way to compare two
  detectors that emit differently-scaled scores: fix the number of alarms an operator would
  receive, then ask who spent them better.
- **Incident-level counting** — a run of consecutive alarms on one channel counts **once**. An
  operator is paged once per incident; counting each window separately lets a chatty detector
  inflate both its false-positive count and its apparent reduction.

**Refused, with reasons.**
- **Point-adjusted F1.** Marks an entire ground-truth range as detected if the detector fires
  once anywhere inside it, which lets a near-random scorer post a high F1. That inflation is the
  specific problem TSB-AD was built to expose; adopting the benchmark and then using the metric
  it exists to discredit would be self-defeating (ADR-013). Our numbers will therefore look
  *worse* than papers that point-adjust and are not directly comparable to them.
- **Accuracy and ROC-AUC.** Dominated by the negative class at these base rates.
- **False-positive reduction on its own.** Maximised by suppressing everything. Always paired
  with recall (ADR-016).

---

## 3. Q1 — the core claim, and how it is made falsifiable

### 3.1 The cheat this design exists to catch

The easy way to report a large false-positive reduction is to mute every alarm inside a deploy
or fault window. A naive evaluation cannot tell that apart from correctly attributing an
artifact to its cause. So the generator schedules four populations on purpose (ADR-015):

| Population | What it is | What it proves |
|---|---|---|
| 1 | Deploys that **do** perturb telemetry | The legitimate suppression target |
| 2 | Deploys that perturb **nothing** (default 35%) | Suppression fired by the marker alone has nothing to hide behind |
| 3 | Real faults **outside** every window (control) | Detection is unaffected away from context |
| 4 | Real faults **inside** windows, including quiet ones (default 40% of faults) | **Must still be detected.** A blanket suppressor loses exactly this population |

Scope is enforced as well as timing: a deploy touches a subset of channels, an artifact is only
placed on a channel the deploy actually touched, and population 4 lands on in-scope channels.
A policy that suppresses fleet-wide on any marker over-suppresses and is caught.

Markers are published as the run reaches them, never dumped up front — a policy able to see the
whole future would be solving an easier problem than the real one.

### 3.2 Protocol

1. **Shadow pass (read-only).** Run the detector with conditioning **off** over the scenario
   stream. This is the baseline, on byte-identical data.
2. **Conditioned pass.** Same stream, same seed, conditioning **on**.
3. Score both against the ground-truth plan written by `--write-plan` *before* either pass.
4. Report the full table below, including any regression.

```
python loadgen.py --rate 400 --duration 900 --channels 8 --scenario \
    --write-plan docs/results/scenario-plan.json
```

### 3.3 Target

Both halves must hold (NFR-8 / ADR-016):

- **≥ 40%** false-positive reduction during deploy and pipeline windows, **and**
- **≈ 0** recall loss on real faults, broken out inside vs. outside context windows.

### 3.4 Results

**Not yet measured.** The conditioning policy lands in Phase 3; the generator, the context
topic and the ground-truth plan are in place as of Phase 1. This table is the shape the result
will take, and it will be filled from a recorded command:

| Measure | Unconditioned (shadow) | Conditioned | Delta |
|---|---|---|---|
| False positives during perturbing-deploy windows | — | — | — |
| False positives during quiet-deploy windows | — | — | — |
| Recall, faults **outside** context windows | — | — | — |
| Recall, faults **inside** context windows | — | — | — |
| Recall, faults inside **quiet** deploy windows | — | — | — |
| Precision (incident-level) | — | — | — |
| VUS-PR | — | — | — |

The quiet-deploy and inside-window rows are the ones that fail loudly under blanket suppression.

---

## 4. Q2 — detector vs. baseline on TSB-AD-M

**Corpus.** TSB-AD-M, the multivariate track: 200 labelled series, 2.4 GB extracted.
Fetched by `python scripts/fetch_tsb_ad.py`; archive sha256
`7de86ac27f30eeb48d833bb061055670e3f3de07defd995cf2bd5db10ccc9a0d`. Git-ignored.

Each file is a CSV of numeric feature columns plus a per-timestamp `Label` column (0/1); the
filename encodes the train split and the first anomaly index.

**Protocol.** The same detector code that runs on the stream is run over each series offline.
Both detectors are scored per series; results are reported per series and aggregated, never as
a single headline mean that hides the spread.

**Reporting obligation.** Where the foundation model *loses* to the z-score baseline is
reported with the same prominence as where it wins, and the boundary between the two regimes is
named. Recent work is openly divided on whether time-series foundation models beat simple
methods at anomaly detection (PROJECT_PLAN section 15.1); "I measured where the trendy method is
the wrong tool" is a stronger finding than an unexamined win.

**Results: not yet measured.** Phase 5.

---

## 5. Q3 — latency and throughput

### 5.1 Measured so far

| Measurement | Value | How | When |
|---|---|---|---|
| Producer throughput, blast mode, single process | **76,556 events/s** sustained over 20 s, 0 delivery failures | `python loadgen.py --rate 0 --duration 20 --channels 16` | Phase 1 |
| Producer throughput, target-rate mode | **1,999 events/s** against a 2,000 target | `python loadgen.py --rate 2000 --duration 10` | Phase 1 |
| Live bridge throughput | **242 readings/s** from 1,387 MQTT messages over 45 s, 336 channels, 0 gaps | `python mqtt_bridge.py --topic inverters --duration 45` | Phase 1 |
| Scenario mode | 60,000 readings + 12 context markers over 150 s at a 400 ev/s target, 0 failures | `python loadgen.py --rate 400 --duration 150 --scenario` | Phase 1 |

The producer is not the bottleneck: it clears the 20,000 events/s NFR-4 target by 3.8x on its
own. The consumer side, the throughput-vs-parallelism curve and backpressure behaviour are
Phase 2 and are **not yet measured** — no claim is made about end-to-end throughput here.

The live feed's own rate (~242 readings/s on `inverters`, ~4,000/s achievable on `strings`) is
far below the throughput target. That is a property of the source, not of the pipeline, and is
exactly why the synthetic harness is retained (ADR-014).

### 5.2 Latency budgets

Split deliberately (ADR-017):

- **Hot path, ≤ 250 ms p99.** The detector every window must clear before an episode is raised.
  The z-score baseline lives here.
- **Off critical path, ≤ 5 s.** The foundation model, batched. Its verdict enriches an episode
  the hot path already raised; if it is slow or down, detection is unaffected.

Pinning the 250 ms budget to a foundation model on this laptop would not be honest: a
200M-class model is roughly 200–500 ms per batch on this CPU and effectively wants a GPU, and
this machine has 4 GB of VRAM. The model is chosen to fit the budget rather than the budget
stretched to fit the model.

**Per-detector latency: not yet measured.** Phase 1 (baseline) and Phase 2 (final targets).

---

## 6. Honesty rules held in this document

- Every number states the hardware and the command that produced it.
- A target that is missed is reported as missed, not quietly re-scoped afterwards. Revisions to
  targets are made *before* measurement and carry an ADR (ADR-016, ADR-017).
- Losses are reported with the same prominence as wins.
- "Not yet measured" is used rather than an estimate.
