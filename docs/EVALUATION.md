# Evaluation

> Methodology first, results as they are measured. Nothing in this file is a projection: a
> number appears here only after it has been produced by a command recorded alongside it.
> Sections marked **not yet measured** are honest placeholders, not omissions.

**Reference hardware for every number in this file:** Intel Core i7-12650H (10 cores /
16 threads), 15.6 GB RAM, NVIDIA RTX 3050 Ti Laptop (4 GB VRAM), Windows 11, Docker Desktop
with 16 CPU / 8.1 GB allocated to the VM, Python 3.12.10. **One exception, named where it
appears:** the QLoRA planner numbers in section 6 were produced on a V100-SXM2-32GB, because
a 7B adapter does not fit in 4 GB of VRAM.

---

## 1. What is being evaluated, and against what

Four separate questions, deliberately not blended into one score:

| # | Question | Data | Compared against |
|---|---|---|---|
| Q1 | Does context-conditioned detection reduce false positives without losing real anomalies? | Adversarial scenario runs (section 3) + chaos-injected pipeline faults | The **unconditioned** baseline on identical data, via a shadow pass |
| Q2 | Is the foundation-model detector actually better than a cheap baseline? | TSB-AD-M, 200 labelled multivariate series | Rolling z-score baseline |
| Q3 | What does detection cost in latency and throughput? | Synthetic load harness | The NFR budgets in `REQUIREMENTS.md` |
| Q4 | Does a fine-tuned planner beat the deterministic one on cases the rules get wrong? | 300 held-out hard planning cases (section 6) | The deployed rule-based planner, scored two ways |

Q1 is the core contribution. Q2 is the honest-benchmark obligation. Q3 is the engineering
budget. Q4 is the one question whose answer came out in the model's favour.

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

> Sections 3.4 to 3.7 are v1 to v3, kept as written. The runs are real and the numbers
> stand; two of the conclusions drawn from them do not, and where that is so it is said in
> place rather than edited out. v4 is sections 3.8 to 3.11.

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
the intended physical question. **The synchrony hypothesis was untested rather than refuted**
at this point, and neither measurement supported a claim in either direction about it.

It has since been tested. The episode record was fixed (ADR-035) and the run repeated as v3
in section 3.6: with true onsets the hypothesis is answerable, and the answer is that the
criterion is too weak to reach NFR-8 rather than that it was mismeasured.

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

### 3.6 v3 -- synchrony on true onsets. The hypothesis is now tested, and it fails.

v1 and v2 both compared window boundaries, quantised to the 10 s slide, so a 5 s tolerance
could only ever match an exact tie (section 3.4). The episode record now carries the event
time of the reading that drove the score (ADR-035), and over a 420-second scenario every one
of 35 episodes lands strictly off the grid, spread 0-27 s inside its window. So the
comparison finally has the resolution the criterion was written for.

Run `d71db158`, **same command, same seed, same density as v1 and v2**, with nothing changed
but the timestamp the policy reads:

```
python evaluate.py --duration 900 --rate 400 --channels 12     --deploys-per-hour 60 --faults-per-hour 120     --report-json docs/results/paired-evaluation-v3-onset.json
```

| Measure | Shadow | Conditioned | Delta |
|---|---|---|---|
| Episodes recorded | 76 | 76 | |
| Pages raised | 76 | 69 | -7 |
| Attributed (not paged) | 0 | 7 | |
| False pages (artifact + unexplained) | 63 | 56 | **-7** |
| of which artifact-driven | 42 | 36 | -6 |
| Recall, all real faults | 90.0% (27/30) | 83.3% (25/30) | **-6.7%** |
| Recall, faults **outside** windows | 82.4% (14/17) | 76.5% (13/17) | -5.9% |
| Recall, faults **inside** windows | 100.0% (13/13) | 92.3% (12/13) | -7.7% |
| Recall, faults in **quiet** windows | 100.0% (3/3) | 66.7% (2/3) | -33.3% |
| Precision (incident-level) | 17.1% | 18.8% | +1.7% |

**false-positive reduction +11.1% (target >= 40%, missed) -- recall loss +6.7% (tolerance
<= 5%, missed). NFR-8 NOT MET.**

Fail-open held: **76 of 76** episodes identical to the unconditioned pass, `no_context=76`.

### 3.7 The first four measurements, and what they added up to

> Superseded by sections 3.8 to 3.11, which are v4. The conclusion below stood on the
> evidence available at the time and two things in it turned out to be wrong: the
> `isolated=0` reading (see G-16 -- the verdict field could not report what it was read as
> reporting), and the inference that in-scope synchrony is rare. Kept as written, because a
> record that quietly repairs its own earlier conclusions is not a record.


| Run | What changed | FP reduction | Recall loss | Quiet-window recall | NFR-8 |
|---|---|---|---|---|---|
| **v1** | corroboration by co-occurrence in a 30 s window | **+60.9%** | **-36.7%** | -100.0% | missed |
| **v2** | required synchrony, compared on window starts | +9.0% | -10.0% | -33.3% | missed |
| **low density** | 20 deploys/hour instead of 60 | +6.7% | -6.7% | +0.0% | missed |
| **v3** | required synchrony, compared on **true onsets** | +11.1% | **-6.7%** | -33.3% | missed |

Read down the columns rather than across the rows, because the trajectory is the finding.

**v1 was not a near-miss, it was blanket suppression.** It cleared the 40% target by
attributing 39 of 79 episodes, and it paid for that by losing every real fault in a quiet
deploy window -- where by construction there is no artifact to attribute anything to. The
population that exists to catch exactly this (ADR-015) caught it.

**v2 and v3 are the honest version of the mechanism, and it is too weak.** Requiring
synchrony cuts attributions from 39 to 7 and holds recall loss to 6.7%, but it buys only
11.1% of the false pages. The verdict breakdown says why: `corroborated=7, implausible=69,
isolated=0`. In 76 episodes the corroboration test **never once** concluded that a channel
had moved alone -- not because siblings always moved with it, but because the in-scope
siblings that would exonerate it had usually not closed yet when its own turn came (gap G-7).
The test that was supposed to discriminate mostly declines to fire at all.

**The onset fix moved the numbers in the right direction and did not rescue the target.**
v3 against v2: +2.1 points of reduction and 3.3 points less recall loss. Real, consistent
with the resolution having improved, and nowhere near 40%.

**Two of the seven attributions still cost a real fault**, including one of the three in a
quiet window. So the residual collateral alone (2/30 = 6.7%) exceeds NFR-8's 5% tolerance
before the reduction target is even considered.

#### The conclusion this supports

The **plausibility half of the design works**: 69 of 76 episodes overlapping a pipeline event
that had lost nothing were raised as `implausible` rather than attributed, which is the
reconciliation-conditioning mechanism doing exactly its job, in every run. Fail-open holds
end to end.

