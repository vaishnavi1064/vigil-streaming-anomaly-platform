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
- **Range-tolerant AUC-PR / AUC-ROC** — threshold-independent and tolerant of small boundary
  offsets, which is what a windowed detector actually produces. **VUS-style, and deliberately
  not called VUS-PR:** tolerance is applied here by dilating the label set, so a detector
  firing exactly on the labelled points is penalised at higher tolerances for not covering
  the buffer (a perfect point-detector scores about 0.77, not 1.0). Published VUS weights
  the buffer region instead. Ours is internally consistent, which is all a comparison
  between two of our own detectors needs; it is **not** comparable to published VUS numbers
  and no such comparison is made.
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

Run:

```
python evaluate.py --duration 900 --rate 400 --channels 12 \
    --deploys-per-hour 60 --faults-per-hour 120
```

360,000 readings over 15 minutes on 12 channels. Ground truth, fixed before either pass ran:
**30 real faults** (13 inside context windows, 17 outside, 3 inside *quiet* windows),
**56 injected artifacts**, **14 deploy windows**. Both passes replayed the identical topic
and recorded the identical 79 episodes; only the conditioning decision differed.

#### v1 -- corroboration by co-occurrence. FAILED.

| Measure | Shadow | Conditioned | Delta |
|---|---|---|---|
| Pages raised | 79 | 40 | -39 |
| False pages (artifact + unexplained) | 64 | 25 | **-39** |
| of which artifact-driven | 42 | 7 | -35 |
| Recall, all real faults | 83.3% (25/30) | 46.7% (14/30) | **-36.7%** |
| Recall, faults **outside** windows | 70.6% (12/17) | 64.7% (11/17) | -5.9% |
| Recall, faults **inside** windows | 100.0% (13/13) | 23.1% (3/13) | **-76.9%** |
| Recall, faults in **quiet** windows | 100.0% (3/3) | 0.0% (0/3) | **-100.0%** |
| Precision (incident-level) | 19.0% | 37.5% | +18.5% |

**false-positive reduction +60.9% (target >= 40%, met) -- recall loss +36.7% (tolerance
<= 5%, missed). NFR-8 NOT MET.**

The quiet-window row is the whole finding. During a quiet deploy there is no artifact at
all, so there is nothing for a correct policy to attribute anything *to* -- and this one
attributed every real fault there. That population exists precisely to catch blanket
suppression (ADR-015), and it caught ours.

The verdict breakdown says why: `corroborated=39, implausible=40, isolated=0`. The
corroboration test **never once** concluded a channel had moved alone. Asking "were this
channel's in-scope siblings also flagged in this window" is not discriminating at this
density: with 56 artifacts and 30 faults across 12 channels, and 14 deploys whose windows
overlap to cover nearly the whole run, two in-scope channels are flagged in almost any
30-second bucket by coincidence.

The pipeline half of the policy worked. Forty episodes overlapping a pipeline event that
had lost nothing were raised as `implausible` rather than attributed -- the mechanism check
doing its job.

Raw result: `docs/results/paired-evaluation-v1-corroboration-only.json`.

#### v2 -- corroboration requires synchrony. ALSO FAILED, and for a reason that invalidates the test.

The diagnosis pointed at a physical distinction rather than a threshold to tune. A deploy
artifact hits the channels it touched **at the same instant** -- a collector restart blips
them together. Two independent faults landing in the same 30-second bucket are not
synchronised to the second. So corroboration now asks whether in-scope siblings *started*
within a tolerance far tighter than a window (default 5 s), and the index records episode
start times rather than window buckets.

Re-measured on the **same scenario at the same density and the same seed**, so the only
variable is the policy.

Run `753ddb71`, same command, same seed, 360,014 readings, identical ground truth
(30 real faults, 56 artifacts, 14 deploy windows). Reconciliation reported zero drift and
31 health windows, none disturbed.

| Measure | Shadow | Conditioned | Delta |
|---|---|---|---|
| Episodes recorded | 78 | 78 | |
| Pages raised | 78 | 72 | -6 |
| False pages (artifact + unexplained) | 67 | 61 | **-6** |
| of which artifact-driven | 44 | 39 | -5 |
| Recall, all real faults | 83.3% (25/30) | 73.3% (22/30) | **-10.0%** |
| Recall, faults **outside** windows | 70.6% (12/17) | 58.8% (10/17) | -11.8% |
| Recall, faults **inside** windows | 100.0% (13/13) | 92.3% (12/13) | -7.7% |
| Recall, faults in **quiet** windows | 100.0% (3/3) | 66.7% (2/3) | -33.3% |
| Precision (incident-level) | 14.1% | 15.3% | +1.2% |