The **corroboration half does not deliver NFR-8**, and after four measurements the reason is
no longer a measurement artefact: at this density, on this detector, in-scope synchrony is
too rare among *detected* episodes to attribute enough of them, and when it does fire it is
roughly a coin flip. Reaching 40% with this policy would need either a corroboration index
over completed windows rather than closed episodes (removing G-7's asymmetry at the cost of
latency), or a different discriminator entirely -- magnitude and direction agreement across
scope, say, rather than timing.

**NFR-8 is not met and is reported as not met.** The false-positive reduction is +11.1%
against a 40% target. That is the result.

### 3.8 v4a -- the control: the same policy, with the evidence actually present

Step one of v4 changed no discriminator. It changed *when* the discriminator is asked
(ADR-037): the corroboration index is filled when an episode opens rather than when it
closes, and the verdict waits behind an event-time barrier keyed to the fleet watermark --
the minimum across channels, so the slowest channel governs. v3's synchrony test is
otherwise untouched, which is what makes this a control rather than a fifth attempt.

Run `926aaf75`, **same command, same seed, same density, same scenario generator as v1-v3**:

```
python evaluate.py --duration 900 --rate 400 --channels 12     --deploys-per-hour 60 --faults-per-hour 120     --report-json docs/results/paired-evaluation-v4a-watermarked.json
```

Ground truth identical to v1-v3: 30 real faults (13 inside context windows, 17 outside, 3
inside quiet windows), 56 artifacts, 14 deploy windows.

| Measure | Shadow | Conditioned | Delta |
|---|---|---|---|
| Episodes recorded | 86 | 86 | |
| Pages raised | 86 | 65 | -21 |
| Attributed (not paged) | 0 | 21 | |
| False pages (artifact + unexplained) | 72 | 52 | **-20** |
| of which artifact-driven | 46 | 26 | -20 |
| Recall, all real faults | 83.3% (25/30) | 66.7% (20/30) | **-16.7%** |
| Recall, faults **outside** windows | 70.6% (12/17) | 64.7% (11/17) | -5.9% |
| Recall, faults **inside** windows | 100.0% (13/13) | 69.2% (9/13) | **-30.8%** |
| Recall, faults in **quiet** windows | 100.0% (3/3) | 100.0% (3/3) | **+0.0%** |
| Precision (incident-level) | 16.3% | 20.0% | +3.7% |

**false-positive reduction +27.8% (target >= 40%, missed) -- recall loss +16.7%
(tolerance <= 5%, missed). NFR-8 NOT MET.**

Fail-open held: 86 of 86 episodes identical to the unconditioned pass, `no_context=86`.

#### The evidence really was missing, and it is not any more

This is the measurement G-7 was a hypothesis about, and it is now instrumented rather than
inferred. The policy records, for every scoped decision, how many in-scope siblings were
synchronous with the episode and how many were present anywhere in its span:

```
corroboration evidence over 83 scoped decisions:
  in-scope siblings synchronous              mean 0.65   (33 decisions with >= 1)
  present anywhere in the episode span       mean 2.47   (56 decisions with >= 1)
```

In two thirds of scoped decisions there is now an in-scope sibling in the record to reason
about. Attributions went from 7 in v3 to 21 here, on the same scenario at the same density,
with no change to the criterion. **G-7 is closed.**

#### What it bought, and what it exposed

+27.8% against v3's +11.1%: the watermarking alone is worth 16.7 points of false-positive
reduction, which is most of the way from v3 to the 40% target. It also **more than doubled
the recall loss**, from 6.7% to 16.7%, and all of it lands inside deploy windows
(-30.8%, nine of thirteen faults detected where v3 detected twelve).

That is not a regression. It is the criterion being tested for the first time on complete
data, and failing:

> **Synchrony inside scope does not separate a deploy artifact from a real fault, because a
> real fault is often synchronous inside scope.** A pump seizing moves its vibration, its
> bearing temperature and its flow within seconds of each other. If those channels are
> inside a deploy's scope -- and the adversarial generator puts 40% of faults inside deploy
> windows on purpose -- then every question the v1-v3 policy knows how to ask answers
> "deploy", and a real fault stops paging anyone.

v3 was protected from this by its own blindness: it could not see enough siblings to fire,
so it could not fire wrongly. Removing the blindness is what makes the criterion's weakness
measurable. The quiet-window population is undamaged (3/3, as in the low-density row), so
this is not blanket suppression returning; it is a discriminator attributing the wrong
things confidently.

#### The barrier cost almost nothing, and the index change did the work

```
verdict barrier: buffer 30s | held 86 | released on watermark 84 | on flush 2
                 | delayed past episode close 2 of 84, event-time p50 0.0s max 66.4s
```

Only **2 of 84** verdicts were actually delayed past the moment their episode closed. The
reason is geometry: an episode does not close until its channel has been quiet for two
window slides, by which point a sibling that departed within the synchrony tolerance has
already had its first window closed and scored. So the *index* change is what supplied the
evidence, and the barrier is a guarantee rather than the active ingredient in this run.

That is worth saying plainly, because the reverse would have been easy to imply. The barrier
earns its place by making the property hold rather than happen to hold -- it does not depend
on the merge gap being larger than a window, and it is what makes the verdict a function of
event time rather than of the order episodes closed in. But on this data, at this geometry,
it is not where the 16.7 points came from. Both halves are reported so the attribution of
credit is checkable.

#### A correction to the earlier diagnosis (G-16)

Sections 3.4 and 3.7 read `isolated=0` as "the corroboration test never once concluded a
channel had moved alone". The verdict field could not have reported that. When several
context events overlap an episode the policy kept the **last** non-out-of-scope rejection,
and pipeline health events sort last and answer `implausible` -- so an `isolated` conclusion
from the deploy test was overwritten in the record whenever a health event also overlapped,
which is nearly always. This run shows `corroborated=21, implausible=65, isolated=0` while
the evidence counters show 50 of 83 scoped decisions with no synchronous sibling at all:
those 50 *are* isolated conclusions, and the record never said so.

No measured number changes. Both verdicts raise the episode, so `paged`, recall and
false-positive reduction are unaffected in every run published here. What was wrong was a
line of reasoning that rested on a field which could not carry it.

### 3.9 The scenario v4 is measured on, and why it is not the v1-v3 scenario

Step two changes the data as well as the policy, and that has to be stated before any
number is read. The generator now places excursions on the inventory (ADR-038):

| | v1 - v4a | v4, v4w |
|---|---|---|
| A deploy touches | a random subset of channels | one metric family across the machines of one deploy ring |
| A real fault touches | exactly one channel | 2-4 metrics on **one machine** (55%), 1-2 metrics on **two machines in one cabinet** (15%), or one channel (30%) |
| Faults are counted | per channel-episode | **per incident** -- a seizing pump is one thing that happened |

Ground truth for both v4 runs, written before either pass: **30 fault incidents** (15
single-channel, 13 machine faults, 2 cabinet faults) spanning **57 channel-episodes**,
**43 injected artifacts**, **11 deploy windows**, 7 incidents inside quiet deploys.

Three consequences, all of which cut against reading v4 as a continuation of v1-v3:

1. **The scenario is not the same scenario.** Same seed, same command, same density -- but
   the generator consumes its random stream differently, so it schedules 11 deploys where
   v1-v3 scheduled 14, and 43 artifacts where they had 56. v4 is compared to **its own
   shadow pass**, which is what every result here has always been; it is not comparable to
   v1-v3 digit by digit.
2. **Multi-channel faults are harder to lose.** An incident counts as detected if *any* of
   its channels pages someone, so a policy has to attribute every metric of a seizing pump
   to hide it. That makes the recall column structurally more forgiving than v1-v3's, which
   is why the per-channel-episode row is reported beside it: at 12 channels the conditioned
   pass loses 1 incident and 4 channel-episodes from the same run.
3. **The trap population is deliberate.** Six multi-channel faults land wholly inside a
   deploy's scope, moving within four seconds of each other. On timing, scope and fraction
   they are indistinguishable from a deploy artifact. That is the case ADR-038 exists for,
   and a generator that did not produce it would make the topology test unfalsifiable.

### 3.10 v4 -- the topology discriminator, at two fleet widths

Both runs use the identical policy and differ only in how much structure the fleet has.
Each reports a **fourth pass** in the same run, on byte-identical records, with the
blast-radius test switched off -- so the topology's contribution is an ablation rather than
a comparison across runs whose window boundaries fall differently.

#### v4, 12 channels -- three machines, one cabinet, one ring

```
python evaluate.py --duration 900 --rate 400 --channels 12 \
    --deploys-per-hour 60 --faults-per-hour 120 \
    --report-json docs/results/paired-evaluation-v4-topology.json
```

| Measure | Shadow | Conditioned | Delta |
|---|---|---|---|
| Episodes recorded | 76 | 76 | |
| Pages raised | 76 | 66 | -10 |
| False pages (artifact + unexplained) | 53 | 43 | **-10** |
| of which artifact-driven | 35 | 26 | -9 |
| Recall, all real faults (incidents) | 93.3% (28/30) | 90.0% (27/30) | **-3.3%** |
| Recall, faults **outside** windows | 87.5% (14/16) | 87.5% (14/16) | **+0.0%** |
| Recall, faults **inside** windows | 100.0% (14/14) | 92.9% (13/14) | -7.1% |
| Recall, faults in **quiet** windows | 100.0% (7/7) | 100.0% (7/7) | **+0.0%** |
| Recall, per fault channel-episode | 89.5% (51/57) | 82.5% (47/57) | -7.0% |
| Precision (incident-level) | 30.3% | 34.8% | +4.6% |

**false-positive reduction +18.9% (target >= 40%, missed) -- recall loss +3.3%
(tolerance <= 5%, MET). NFR-8 NOT MET.** Fail-open held, 76 of 76.

Ablation, same records: **timing only +17.0% / -3.3%, with blast radius +18.9% / -3.3%.**

The topology test barely runs here, and the reason is in the fleet rather than in the
policy: 12 channels is three pumps in one cabinet on one ring, so the rack and ring levels
carry no information and only "did this span two machines" can fire. It fired **once** in
289 rejections. The difference between the two columns is one attribution, which is noise
at this count -- an earlier run of the same configuration had the sign the other way
(`docs/results/paired-evaluation-v4-topology-prefix-g16.json`, +18.3% with the test against
+21.7% without). **At this fleet width the honest finding is that the discriminator has
nothing to discriminate on.**

#### v4w, 24 channels -- six machines, two cabinets, two rings

Per-channel sampling rate is held constant (800 ev/s over 24 channels is the same 33 Hz per
channel as 400 over 12), because the z-score scales with the square root of the window's
point count and halving it would move the detector's operating point inside the comparison.

```
python evaluate.py --duration 900 --rate 800 --channels 24 \
    --deploys-per-hour 60 --faults-per-hour 120 \
    --report-json docs/results/paired-evaluation-v4w-topology-wide.json
```

| Measure | Shadow | Conditioned | Delta |
|---|---|---|---|
| Episodes recorded | 147 | 147 | |
| Pages raised | 147 | 141 | -6 |
| False pages (artifact + unexplained) | 112 | 108 | **-4** |
| of which artifact-driven | 37 | 33 | -4 |
| Recall, all real faults (incidents) | 93.3% (28/30) | 90.0% (27/30) | **-3.3%** |
| Recall, faults **outside** windows | 93.8% (15/16) | 93.8% (15/16) | **+0.0%** |
| Recall, faults **inside** windows | 92.9% (13/14) | 85.7% (12/14) | -7.1% |
| Recall, faults in **quiet** windows | 100.0% (7/7) | 100.0% (7/7) | **+0.0%** |
| Recall, per fault channel-episode | 86.0% (49/57) | 82.5% (47/57) | -3.5% |
| Precision (incident-level) | 23.8% | 23.4% | -0.4% |

**false-positive reduction +3.6% (target >= 40%, missed) -- recall loss +3.3%
(tolerance <= 5%, MET). NFR-8 NOT MET.** Fail-open held, 147 of 147.

#### The ablation, which is the actual measurement

| Same records, same run | FP reduction | Recall loss | Attributed | Single-sensor faults kept |
|---|---|---|---|---|
| timing and scope only | **+8.0%** | **-6.7%** | 11 of 147 | 12/15 |
| **with the blast-radius test** | **+3.6%** | **-3.3%** | 6 of 147 | **13/15** |

The topology test refused 5 attributions. Doing so **halved the recall loss**, from 6.7% to
3.3%, and recovered a real fault the timing-only policy attributed away -- and it cost 4.4
points of false-positive reduction. The verdict record says which test did it:

```
corroborated=6, fault_domain=4, narrow_blast_radius=1, isolated=46, implausible=90
rejections reached: fault_domain=5, narrow_blast_radius=2, isolated=61,
                    implausible=423, out_of_scope=148
```

Five `fault_domain` refusals -- "every channel that moved sits on pump-02, while deploy-0007
reached three machines" -- and two `narrow_blast_radius`, single-machine canary rollouts
that no evidence could separate from a fault. Against one refusal at 12 channels. **The
discriminator fires where the topology has structure and is inert where it does not**,
which is what it claims to do, and both fleet widths are published so the claim is bounded
by the width it was measured at.

**The 24-channel ablation replicates.** An earlier run of the same configuration, before the
verdict-record fix, is kept at
`docs/results/paired-evaluation-v4w-topology-wide-prefix-g16.json`. Its episodes and window
boundaries differ -- 139 against 147 -- but the ablation points the same way and by nearly
the same amount:

| 24-channel run | timing only | with blast radius | recall loss saved | reduction given up |
|---|---|---|---|---|
| earlier (139 episodes) | +11.1% / -6.7% | +6.1% / **-0.0%** | 6.7 pts | 5.0 pts |
| published (147 episodes) | +8.0% / -6.7% | +3.6% / **-3.3%** | 3.4 pts | 4.4 pts |