**false-positive reduction +9.0% (target >= 40%, missed) -- recall loss +10.0% (tolerance
<= 5%, missed). NFR-8 NOT MET.**

Verdicts: `corroborated=6, implausible=72, isolated=0`. Six attributions, of which the
recall column says roughly half took a real fault with them. v1 over-suppressed; v2 barely
suppresses, and still loses recall. Raw result:
`docs/results/paired-evaluation-v2-synchrony.json`.

#### Why v2 failed, and what it means for v1

The synchrony criterion was never actually exercised at the resolution it was written for.

An `Episode`'s `t_start_ms` is the start of the **window** that first flagged it
(`src/vigil/episodes.py`), and windows slide by 10 s. So the only start-time gaps two
episodes can have are 0 s, 10 s, 20 s, ... A synchrony tolerance of 5 s selects exactly one
of those: **zero**. What v2 measured was not "did these channels move within 5 seconds of
each other" but "did they first get flagged in the very same window bucket".

Measured against the ground-truth plan for this run, the artifacts of a perturbing deploy
land like this:

| Statistic over in-scope artifact onsets | Value |
|---|---|
| Spread between first and last onset in one deploy | median 13.3 s, max 57.7 s |
| Gap between consecutive in-scope onsets | median 1.8 s, p90 11.5 s |
| Consecutive pairs within 5 s of each other | 33 of 47 (70%) |
| Consecutive pairs within 1 s | 16 of 47 (34%) |

So at data resolution the artifacts *are* mostly within a few seconds of each other, which is
the signal the policy was designed around -- but the episode record rounds those few seconds
to a 10 s grid before the policy ever sees them.

That reframes v1 too. Both attempts asked a coincidence question at two different bucket
widths: v1 at 30 s (any overlap in the window), v2 at 0 s (the same bucket). Neither asked
the intended physical question. **The synchrony hypothesis is untested, not refuted**, and
this measurement does not support a claim in either direction about it.

What can be claimed from the two runs: the **mechanism check works**. In both, every episode
overlapping a pipeline event that had lost nothing was raised as `implausible` rather than
attributed (72 of 78 here), which is the reconciliation-conditioning half of the design
doing exactly its job.

#### What this measurement does not establish

- **The deploy density is unrealistically high.** Fourteen deploys with 60-180 s durations
  over a 900 s run overlap enough to cover nearly the whole window. Real fleets do not
  deploy continuously. That makes this a *hard* case rather than a representative one, and
  it is kept because a policy that survives it is more interesting than one tuned for a
  quiet afternoon. A sensitivity run at a lower rate is a separate row, not a replacement.
- **Conditioning is applied as episodes close, so the corroboration index only holds
  episodes that closed earlier.** An episode closing early therefore sees fewer potential
  siblings than one closing late. The asymmetry is real; a batch pass over completed windows
  would remove it, at the cost of latency.
- **The shadow baseline's own recall is 83.3%, not 100%.** Five real faults were never
  detected by the z-score detector at all, conditioning or no conditioning. That is a
  detector limitation and it caps what any conditioning policy can preserve.
- **v1 and v2 are the same scenario, not the same stream.** Within a run the two passes
  replay byte-identical records, which is what makes the pair a controlled comparison. Across
  runs the scenario is regenerated from the same seed and the same parameters, and the
  ground truth comes out identical (30 / 56 / 14), but the shadow pass differs slightly
  anyway -- 79 episodes at 19.0% precision in v1 against 78 at 14.1% in v2 -- because deploy
  timing is anchored to wall clock and window boundaries fall differently. Compare v1 and v2
  at the level of the effect, not digit by digit.

### 3.5 Sensitivity at a realistic deploy density, and the fail-open check

The v1/v2 runs deploy 60 times an hour, which overlaps enough to cover nearly the whole run.
This row lowers that to 20 an hour -- six deploys over 15 minutes, windows covering a
fraction of the stream rather than all of it -- and changes nothing else.

Run `4c4b0a0d`:

```
python evaluate.py --duration 900 --rate 400 --channels 12     --deploys-per-hour 20 --faults-per-hour 120     --report-json docs/results/paired-evaluation-low-density.json
```