Two independent runs, the same trade in the same direction. That is weaker evidence than a
seed sweep and stronger than one run, and it is stated as exactly that. The 12-channel
ablation does **not** replicate -- the sign of its reduction delta flips between runs -- which
is consistent with a test that fires once there and is therefore measuring noise.

### 3.11 All seven measurements, and what v4 settles

| Run | What changed | FP reduction | Recall loss | Quiet-window recall | NFR-8 |
|---|---|---|---|---|---|
| **v1** | corroboration by co-occurrence in a 30 s window | **+60.9%** | **-36.7%** | -100.0% | missed |
| **v2** | required synchrony, compared on window starts | +9.0% | -10.0% | -33.3% | missed |
| **low density** | 20 deploys/hour instead of 60 | +6.7% | -6.7% | +0.0% | missed |
| **v3** | required synchrony, compared on **true onsets** | +11.1% | -6.7% | -33.3% | missed |
| **v4a** | same policy, evidence made **present** (ADR-037) | **+27.8%** | **-16.7%** | +0.0% | missed |
| **v4** | topology discriminator, 12 ch (3 machines, 1 cabinet) | +18.9% | **-3.3%** | +0.0% | missed |
| **v4w** | topology discriminator, 24 ch (6 machines, 2 cabinets) | +3.6% | **-3.3%** | +0.0% | missed |

v4a shares the v1-v3 scenario; v4 and v4w share a different one (section 3.9). Read the last
three rows against their own shadow passes and their own ablations, not against the first
four.

**NFR-8 is not met, for the fifth measurement.** The reduction is +18.9% at 12 channels and
+3.6% at 24, against a 40% target.

#### What v4 established that the four before it could not

**The corroboration test was never starved of evidence in the way it appeared to be, and it
was never silent.** Both of those were artefacts. G-7 was real -- in-scope siblings now
appear in 58 of 69 scoped decisions at 12 channels -- and G-16 was a reporting defect that
made every run print `isolated=0`. With both fixed the record reads `isolated=46` at 24
channels: the corroboration test concludes "this channel moved alone" constantly, and always
did.

**Timing is the wrong axis, and now there is a measurement rather than an argument.** v4a
put complete evidence behind v3's criterion and the criterion promptly attributed real
faults: +27.8% reduction bought at -16.7% recall, ten points worse than v3 managed while
half-blind. A machine failing is synchronous within its own scope, so on the axis synchrony
measures, a seizing pump and a rollout are the same event.

**Shape separates what timing cannot, by the amount the ablation says and no more.** Five
refusals at 24 channels halved the recall loss and cost 4.4 points of reduction. That is a
real effect, on byte-identical records within one run, in the direction the mechanism
predicts. It is also small.

**The recall half of NFR-8 is met for the first time**, in both v4 runs (-3.3% against a 5%
tolerance) with quiet-window recall untouched at 7/7. Every earlier run failed it. What
fails now is the reduction half, and at 24 channels it fails worse than v3 did -- because
each of the three fixes makes the policy *more* reluctant to attribute, and reluctance is
what the reduction target punishes.

#### Where this leaves the mechanism, and the ceiling nobody had computed

The **plausibility half continues to work** in every run: 90 of 147 episodes overlapping a
pipeline event that had lost nothing were raised as `implausible`. **Fail-open holds end to
end** in all seven runs, 147 of 147 in the largest.

The **corroboration half still does not reach NFR-8**, and after v4 the reason is no longer
that the test cannot see or cannot fire. It sees, it fires, and it is right more often than
before. There is simply not enough attributable noise in these runs for a policy this
conservative to remove 40% of the false pages -- and the arithmetic is worth stating,
because it was never done in v1 to v3:

> At 24 channels, **75 of the 112 false pages are `unexplained`** rather than
> artifact-driven. They overlap no injected excursion of any kind, so no context signal can
> attribute them, correctly or otherwise. The ceiling on any conditioning policy in that run
> is therefore **37/112 = 33%**, reached only by attributing every single artifact page and
> never being wrong. **The 40% target was unreachable on that run before the policy made a
> single decision.** At 12 channels the ceiling is 35/53 = 66%, and +18.9% is 29% of it.

That is a property of the detector's false-positive mix, not of the discriminator, and it is
the first thing a v5 has to confront. It is also a decision for the architect rather than an
implementation choice, so it is recorded as **B-6**: either the reduction is measured against
the attributable subset -- which changes what NFR-8 means and must be argued for, not
adopted quietly because it flatters the number -- or the unexplained pages are reduced at the
detector, which is a detection problem rather than a conditioning one.

**NFR-8 is reported as not met.** The pair is +18.9% / -3.3% at 12 channels and +3.6% /
-3.3% at 24, against +40% / -5%.

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

**Results.** Run `python benchmark.py --report-json docs/results/benchmark.json`, windows of
100 points sliding by 50, `--max-points 20000`, Chronos-Bolt-tiny on CPU.

**144 of 200 series scored.** The other 56 were dropped because their labelled anomalies
begin past the 20,000-point truncation, so the truncated view of them contains no positives
at all. That is a real bias and it points one way: the scored corpus is the early-onset half
of the corpus. It is stated here rather than left in the log, and the harness now prints it
with the aggregate.

| Detector | AUC-PR (median) | AUC-PR (mean) | F1 at a matched alarm budget | Scoring time |
|---|---|---|---|---|
| **zscore** | **0.198** | **0.321** | **0.325** | **59 s** |
| chronos-bolt-tiny | 0.152 | 0.257 | 0.231 | 8,312 s |

Precision, recall and F1 are equal by construction at a matched alarm budget: the detector is
allowed exactly as many alarms as there are positive windows, so a false positive and a false
negative are the same event counted twice.

**Head to head over 144 series: zscore wins 78, chronos-bolt-tiny wins 56, 10 ties.**

### 4.1 The finding: the foundation model loses, and costs 141x more to run

The zero-shot foundation model is beaten by a rolling z-score on this corpus, on the median,
on the mean, and on the head-to-head count -- while taking **141 times more compute** to
produce that worse answer (8,312 s against 59 s for the same 2.1 million points). On a
laptop CPU this is not a close call.

This is the outcome `docs/PROJECT_PLAN.md` section 15.1 flagged as an open question in the
literature, and it is why the z-score baseline was built first and kept: the interesting
result was always going to be *where* the trendy method is the wrong tool, and it is here
for most of this corpus.

### 4.2 Where it does win, which is not nowhere

Chronos wins 56 series, and the wins are not scattered at random.

| Dataset family | zscore wins | chronos wins | ties | n |
|---|---|---|---|---|
| SVDB (ECG) | 21 | 1 | 0 | 22 |
| SMAP (spacecraft telemetry) | 21 | 6 | 0 | 27 |
| LTDB (ECG) | 4 | 1 | 0 | 5 |
| OPPORTUNITY (wearables) | 4 | 3 | 0 | 7 |
| MSL (spacecraft telemetry) | 8 | 8 | 0 | 16 |
| SMD (server machines) | 9 | 12 | 0 | 21 |
| **Exathlon (Spark clusters)** | 8 | **19** | 0 | 27 |
| TAO (ocean buoys) | 0 | 3 | 10 | 13 |

The split follows the shape of the signal rather than the domain label. Where an anomaly is a
sharp amplitude excursion against a stationary baseline -- ECG, spacecraft sensors -- a
z-score is already the right model and forecasting buys nothing. Where the normal signal is
structured and non-stationary and an anomaly is a *departure from an expected pattern* rather
than from an expected level -- Exathlon's Spark cluster traces, SMD's machine metrics -- the
forecaster earns its keep, and it takes Exathlon 19-8.

Anomaly density says the same thing from another angle:

| Anomalous fraction of windows | zscore wins | chronos wins | ties | chronos win rate |
|---|---|---|---|---|
| < 1% | 11 | 10 | 0 | 48% |
| 1-5% | 32 | 30 | 2 | 47% |
| 5-10% | 24 | 13 | 5 | 31% |
| > 10% | 11 | 3 | 3 | 18% |

The model is competitive on rare anomalies and falls away as they become common. The
mechanism is visible in the design: the forecaster conditions on recent history, and when
more than a tenth of that history is itself anomalous, it forecasts the anomaly and the
residual goes flat. The z-score's decayed reference has the same weakness in principle and
is evidently less sensitive to it in practice.

### 4.3 What this benchmark does not establish

- **The corpus is the early-onset half.** 56 of 200 series were excluded by truncation, all
  of them series whose anomalies start late. Removing the truncation would take an estimated
  4+ hours of CPU here and has not been run.
- **Window resolution, not point resolution.** Each window is scored once against a window
  label (see the module docstring for why point-spreading was abandoned). This is a coarser
  task than point-level TSB-AD scoring and the numbers are **not comparable** to point-level
  leaderboards.
- **Multivariate series, univariate detectors, max across columns.** An anomaly that exists
  only in the correlation between features -- where each column alone looks normal -- cannot
  be detected by either detector as driven here. Such anomalies are in this corpus.
- **One model, at its smallest size.** Chronos-Bolt-**tiny** was chosen to fit the laptop
  (measured per window at batch 32: tiny 0.64 ms, mini 1.15 ms, small 2.95 ms, base 9.32 ms).
  A larger checkpoint may well close the gap; that this one does not close it at 141x the
  cost is the claim, not that no foundation model can.
- **The timings are wall-clock on a machine that was not idle.** Other work ran during the
  benchmark. The 141x ratio is far too large to be an artefact of that, but neither figure is
  a clean latency measurement, and the per-window latencies in section 5.1b are.

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

### 5.1z NFR-3, end-to-end detect latency: not measured, and not measurable as written

NFR-3 asks for event-to-flag within 2 s p99. It has not been measured, and it cannot be met
at this geometry: windows are 30 s sliding by 10 s, so nothing can be flagged before the
window containing it closes. The floor is one slide at best and one window at worst, against
a hot-path scoring cost of 0.15 ms p99 -- the requirement is an order of magnitude below its
own lower bound, and the binding term is the geometry, not the pipeline.

Recorded as B-5 in `docs/BLOCKERS.md` with the options. What the platform actually controls
-- the delay from a window closing to its episode being raised -- is worth measuring under
either resolution, and is not yet measured.

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

## 6. Q4 — the fine-tuned planner against the deterministic one

**Hardware exception, stated because the rest of this file promises otherwise.** Every other
number here was taken on the reference laptop. This one could not be: the adapter is a 7B
model in NF4 and the laptop has 4 GB of VRAM (C-1). Training and scoring both ran on
**Northeastern Explorer**, one **V100-SXM2-32GB**, Python 3.12, torch 2.5.1+cu121,
transformers 4.46.3, peft 0.13.2, trl 0.12.0, bitsandbytes 0.45.0.

**What is being asked.** Not "does the model emit valid JSON" — a template does that. The
claim under test is that a fine-tuned planner **beats the deployed rules on the cases the
rules get wrong**, so every metric is reported for all three planners on the same 300 held-out
cases.

**The split.** `vigil.tuning.hard`, 300 cases, seed 20260907, `split="test"`. Episode shapes
outside the five symptoms `Diagnoser` can name — and it has no fall-through, so it
misclassifies them rather than abstaining — on channels and metric vocabularies the training
split never contains, with every runbook passage stating its licences in prose rather than as
a machine-readable line, and retrieval returning the matching passage plus two distractors.
Targets come from the hand-written teacher policy in `vigil.tuning.hard.TEACHER` (ADR-036), so
**the teacher is the ceiling**: a model can reach that policy and cannot exceed it.

**Both baselines are reported, because either alone misleads.** *As deployed* is the live
parser, which finds no `licensed-actions:` line in a prose passage and therefore escalates on
everything. *Given licences* hands the rules the machine-readable licence set the parser could
not have extracted — deliberately generous, so it measures the rules' reasoning rather than
their inability to read.

```bash
python train_planner.py --base-model Qwen/Qwen2.5-7B-Instruct
python evaluate_planner.py --adapter artifacts/planner-qlora \
    --base-model Qwen/Qwen2.5-7B-Instruct \
    --report-json docs/results/planner-eval.json
```

**The training run.** QLoRA: NF4 double-quantised weights, fp16 compute (ADR-039 — Volta has
no bf16), LoRA r=32 on the attention and MLP projections, effective batch 16, lr 1e-4,
completion-only loss via `DataCollatorForCompletionOnlyLM`. One epoch, **75 optimizer steps in
34.6 minutes**, final `train_loss` **0.034**, no NaN in any logged step.

### 6.1 Head to head, 300 held-out cases

Raw result in `docs/results/planner-eval.json`.

| Planner | Exact match | Action-set match | Proposed a forbidden action | Missed a wanted action | Escalated only | p50 per plan |
|---|---|---|---|---|---|---|
| rules, **as deployed** | 0.0% (0/300) | 0.0% | 0.0% (0) | 100% (300) | 100% (300) | 0.006 ms |
| rules, **given licences** | 20.0% (60/300) | 20.0% | **61.3% (184)** | 60.0% (180) | 0.0% | 0.009 ms |
| **QLoRA Qwen2.5-7B-Instruct** | **97.7% (293/300)** | **97.7%** | **1.0% (3)** | **2.3% (7)** | 0.0% | 15,840 ms |
| teacher policy | 100% by construction | 100% | 0% | 0% | — | — |

**Against the generous baseline the exact-match delta is +77.7 points and the forbidden-action
delta is -60.3 points. Against the baseline that actually runs, +97.7 points.** Put as
headroom rather than as a difference of percentages: the gap between the generous rules and
the teacher is 80 points, and the fine-tune closes **97.1%** of it.

Exact match is order-sensitive and action-set match is not; they are identical here at 293, so
the model made no ordering-only errors — gather evidence before changing state came out right
in every plan it otherwise got right.