Ground truth: **30 real faults** (12 inside context windows, 18 outside, 7 inside quiet
windows), **20 injected artifacts**, **6 deploy windows**.

| Measure | Shadow | Conditioned | Delta |
|---|---|---|---|
| Pages raised | 73 | 69 | -4 |
| False pages (artifact + unexplained) | 60 | 56 | -4 |
| of which artifact-driven | 20 | 16 | -4 |
| Recall, all real faults | 90.0% (27/30) | 83.3% (25/30) | -6.7% |
| Recall, faults **outside** windows | 94.4% (17/18) | 94.4% (17/18) | **+0.0%** |
| Recall, faults **inside** windows | 83.3% (10/12) | 66.7% (8/12) | -16.7% |
| Recall, faults in **quiet** windows | 71.4% (5/7) | 71.4% (5/7) | **+0.0%** |
| Precision (incident-level) | 17.8% | 18.8% | +1.0% |

**false-positive reduction +6.7% (target >= 40%, missed) -- recall loss +6.7% (tolerance
<= 5%, missed). NFR-8 NOT MET.**

What the row adds beyond a third failure. **The v1 pathology is gone.** v1 suppressed every
real fault in a quiet deploy window; here quiet-window recall is preserved exactly, and so
is recall outside windows. The remaining loss is entirely inside deploy windows: four
attributions, and two of them took a real fault with them. That is the same picture the v2
diagnosis predicts -- corroboration now fires only on an exact start-time tie, rarely, and
when it does fire it is close to a coin flip.

Density is therefore not what makes the policy miss NFR-8. It misses at 60 deploys an hour
by over-suppressing (v1) or barely acting (v2), and at 20 an hour by barely acting. The
binding problem is the resolution of the corroboration test, which is B-4.

#### Fail-open, verified end to end

The same run adds a third pass: conditioning **on**, pointed at a context topic that exists
and is empty.

```
fail-open (ADR-007) HELD: 73 of 73 episodes identical to the unconditioned pass
                        | 0 missing, 0 extra, 0 suppressed, 0 attributed
```

The detector reported `decided 73 | attributed 0 | raised 73 | no_context=73` against
`context events seen 0`. Every episode the unconditioned baseline raised, the signal-less
conditioned pass raised too, with the same channel, span, detector and status. This is the
Phase 3 gate's fail-open clause, verified against a live pipeline rather than only in unit
tests.

Scoped precisely: an empty topic leaves the context source *available and silent*, which the
policy answers with `no_context`. A source that cannot be reached at all is answered with
`fail_open` and is covered by unit tests in `tests/test_conditioning.py` -- taking the broker
away mid-run would take the readings with it and leave nothing to condition.

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
| Consumer throughput, replay of a filled topic | **43,160 readings/s** over 1,682,409 readings | `python detector.py --from-beginning` | Phase 1 |
| Live bridge throughput | **242 readings/s** from 1,387 MQTT messages over 45 s, 336 channels, 0 gaps | `python mqtt_bridge.py --topic inverters --duration 45` | Phase 1 |
| Scenario mode | 60,000 readings + 12 context markers over 150 s at a 400 ev/s target, 0 failures | `python loadgen.py --rate 400 --duration 150 --scenario` | Phase 1 |

The producer is not the bottleneck: it clears the 20,000 events/s NFR-4 target by 3.8x on
its own. Neither figure is an end-to-end throughput claim - the topic was pre-filled for the
consumer measurement, so the two were not running against each other.

The Phase 1 consumer figure above (43,160 readings/s) has since been superseded by a proper
sweep over a 3,928,127-record backlog: **94,495 readings/s** on one consumer and a plateau of
**170,414 readings/s** at three to six consumers over six partitions. NFR-4 is met with room
to spare; **NFR-5's near-linear claim is not met** - six consumers buy 1.80x. The curve, the
plateau, what binds it, and the rebalance re-delivery the sweep exposed are all in
`docs/SCALE.md`.

The live feed's own rate (~242 readings/s on `inverters`, ~4,000/s achievable on `strings`) is
far below the throughput target. That is a property of the source, not of the pipeline, and is
exactly why the synthetic harness is retained (ADR-014).

### 5.1a The Phase 1 end-to-end gate run

Loadgen and the detector running against each other, live, for 7 minutes:

```
python loadgen.py --rate 600 --duration 420 --channels 8 --scenario \
    --deploys-per-hour 60 --faults-per-hour 45 --seed 20260905 \
    --write-plan docs/results/phase1-gate-plan.json
python detector.py --from-beginning --group vigil-gate --stop-after-idle-s 25
```

| | |
|---|---|
| Readings produced / consumed | **252,000 / 252,000** (exact) |
| Delivery failures | 0 |
| Late readings (behind the watermark) | **0** |
| Windows emitted / scored / cold / thin-dropped | 360 / 352 / 8 / 0 |
| Windows flagged -> episodes | 213 -> **24** (189 merged into existing incidents) |
| Injected ground truth inside episodes | level_shift 3, spike 5, variance_burst 2 |
| Foundation model submitted / scored / **dropped** | 360 / 296 / **0** |

Produced and consumed counts match exactly and nothing arrived behind its watermark. That is
a single-consumer, single-run observation, **not** the zero-drift claim: that claim needs the
Phase 2 reconciliation harness over a multi-hour run, and is not made here.

### 5.1b Per-detector latency, measured

| Detector | Path | Windows | p50 | p95 | p99 | max | Budget |
|---|---|---|---|---|---|---|---|
| `zscore` | hot | 352 | 0.312 ms | 0.387 ms | **0.493 ms** | 5.879 ms | 250 ms p99 (NFR-1) |
| `chronos-bolt-tiny` | off critical | 296 | 6.298 ms | 24.180 ms | **34.833 ms** | 35.197 ms | not bound by NFR-1 |

The hot path clears its budget by roughly 500x, so NFR-1 is met with large headroom and is
plainly not the binding constraint on this design.

**An honest discrepancy worth naming.** In isolation chronos-bolt-tiny costs 0.64 ms per
window at batch 32. In this run it cost 34.8 ms p99 amortised, 54x worse. The cause is batch
starvation, not the model: 296 windows over 466 s arrived across 91 batches, roughly 3.3
windows each, so nearly every forward pass paid close to fixed overhead for an almost empty
batch. Batching only pays when there is enough window throughput to fill a batch, and at 8
channels with a 10 s slide the stream produces about 0.8 windows/s. This is a property of the
measurement conditions and would improve with more channels; it is recorded rather than
smoothed over, and the isolated figure is not presented as the operational one.

### 5.1c Chronos-Bolt model sizes on this CPU

Measured directly: context 256, horizon 12, median of repeated calls.

| Checkpoint | Params | Batch 1 | Batch 32 (total) | Batch 32 (per window) |
|---|---|---|---|---|
| `chronos-bolt-tiny` | 8.7M | 5.5 ms | 20.5 ms | 0.64 ms |
| `chronos-bolt-mini` | 21.2M | 7.7 ms | 36.9 ms | 1.15 ms |
| `chronos-bolt-small` | 47.7M | 13.1 ms | 94.4 ms | 2.95 ms |
| `chronos-bolt-base` | 205.3M | 33.1 ms | 298.4 ms | 9.32 ms |

The base model's *batch* takes 298 ms, so the last window in a batch would breach a 250 ms
per-window hot-path budget on its own. That is the concrete number behind ADR-017 keeping the
model off the critical path, and behind choosing tiny as the default.

### 5.1d Where the two detectors disagree

Over the same 252,000 readings, each at its own threshold:

| Detector | Episodes | Avg peak score | Max peak score |
|---|---|---|---|
| `zscore` (threshold 8.0) | 19 | 33.9 | 112.2 |
| `chronos-bolt-tiny` (threshold 6.0) | 5 | 7.7 | 9.7 |

These counts are **not** a quality comparison. The thresholds were never calibrated against
each other, so the two detectors are spending different alarm budgets; reading "the baseline
found more" as "the baseline is better" is exactly the error the matched-alarm-budget rule in
section 2 exists to prevent. The honest comparison is section 4's TSB-AD-M benchmark, which is
not yet run. What this run does establish is that both detectors are live on the same stream
and producing independently attributable episodes.

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

Per-detector latency is measured in section 5.1b. Final targets are locked after Phase 2
produces the scaling curve.

---

## 6. Honesty rules held in this document

- Every number states the hardware and the command that produced it.
- A target that is missed is reported as missed, not quietly re-scoped afterwards. Revisions to
  targets are made *before* measurement and carry an ADR (ADR-016, ADR-017).
- Losses are reported with the same prominence as wins.
- "Not yet measured" is used rather than an estimate.