Structural conformance across all 300 generated plans: **0 schema-invalid, 0 unknown verbs, 0
fenced replies, 0 ungrounded actions** (every non-escalation action cited a licence present in
the passage it was given), **0 gate rejections**.

### 6.2 Per symptom, which is where the shape of the win is

| Symptom | n | rules as deployed | rules given licences | fine-tuned |
|---|---|---|---|---|
| stuck_sensor | 60 | 0.0% | 0.0% | **100% (60)** |
| oscillation | 60 | 0.0% | 0.0% | **100% (60)** |
| counter_rollover | 60 | 0.0% | 0.0% | 95.0% (57) |
| correlated_step | 60 | 0.0% | 0.0% | 93.3% (56) |
| **slow_drift** | 60 | 0.0% | **100% (60)** | **100% (60)** |

### 6.3 The honest reading

**The win is precisely where the rules cannot go.** On the four symptoms the generous rules
score 0/60, the model scores 60, 60, 57 and 56. It is not edging past a baseline on shared
ground; it is answering a population the baseline has no route to, because the licence is in
prose and the symptom is one `Diagnoser` names wrongly.

**slow_drift is the control, and it holds.** That symptom is in the set for one reason: it is
the case the rules get right. A model that had learned the cheap policy — *disagree with the
deterministic planner* — would score near zero on it. It scores 60/60, the same as the rules.
So the fine-tune learned the teacher's policy, not the complement of the baseline. That is the
specific failure mode ADR-036 shaped the task to expose, and it did not occur.

**Three forbidden actions, not zero.** Three of 300 plans (1.0%) contain an action the
teacher's policy forbids for that symptom, against 184 of 300 (61.3%) for the generous rules.
That is a 60-point reduction in proposed harm alongside the capability gain, and it is the
more interesting half of the result — the usual worry is that a more capable planner proposes
more dangerous things, and here it proposed fewer. But it is 1.0% and not 0.0%, and the
difference matters: on this evidence the model is **much safer than the rules, not safe**.

**The safety gate caught none of the three, and would not have.** `gate_rejected` is 0 across
all 300 model plans. The deterministic gate checks blast radius, action class and episode
state; it does not check whether an action is appropriate *to the symptom*, which is what the
teacher's forbidden set encodes. So the residual harm sits outside the gate's jurisdiction by
design (ADR-027, ADR-028). The gate is not a second line of defence against this class of
error and should not be described as one.

**The seven misses are two symptoms, not a spread.** All seven non-exact plans omitted a
wanted action, and they fall entirely on counter_rollover (3) and correlated_step (4). No
symptom's wanted and forbidden sets overlap, so a plan containing a forbidden action cannot be
exact, which places all three forbidden proposals inside those seven cases. Both symptoms
share a feature the other three lack: the correct plan turns on evidence from *outside* the
flagged channel — a rollover has to be told apart from a genuine counter reset, a correlated
step has to be checked against pipeline health. That is a plausible account of the residual,
not a measured one: the per-case replies were not retained from the cluster run.

**Latency: 15.8 s per plan, and it is not a deployable number.** p50 15.8 s, p95 21.1 s, range
9.3-22.2 s, 78 minutes for the full 300-case pass. That is roughly 1.7 million times the
rules' 0.009 ms, and it measures 4-bit sequential generation on a V100 at batch size 1 with no
serving stack — not the vLLM path Phase 5 specifies. The comparison is not like-for-like
either: the rules' figure times the `plan()` call alone, the model's a full generation. What
it does establish is that this planner cannot sit on the hot path in this serving
configuration. It does not need to — the agent plans *after* an episode is raised, off the
critical path, so the 250 ms budget in section 5.2 does not apply to it. No latency claim for
a served adapter is made here, because none has been measured.

### 6.4 What this does not establish

- **The ceiling is a hand-written policy.** 97.7% is agreement with `TEACHER`: five policies
  written by one person, with their justifications recorded. It is not agreement with ground
  truth. A teacher that is wrong about an operational question would be reproduced faithfully
  by this model and scored as correct.
- **Held-out means distribution-shifted, not independently sourced.** Both splits come from
  the same generator. The test split withholds the training channels, metric vocabulary and
  sentence templates, and states licences in prose the training split never uses — a real
  shift, and the one the rules fail on — but no operator wrote these runbooks, and the result
  does not transfer to real ones without being re-measured on them.
- **One adapter, one pass, one seed.** Decoding is greedy (`do_sample=False`), so the numbers
  are reproducible from this adapter, but nothing here estimates variance across training
  seeds. A second run could land somewhere in a range this measurement cannot bound.
- **fp16 and one epoch, both deviations from the B-3 spec** (bf16, three epochs). The
  measurement describes the adapter that exists, not the one the spec planned. ADR-039.
- **The adapter is not wired into the running agent.** `RemediationAgent` still defaults to
  `RunbookPlanner`, so every other number in this repository that involves a plan comes from
  the deterministic planner. D-6 is not yet reversed.

---

## 7. The VLM explainer: the local cost measured, the model cost not

Not a Q. There is no head-to-head here and no target being tested - this section reports what
a component costs and states, precisely, which half of that cost has been measured. It is in
this file rather than in `ARCHITECTURE.md` because the unmeasured half is a number a reader
will otherwise assume.

**What fires, and when.** The explainer renders a flagged window to a chart and asks a
vision-language model to read it (ADR-004, the VLM4TS screen-then-verify pattern). It runs on
episodes, never on windows, and not even on all episodes: an episode the conditioning policy
attributed to a deploy or a pipeline fault has already been explained by the context event, so
paying for a picture of one would be paying to explain the same thing twice. Only episodes
that would page a human are sent.

### 7.1 How rare the rare path actually is

Taken from the v4w run (`docs/results/paired-evaluation-v4w-topology-wide.json`) rather than
asserted: 900 s at 24 channels, 30 s windows sliding by 10 s, so 88 windows per channel and
**2,112 window scorings**. That run raised 147 episodes, of which 6 were attributed, leaving
**141 explanations** -- one API call per 15 window scorings, or 6.7%.

That ratio is the design working, and it is also the reason the ratio is not a general claim:
it is the density of *this* scenario, which schedules faults adversarially and is deliberately
harder than a real fleet (G-8). A quieter stream sends fewer.

### 7.2 Measured: the render, on the reference laptop

The half the platform owns. 30 repetitions per row after a warm-up, matplotlib Agg backend,
900x380 px at 100 dpi.

| Window plotted | Render p50 | Render p95 | PNG | Base64 on the wire |
|---|---|---|---|---|
| 120 samples | **54.0 ms** | 57.0 ms | 28.0 KB | 37.3 KB |
| 300 samples | **54.6 ms** | 112.9 ms | 31.9 KB | 42.6 KB |

Render cost is essentially flat in the number of samples over this range -- it is figure setup,
not plotting -- and the p95 at 300 samples is a garbage-collection artifact of the measurement
loop rather than a property of the size.

This runs on the explainer's own worker thread behind a bounded queue, so it is not on the hot
path and does not enter the 250 ms budget in section 5.2.

### 7.3 Not measured: the model call

**There is no `ANTHROPIC_API_KEY` in this environment, and no VLM endpoint key either.** The
Claude backend (ADR-041) is built, wired and tested, and it has never been run against
Anthropic. That means the following are **unmeasured**, not estimated:

- **NFR-2's 5 s explanation budget.** Unverified since B-1 and still unverified. What is known
  is that 54 ms of it is rendering and the rest is a network round trip to a model.
- **Token cost per explanation.** The output is capped at 400 tokens; the input is a ~28-32 KB
  PNG plus six lines of text, and what that comes to in image tokens has not been counted.
- **Whether the explanations are any good.** This is the older and larger gap (C-2). Judging
  explanation quality needs a judge - Ragas/DeepEval against a key - and a model reading its
  own colleague's chart description is not an evaluation.

To produce all three, set `ANTHROPIC_API_KEY` and run the detector on any scenario that raises
an episode; the explainer's counters print requested / explained / failed and a mean latency in
the run summary.

### 7.4 What the tests prove, and what they cannot

40 tests across `tests/test_explain.py` and `tests/test_claude_explainer.py`. **None of them
call Anthropic.** The Claude success path runs against an injected stub client and the
endpoint path against a local fake HTTP server, because a suite that spends money on every run
is a suite nobody runs, and because CI has no key either.

What that does establish: the request shape the SDK is handed (image block first, episode
numbers beside it, no sampling parameters -- current Sonnet rejects `temperature` with a 400),
the parse across multiple text blocks, the counters, and that every failure mode is a recorded
absence rather than an exception. A refusal, an empty answer, a transport error, a chart that
will not draw, and a missing key each have a test asserting detection is unaffected and the
episode carries a stated reason rather than invented prose.

What it cannot establish is everything in 7.3. A stub returns what the test told it to; it is
evidence about this code and none at all about the model.

### 7.5 What the explanation is allowed to do to the agent

Nothing, deliberately (ADR-042). The explanation is carried into `Diagnosis.evidence` as
`vlm_explanation`, where the trace and the operator see it. It does not touch the symptom or
the retrieval query, so no sentence a model wrote can change which runbook is found and
therefore which actions are licensed. A test feeds the diagnoser a deliberately misleading
explanation -- "certainly a safety interlock failure requiring immediate shutdown" -- and
asserts the symptom, query and summary come out byte-identical to the same episode with no
explanation at all.

This is the weakest of the three readings of the plan's "grounded evidence", and it is chosen
for a specific reason: section 6.3 established that the safety gate checks blast radius and
action class but **not** appropriateness to the symptom. Letting free text steer the symptom
would aim squarely at the one hole the gate does not cover. The consequence is that the
explainer currently helps the human and not the agent, which is a real limitation and not a
step on the way to something -- making it help the agent is a new decision needing its own
measurement.

---

## 8. The storage layer: what was measured, and the two defects measuring it found

Not a Q. Like section 7 this reports what a component does and costs, and it is here rather
than in `ARCHITECTURE.md` because two of its numbers contradict what a reader would otherwise
assume, and because both defects below were found by running the thing rather than by
reviewing it.

**The split** (ADR-006, built in ADR-043 to ADR-046). Postgres holds episodes and app state;
ClickHouse serves readings and per-window scores; Iceberg on MinIO is the durable record and
what reconciliation audits against. Nothing is mirrored between them.

### 8.1 What ran, on what

Reference hardware as section 1, Docker VM 8.13 GB / 16 CPU. Measured with the full default
stack up, which is what `docker compose up` starts:

| Service | Memory | Limit |
|---|---|---|
| Kafka | 910 MB | 2 GB |
| ClickHouse | 203 MB | 1 GB |
| MinIO | 67 MB | 512 MB |
| Postgres | 42 MB | 1 GB |
| **Total** | **1.2 GB** | of 8.13 GB |

Flink stays behind its compose profile. Its 4 GB on top of these would not fit, and that
combination is the one thing the compose file cannot run at once -- stated because "one
command brings it all up" is otherwise read as including Flink.

### 8.2 ClickHouse, measured

```
python loadgen.py --rate 400 --duration 20 --channels 6
python warehouse.py --from-beginning --stop-after-idle-s 12 --no-scores
```

8,000 readings, 6 channels, **0 undecodable, 0 write failures**, 2 batches, **mean write
262 ms**. Every channel's sequence span equals its reading count exactly -- the same identity
invariant the reconciliation ledger checks, computed independently on the serving copy.

### 8.3 Iceberg, measured, including the restart that is the whole claim

```
python lake.py --from-beginning --stop-after-idle-s 12
python lake.py --from-beginning --stop-after-idle-s 8     # the restart
python lake.py --verify
```

| Step | Result |
|---|---|
| First run | 8,000 readings, 1 snapshot |
| **Restart with `--from-beginning`** | **0 records read** -- the snapshot's offsets won over the flag |
| 3,600 further readings | resumed exactly at the boundary, 2nd snapshot |
| `--verify` | 11,600 rows, **0 duplicate rows, 0 sequence gaps** |

The restart is the measurement that matters. `--from-beginning` was passed deliberately and
ignored, because the offsets recorded in the current Iceberg snapshot are the ones that match
the stored rows (ADR-044). A sink relying on a Kafka offset commit would have re-read and
re-written all 8,000.

### 8.4 The reconciliation result: two counts, derived differently, agreeing

```
python reconciler.py --from-beginning --stop-after-idle-s 12 --audit-lake --no-emit --no-store
```

**6 of 6 channels agreed.** One count comes from streaming the Kafka log through the ledger;
the other from reading Parquet off object storage. They share no code and no state, which is
the only reason agreement between them is worth anything.

The same run also shows both halves reporting the *same* underlying event in their own terms,
which is the clearest evidence the two views are independent:

| | What it reported |
|---|---|
| Live ledger | `drift -3600 ... reordered 3600` -- the sequence regressed, which from the stream's point of view it did |
| Lake audit | `gaps 0`, `duplicates 0`, `reused-seq 3,600` -- no data missing, nothing written twice, 3,600 readings sharing a seq |

Both are correct. The cause is in 8.5.

### 8.5 Two defects that only appeared under real data

**The rollup counted rows written, not readings.** A ClickHouse materialized view fires on the
rows being inserted and never sees the `ReplacingMergeTree` dedupe that happens later at merge
time. With `count()`, replaying a 100-reading batch once made the per-minute rollup report
**200**. `uniqExact` over the identity is immune, and is now what the rollup stores; full and
partial replays both leave it at 100. A test pins it.

`value_avg` remains vulnerable and the schema says so: sum and count both inflate on a replay,
so the mean is exact when a partition was written once or replayed whole, and biased when a
replay covered part of it. Anything that cannot tolerate that reads `readings_exact`.

**`(channel, seq)` is not unique across producer restarts.** The generator restarts its
per-channel sequence at 1, so the lake held two genuinely different readings -- eight minutes
apart, different values -- both carrying `seq=5`. Keyed on `(channel, seq)`, ClickHouse would
have **deleted the older one at merge time and reported it as a successful dedupe**: data loss
presented as correctness, which is the worst shape a defect can take here.

Identity in both stores is now `(channel, seq, event_ts)` (ADR-046). A redelivered record
carries an identical timestamp and still collapses; distinct readings no longer do. The lake
audit reports the two causes separately, because one is a fault in the sink and the other is a
fact about the producer.

This also narrows a claim in `docs/CORRECTNESS.md` section 2, which stated the per-channel
sequence as an identity without qualification. It is dense within a producer lifetime, not
across a stored table's history, and the document now says so.

### 8.6 What this does not establish

- **No throughput number for either sink.** 262 ms per 4,000-row batch is a write latency on
  an idle laptop, not a sustained ingest rate, and neither sink has been run against the
  76,556 ev/s the producer can reach (`docs/SCALE.md`). The scale harness has not been pointed
  at them.
- **One partition's worth of failure testing.** The effectively-once claim was verified by
  restarting the process cleanly. It has not been verified by killing it mid-commit, which is
  what the chaos suite does to Flink and what would actually exercise Iceberg's commit
  atomicity rather than trusting it.
- **No retention, compaction or expiry policy.** The lake grows without bound, small files
  accumulate one per commit, and nothing expires old snapshots. At this data volume none of
  that binds; all three are real operational work that has not been done.
- **The catalog is a single point of failure for two stores.** ADR-043 puts the Iceberg
  catalog in the application Postgres, so that database being down takes app state and the
  lake's catalog with it. Accepted deliberately, and named here rather than only in the ADR.

---

## 9. The deployment layer: validated configuration, nothing applied

Not a Q, and not a measurement of the running system. This section exists because the
repository now contains Kubernetes manifests, Terraform and monitoring configuration, and
those are the artifacts most easily mistaken for evidence that something was deployed.
**Nothing here has run on a Kubernetes cluster.** No kind or minikube cluster was created, no
pod was ever scheduled, no Prometheus has scraped anything.

### 9.1 What was validated, with what, and what it returned

Re-runnable: `python scripts/validate_deploy.py --report docs/results/deploy-validation.txt`.
Transcript committed at that path.

| Check | Tool | Result |
|---|---|---|
| 25 resources across 11 manifests | `kubeconform -strict`, k8s 1.31.0 | **Valid 25, Invalid 0, Errors 0** |
| Rendered overlay | `kubectl kustomize k8s` | 19 resources rendered, exit 0 |
| IaC type-check | `terraform validate` | "Success! The configuration is valid." |
| IaC formatting | `terraform fmt -check` | clean |
| Alert rules | `promtool check rules` | 10 rules found, exit 0 |
| Scrape config | `promtool check config` | valid syntax, 1 rule file |
| Alert routing | `amtool check-config` | SUCCESS, 2 inhibit rules, 2 receivers |
| Dashboard JSON | parse | 9 panels, 9 expressions |
| Dashboard PromQL | `promtool check rules` on the extracted expressions | 9 valid |
| Panel/rule metric names against the exporter | cross-check | **9 referenced, 9 exported, 0 phantom** |

**10 passed, 0 failed, 0 skipped.** Tool versions: kubectl 1.37.0, kubeconform (latest),
terraform 1.16.2, promtool 3.14.0, amtool 0.34.0, installed to `E:\tools` rather than
system-wide.

The application image was built rather than only described: **900 MB**, all four CLI
entrypoints parse, the package imports, and it runs as non-root uid 10001.

### 9.2 The tool that could not participate, and why it matters

`kubectl apply --dry-run=client` is the validation step most people would expect here, and it
**cannot run offline**. It performs API discovery against a live server before validating
anything, so on a machine with no cluster it fails with a connection error that says nothing
about the manifests — and it fails that way even with `--validate=false`.

This is worth stating rather than quietly substituting a different tool, because "validated
with kubectl --dry-run" is a claim that sounds stronger than kubeconform and, on a machine
with no cluster, cannot have been made. kubeconform validates against the published
Kubernetes JSON schemas offline, in `-strict` mode, which rejects unknown fields — a typo
like `resource:` for `resources:` is caught. That is what did the work.

### 9.3 What static validation does not establish

Every item below is unverified, not merely untested-so-far:

- **That the stack comes up at all.** Start ordering, crash-loop behaviour before the stores
  are ready, and whether the probe timings are right are all unobserved. There are no init
  containers gating consumers on store readiness, so first apply is expected to restart pods.
- **That the probes pass against the real containers.** The ClickHouse probe is `httpGet
  /ping`, deliberately different from the compose `wget` form because the compose healthcheck
  failed on an IPv6 detail. The reasoning is sound and has not been observed working.
- **Resource requests and limits.** Extrapolated from the measured compose footprint (1.2 GB
  across four services). Whether the consumers fit their limits under load is unknown — the
  scale harness has never been pointed at a cluster.
- **Storage.** No StorageClass was exercised. On a cluster without a default StorageClass the
  `volumeClaimTemplates` leave every store Pending, and nothing checks for one.
- **Terraform apply.** `validate` type-checks the configuration; it never contacts a cluster.
  The provider has not authenticated and no resource has been created.
- **Any alert firing or resolving.** The rules parse. No Prometheus instance has scraped the
  API, so the `for:` durations are unexercised and no alert has moved through its lifecycle.
- **The dashboard rendering.** The JSON parses and every expression is valid PromQL over
  metrics that exist. Grafana has never loaded it.

### 9.4 Gaps that are structural rather than untested

Two things will not work even once this is applied, and both are deliberate:

- **Consumer-group lag alerting cannot fire.** Nothing exports
  `kafka_consumergroup_lag`; that needs a Kafka exporter the manifests do not deploy. The
  rule is kept, valid, labelled `requires-kafka-exporter` and routed to a null receiver, so
  the gap is visible rather than discovered when an alert never arrives (ADR-049).
- **Four workloads have no liveness probe.** The detector, reconciler and both sinks restart
  on process exit and not on a hang. Every available probe was worse than none — `exec: true`
  is decoration, and probing Kafka turns a broker outage into a crash-loop. The honest fix is
  a heartbeat file written by each poll loop, which is application work that has not been
  done (ADR-048).

### 9.5 What did not change

The compose stack is untouched. `docker compose up -d` still brings up Kafka, Postgres,
ClickHouse and MinIO, and section 8's measurements still hold — the deployment layer is
additive, and the thing that demonstrably runs is still the thing that ran before.

---

## 10. Honesty rules held in this document

- Every number states the hardware and the command that produced it.
- A target that is missed is reported as missed, not quietly re-scoped afterwards. Revisions to
  targets are made *before* measurement and carry an ADR (ADR-016, ADR-017).
- Losses are reported with the same prominence as wins.
- "Not yet measured" is used rather than an estimate.
