# Blockers, deferrals and assumptions

> Anything that needs a human decision, is stubbed, or was defaulted under BUILD.md's
> blocker protocol. Each entry states what was assumed, so the assumption can be overturned
> cheaply. "None" is a valid state for this file.

## Open -- needs a human

> **Actually open: B-5 and B-6.** B-3 and B-4 are answered and are kept here in place, with
> their resolution at the top of each entry and the reasoning that preceded it below it --
> the history is what makes the answer checkable.

### B-3. RESOLVED 2026-09-13. Trained on the Explorer cluster, and the model beat the baseline.

**The answer.** `python evaluate_planner.py --adapter artifacts/planner-qlora` exits 0 only if
the adapter's exact-match beats the generous baseline. It exited 0. On the same 300 held-out
cases, raw result in `docs/results/planner-eval.json`:

| Planner | Exact match | Proposed a forbidden action |
|---|---|---|
| rules, **as deployed** | 0.0% (0/300) | 0.0% |
| rules, **given licence sets** | 20.0% (60/300) | 61.3% (184) |
| **QLoRA Qwen2.5-7B-Instruct** | **97.7% (293/300)** | **1.0% (3)** |
| teacher policy | 100% by construction | 0% |

That closes 97.1% of the 80-point gap between the generous baseline and the teacher. The
per-symptom breakdown, the three residual forbidden actions, the 15.8 s p50 generation cost and
the limits this does **not** establish are in `docs/EVALUATION.md` section 6. ADR-039 records
what had to change to run at all; ADR-040 records why the reshaped task is what made the win
possible.

**The one real setup constraint, for anyone reproducing this.** The training environment needs
**Python 3.12 with torch 2.5.1+cu121**. That pin is not cosmetic: bitsandbytes' 4-bit kernels
and the triton build they depend on are what break outside it, and they break at import or at
the first quantised matmul rather than at install time, so a mismatched environment looks fine
until the GPU allocation has already been spent. The full working set, pinned in
`run_planner_fast.sbatch`: torch 2.5.1, transformers 4.46.3, peft 0.13.2, trl 0.12.0,
bitsandbytes 0.45.0.

**Two deviations from the spec below, both deliberate and both recorded in ADR-039.** The run
used **fp16, not bf16** -- the available card was a V100 and Volta has no native bf16 -- and
**one epoch (75 steps), not three (225)**. The bf16 caution further down stands as a correct
prior warning; it happened not to bite, with no NaN in any of the 75 logged steps.

**What is left is deployment, not training.** The adapter is not wired into
`RemediationAgent`, which still plans with the deterministic rules, so D-6 is not yet reversed
and every other plan-derived number in this repository is still the rules'.

---

**The history below is kept as written.** It is what the blocker said before the run, and the
reasoning that produced the task the model was measured on.

**Answered 2026-09-07 by reshaping the task (option 2), not by dropping it.** The original
objection stands and is preserved below; what changed is the task, not the verdict on the old
one.

**The original finding (unchanged).** The first training set had five distinct target
sequences over 1,200 examples, each a deterministic function of a symptom `Diagnoser` computes
before the prompt is rendered and then puts in the prompt. A model trained on that learns a
five-way classification whose answer is one of its own inputs; it can approach the
deterministic planner and cannot beat it. `artifacts/planner-dataset/easy-reference.jsonl`
keeps that set as the evidence.

**What the task is now.** `src/vigil/tuning/hard.py`, ADR-036:

- The prompt carries the detector's **per-window evidence** and withholds the symptom, the
  diagnosis summary and the licence list.
- The five shapes are outside what `Diagnoser` can name. It has no fall-through, so they are
  **misclassified rather than abstained on** -- an oscillating channel reads as a variance
  burst and gets silenced while the control loop is unstable.
- Retrieval returns the matching entry plus **two distractors**, so the entry has to be
  chosen from the evidence before its licence can be read.
- The held-out split states every licence **in prose**, on channels and metric vocabularies
  the training split never contains, with sentence templates the training split never uses.

**The measured gap, on 300 held-out cases** (`python evaluate_planner.py`, raw result in
`docs/results/planner-baseline.json`):

| Planner | Exact match | Forbidden action | Note |
|---|---|---|---|
| rules, **as deployed** | **0.0%** | 0.0% | cannot parse a prose licence, so it escalates on everything |
| rules, **given licence sets** | **20.0%** | **61.3%** | generous: handed licences the live parser could not extract |
| teacher policy | 100% by definition | 0% | the ceiling; a model can reach it and not exceed it |

Both baselines are reported because the first alone would flatter the fine-tune and the second
alone would flatter the rules. Per symptom, the generous baseline is right on slow drift
(60/60) and wrong on all four others (0/60 each) -- slow drift is in the set precisely because
the rules handle it correctly, so a model that has learned to answer "not the rules" fails it.

**What is built, and verified without a GPU.**

- `build_planner_dataset.py` -- writes `train.jsonl` (1,200) and `test.jsonl` (300) plus a
  manifest, and exits non-zero if the splits share a channel.
- `src/vigil/tuning/qlora.py` -- the configuration, quantisation and trainer assembly.
- `train_planner.py` -- the training entry point, with `--dry-run`.
- `evaluate_planner.py` -- scores any planner on the held-out split against both baselines.
- 25 tests, including one that **fails if the deployed planner stops being wrong here**,
  because that would remove the headroom and make the fine-tune pointless again.

`python train_planner.py --dry-run` passes on the reference laptop against the real
Qwen2.5 tokenizer: prompts p50 **1,136** tokens, max **1,261**; completions p50 239, max 320;
**0 examples over the 2,048-token limit**; 225 optimizer steps for 3 epochs at an effective
batch of 16.

**Nothing here has been trained.** No number in this repository comes from an adapter.
*(True when written on 2026-09-07. Superseded by the run at the top of this entry: the
numbers in `docs/EVALUATION.md` section 6 come from an adapter, and say so.)*

### The exact run, for your cluster

```bash
# 1. environment (one GPU, see the requirement below)
git clone <this repo> && cd vigil
python -m venv .venv && . .venv/bin/activate
pip install -e ".[train]"

# 2. regenerate the dataset from the seed (deterministic; do not copy artifacts/ across)
python build_planner_dataset.py
python train_planner.py --dry-run          # must print DRY RUN PASSED before spending GPU time

# 3. train
python train_planner.py --base-model Qwen/Qwen2.5-7B-Instruct

# 4. score the adapter against both baselines on the held-out split
python evaluate_planner.py     --adapter artifacts/planner-qlora     --base-model Qwen/Qwen2.5-7B-Instruct     --report-json docs/results/planner-qlora.json
```

`evaluate_planner.py` exits 0 only if the adapter's exact-match beats the generous baseline,
so the shell's exit status is the answer to B-3.

**GPU requirement.**

| | |
|---|---|
| Minimum | **16 GB** VRAM (T4 will not do it -- no bf16; use A10, L4, RTX 3090/4090, A5000 or better) |
| Comfortable | **24 GB**, which is what the defaults are set for |
| Base model | Qwen2.5-7B-Instruct, ~15 GB download, ~4.5 GB resident in NF4 |
| Precision | bf16 compute, NF4 double-quantised weights, paged 8-bit AdamW |
| Sequence length | 2,048 (measured maximum need: 1,261 + 320) |
| Trainable parameters | LoRA r=32 on attention and MLP projections, roughly 0.5% of the model |
| Estimated wall clock | **20-40 minutes** for 225 optimizer steps on a 24 GB card. An estimate from step count and typical throughput, **not a measurement** |
| Disk | ~25 GB for the base model cache plus ~1 GB for adapters and checkpoints |

If bf16 is unavailable, pass `--max-seq-length 1536` and expect fp16 instability on this task;
the loss is dominated by a few confident tokens and fp16's narrower exponent range is where
silent NaNs come from.

**What was reported back.** Step 4's output, in full, as `docs/EVALUATION.md` section 6 --
both baselines, the adapter's score, the per-symptom breakdown and the exact-match delta.
The delta was positive; had it been negative that would have been the finding and it would
have gone in the same place.

### B-4. Reopened and re-answered by v6. The signal that moved the number needs no external cause at all. NFR-8 still not met.

**v6, 2026-09-18.** The diagnosis that held through five attempts -- context conditioning can
only suppress a false page that has an attributable cause, and most of them do not -- is
correct, and it turned out not to be a statement about conditioning. The platform already runs
a second detector on the same windows (ADR-017); consulting it needs no deploy and no pipeline
event. Used as a corroborator that raises no episodes of its own (ADR-050), on the same seed,
density and command as v4:

| Run | FP reduction | Recall loss | Quiet-window recall | NFR-8 |
|---|---|---|---|---|
| v4, 12 ch (context only) | +18.9% | -3.3% | +0.0% | missed |
| v4w, 24 ch (context only) | +3.6% | -3.3% | +0.0% | missed |
| **v6a, 12 ch** | **+24.0%** | **+0.0%** | +0.0% | missed |
| **v6aw, 24 ch** | **+35.4%** | **-3.3%** | +0.0% | missed |

Both runs carry a fourth pass in which the second detector runs, is waited for, records what it
saw and may not act on it (ADR-051), so its contribution is an isolation rather than a
comparison across runs:

| Ablation, byte-identical records, one run | FP reduction | Recall loss |
|---|---|---|
| 12 ch, advisory -> acting | +6.0% -> **+24.0%** | +0.0% -> **+0.0%** |
| 24 ch, advisory -> acting | +9.7% -> **+35.4%** | -6.7% -> **-3.3%** |

**What is new, and it is not the headline.** At 24 channels the signal improved **both halves at
once** -- 25.7 points of reduction *and* the recall loss halved. Every run from v1 to v4w traded
one half against the other. The mechanism is the veto: of the eleven episodes the v4w policy
attributed to a deploy, agreement pulled back ten, and seven of the eleven overlapped a real
fault. A timing policy cannot tell a seizing pump from a rollout (section 3.8); a second
detector looking at the same values often can tell that something is genuinely there.

**What it costs, at both resolutions.** 12 ch: 2 true pages, 0 incidents, channel-episode recall
89.5% -> 86.0%. 24 ch: 6 true pages, 1 incident, 84.2% -> 77.2%, and the lost incident is a
single-channel fault rather than a machine or a cabinet. Quiet-window recall is 7/7 in both.
Fail-open held 76/76 and 145/145.

**NFR-8 is still missed, and the reason is now specific.** +35.4% against 40% is a gap of about
six false pages, not of a ceiling (see B-6 below). The 34 `unexplained` pages that survive are
ones **both** detectors see: both are right that the signal moved, and neither can see that it
moved for no reason. Closing that needs a third kind of evidence, not a tuning of this one.
Full detail in `docs/EVALUATION.md` sections 3.12 to 3.14; the agreement line was fixed before
the run and the whole sensitivity curve is published beside the result.

---

**Closed 2026-09-07.** Option 2 was taken: the defect was fixed (ADR-035) and the run
repeated. All four measurements are published side by side in `docs/EVALUATION.md`
sections 3.4-3.7.

| Run | What changed | FP reduction | Recall loss | NFR-8 |
|---|---|---|---|---|
| v1 | co-occurrence in a 30 s window | +60.9% | -36.7% | missed |
| v2 | synchrony, compared on window starts | +9.0% | -10.0% | missed |
| low density | 20 deploys/hour | +6.7% | -6.7% | missed |
| **v3** | synchrony, compared on **true onsets** | **+11.1%** | **-6.7%** | **missed** |
| v4a | evidence made present, same criterion (ADR-037) | +27.8% | -16.7% | missed |
| v4 | blast-radius discriminator, 12 ch (ADR-038) | +18.9% | **-3.3%** | missed |
| v4w | blast-radius discriminator, 24 ch | +3.6% | **-3.3%** | missed |
| **v6a** | cross-detector agreement, 12 ch (ADR-050) | **+24.0%** | **+0.0%** | missed |
| **v6aw** | cross-detector agreement, 24 ch | **+35.4%** | **-3.3%** | missed |

**What v4 answered (2026-09-07).** The root cause was a distributed-systems defect, not a
statistical one, and fixing it did not rescue the target -- it changed which half fails.
The corroboration test was deciding before its evidence arrived (G-7) and reporting a
verdict it had not reached (G-16). With both fixed, in-scope siblings are present in 58 of
69 scoped decisions, `isolated` fires 46 times where four runs reported zero, and the timing
criterion -- finally tested on complete data -- attributes real faults, because a seizing
pump is synchronous inside its own scope. The blast-radius discriminator then does what it
claims: on byte-identical records at 24 channels it halved the recall loss, from 6.7% to
3.3%, at a cost of 4.4 points of reduction. **The recall half of NFR-8 is met for the first
time in five measurements; the reduction half is missed, and B-6 shows it was arithmetically
unreachable on that run.** Full detail in `docs/EVALUATION.md` sections 3.8 to 3.11.

**What the fix bought.** v3 against v2: +2.1 points of reduction and 3.3 points less recall
loss, on the same seed and density with nothing else changed. Real and in the expected
direction. Not a rescue.

**What it settled.** The synchrony hypothesis is no longer untested. `corroborated=7,
implausible=69, isolated=0` over 76 episodes: the corroboration test never concluded a
channel had moved alone, mostly because the siblings that would exonerate it had not closed
yet when its turn came (G-7). Two of its seven attributions still cost a real fault, so the
collateral alone (6.7%) exceeds the 5% tolerance before the 40% target is considered.

**Superseded 2026-09-07 by v4.** The paragraph below said no fourth attempt was planned and
named two candidates; the second of them, "a different discriminator altogether", is what v4
built. Kept as written -- the reasoning was sound on the evidence then available, and one of
its premises turned out to be false.

**No fourth attempt is planned.** Reaching 40% with this policy would need a corroboration
index over completed windows rather than closed episodes, or a different discriminator
altogether -- magnitude and direction agreement across scope rather than timing. Either is a
new hypothesis, not a refinement of this one, and the honest place to stop is with the
negative result published and the mechanism that *does* work (plausibility, 69 of 76 episodes
correctly raised; fail-open, 76 of 76) reported separately.

### B-5. NFR-3 asks for event-to-flag in 2 s, which the window geometry makes impossible. Restate it or change the geometry?

**What was found.** NFR-3 in `docs/REQUIREMENTS.md` reads "Event -> flag in <= 2 s p99". It
has never been measured and no document discusses it. It also cannot be met as written: the
detector scores **30-second windows sliding by 10 seconds**, so an event cannot be flagged
before the window containing it closes. The floor on event-to-flag latency is therefore one
slide (10 s) at best and one window (30 s) at worst, before any processing cost -- and the
processing cost itself is 0.15 ms p99. The requirement is five to fifteen times below its own
lower bound.

**The options.**

1. **Restate NFR-3 as pipeline overhead**: "an episode is raised within 2 s of its window
   closing". That is the part the platform controls, it is measurable, and the windowing
   delay is then reported separately as a property of the geometry rather than hidden inside
   a latency number. Recommended.
2. **Shrink the geometry to meet the number.** A 2-second window at 1 Hz per channel has two
   samples in it; a z-score over two samples is noise, and the foundation model has no
   context to forecast from. This buys the number by destroying what it measures.
3. **Record NFR-3 as not met** and leave it. Honest, but it leaves a requirement in the spec
   that nothing can ever satisfy, which is worse than one that is wrong.

**Recommendation: 1.** Not taken autonomously because it edits a requirement rather than an
implementation, and because the separately-reported windowing delay is a number the architect
should choose to stand behind. Either way the measurement itself is the same work.

**Note added by v4.** The verdict barrier (ADR-037) adds a configurable event-time delay --
30 s by default -- between an episode closing and its conditioning verdict. Measured, it
delayed only 1 of 144 verdicts past the moment the episode closed, because an episode does
not close until its channel has been quiet for two window slides and the buffer has usually
already elapsed by then. Whatever NFR-3 is restated to, that delay belongs in the statement.

### B-6. Largely answered by v6, and by the option nobody recommended. Still open on one point.

**v6, 2026-09-18. The arithmetic was right and it was not a ceiling on conditioning.** B-6
computed that only 37 of 112 false pages on the v4w run overlapped an injected artifact, so no
*context* signal could remove more than 33% of them. That remains exactly true, and it is true
on the v6aw run too: 42 of 113 are artifact-driven, a context ceiling of 37.2%.

The v6aw policy reached **35.4%**, and it got there by the opposite route -- it removed **37 of
the 71 un-attributable pages (52%)** and only 3 of the 42 artifact pages (7%). A second detector
consulted about the same windows needs no context event to have an opinion (ADR-050), so the
denominator B-6 was worried about never bound it.

**What that does to the three options.**

1. **Report against the attributable subset.** No longer necessary to make the number
   interesting, which is the best possible outcome for a proposal whose main objection was that
   it changes a metric's meaning after five misses. **Declined, and not because it is wrong** --
   because it is now unneeded, and the honest denominator was always the whole population.
2. **Reduce the unexplained pages.** This is what happened, and it did **not** need the detector
   changed: a threshold was not moved, the foundation model was not substituted for the z-score,
   and the episode population is identical to the shadow pass. It needed the second detector
   *consulted* rather than replaced. This is the option that worked.
3. **Leave NFR-8 as written and keep reporting it missed.** Also what happened, and now cheap:
   the gap at 24 channels is +35.4% against 40%, about six false pages.

**What is still open, and it is a smaller question than the original one.** Six pages is close
enough that the temptation to tune is real, and the agreement line is exactly the knob that
would do it -- at a bar of 6.0 instead of 3.0 only 27 of 135 spans agree, so more would be
suppressed. The line was fixed before the run and the whole curve is published (section 3.13) so
that this cannot happen quietly. **The architect's remaining call is whether a sweep of that
line is a legitimate calibration or a tuned result**, and the implementer's view is that any
sweep must be scored on a *different seed* from the one it is chosen on, or it is the latter.
That is a protocol decision, not an implementation one.

The 34 surviving `unexplained` pages are ones **both** detectors see. Both are right that the
signal moved; it moved because of AR(1) noise, and cross-detector agreement is structurally
blind to the difference between a real excursion with a cause and a real excursion without one.
Closing the last 4.6 points needs a third kind of evidence, not more of this one.

---

**The original entry, kept as written.**

**What was found, and it should have been computed four runs ago.** NFR-8 asks for a >= 40%
reduction in false pages. A conditioning policy can only remove a false page by attributing it
to a context event, so it can only ever touch pages that overlap an injected artifact. On the
v4w run, **75 of 112 false pages are `unexplained`**: they overlap no injected excursion at
all, so nothing in the context topic could account for them however good the discriminator is.
The ceiling is 37/112 = **33%**, achieved only by attributing every artifact page and never
being wrong. At 12 channels the ceiling is 35/53 = 66%, and the measured +18.9% is 29% of it.

*(The premise in the first sentence -- "can only remove a false page by attributing it to a
context event" -- is the one v6 falsified. It was true of every policy that existed when it was
written.)*

**The options.**

1. **Report the reduction against the attributable subset** -- false pages that overlap an
   injected artifact -- and report the unexplained population separately as a detector
   property. Defensible, and it measures the thing the policy actually controls. It also
   makes the headline number larger, which is exactly why it must be argued for rather than
   adopted: changing a denominator after four failures to hit it needs a reason that is not
   "the old one was unflattering".
2. **Reduce the unexplained pages at the detector.** They are z-score false positives on
   AR(1) noise, so this is a detection problem: a higher threshold, or the foundation model,
   or both. It leaves NFR-8 as written and attacks the real cause, and it changes the
   shadow baseline every conditioning result is measured against.
3. **Leave NFR-8 as written and keep reporting it as missed.** Honest, costs nothing, and
   the +3.6% headline then describes the run's false-positive mix as much as the policy.

**No recommendation offered.** This one changes what a requirement means after it has been
missed five times, and the reason to prefer any option is a judgement about what the number
is for. That is the architect's call, not the implementer's.


## Decided by the architect

| # | Question | Decision | Consequence |
|---|---|---|---|
| B-1 | How should the VLM explainer be served, given 4 GB of VRAM and no API key? | **Hosted endpoint behind env vars** (`VLM_ENDPOINT`, `VLM_API_KEY`, `VLM_MODEL`). **Extended 2026-09-13 by ADR-041**: Claude is now the preferred backend when `ANTHROPIC_API_KEY` is set, and this one is kept beside it rather than replaced. | Built against that interface on 2026-09-06 -- this row previously said so before it was true. With no key set it reports itself unavailable and detection is unaffected -- the documented degradation, not a stub pretending to work. Explanations appear the moment a key is supplied, and NFR-2's 5 s budget is measured against the real endpoint rather than assumed. Cost accepted: a per-flagged-window API cost and a third-party dependency on the rare path only. |
| B-2 | Is the QLoRA fine-tune worth attempting on this hardware? | **A GPU will be rented**, so the fine-tune proceeds as the plan specifies. **Reopened as B-3** now that the dataset is built and its target entropy measured. | The dataset builder and training script are written now so the rented time is spent training rather than authoring. Until the GPU is available the agent runs the deterministic planner, which stays as the baseline the fine-tuned model is measured against rather than merely replaced by. |

## Defaulted (proceeding under a recorded assumption)

| # | Item | Default taken | Recorded in | Reversible by |
|---|---|---|---|---|
| D-1 | The plan left the live feed "TBD" | Public TDengine solar-fleet MQTT feed, topic `inverters` | ADR-010 | Change `MQTT_*` in `.env` and add a `TopicMapping` |
| D-2 | The feed offers no history API, so FR-1's REST backfill cannot be built | Gap **detection** only; edge guarantee restated as at-most-once | ADR-011 | Only by switching to a feed with history |
| D-3 | Inverters publish no expected-power field | Use `PR_Local` as the normalised residual for that topic | ADR-012 | Swap the `primary=True` metric in the mapping |
| D-4 | PyFlink publishes no wheel for Windows on Python 3.12 | Run Flink as compose services behind a profile, job submitted to the cluster | ADR-022 | Nothing to reverse; this is also the deployment ARCHITECTURE.md describes |
| D-5 | A faithful VUS-PR is subtle enough that a half-right version is a real risk | Dropped it; report detection latency and event-level rate beside point recall instead | ADR-023 | Implement the decayed-buffer weighting properly and re-add |
| D-6 | The agent's planner needs a model that will not fit here | Deterministic rule-based planner behind a `Planner` protocol | ADR-026 | Implement the protocol with the fine-tuned model once the GPU is available; the gate is unchanged either way |

## Known constraints on this machine

| # | Constraint | Affects | Status |
|---|---|---|---|
| C-1 | RTX 3050 Ti Laptop, 4 GB VRAM. A 7–8B model will not fit at fp16. | Phase 4 VLM, Phase 5 QLoRA | **Resolved by B-1 and B-2**: VLM served remotely, fine-tune on rented hardware |
| C-2 | No API key of any kind in the environment -- **including no `ANTHROPIC_API_KEY`**. | Phase 4 VLM, Phase 5 judged eval | **Waiting on a key, and now waiting on a specific one.** The explainer is built with two backends behind one contract (ADR-041): Claude through Anthropic's Messages API when `ANTHROPIC_API_KEY` is set, the hosted OpenAI-compatible endpoint otherwise. 40 tests, with the Claude success path against an injected stub and the endpoint path against a local fake; with neither key it reports itself unavailable and detection is unaffected. Setting `ANTHROPIC_API_KEY` is now the whole of what stands between here and three measurements: NFR-2's 5 s budget, the token cost per explanation, and whether the explanations are any good (that last one also needs a judge) |
| C-3 | Docker VM has 8.1 GB of the machine's 15.6 GB. | Phases 2–3 | Managed: Flink is behind a compose profile so JobManager + TaskManager (~2.5 GB) are started deliberately rather than always. ClickHouse and MinIO will need the same treatment. |
| C-4 | Long-running measurements (the >= 4 h soak, the 200-series benchmark, the parallelism sweep) each need the machine to themselves. | Phases 2, 5 | Sequenced rather than parallel. The soak must run after chaos and scale, both of which deliberately break or saturate the stack. |

## Known gaps in what has been proven

Recorded here rather than only in the docs that would flatter themselves by omitting them.

| # | Gap | Where it is stated |
|---|---|---|
| G-1 | ~~The Flink exactly-once path has never been fault-tested.~~ **Closed 2026-09-06.** The TaskManager was SIGKILLed mid-checkpoint and held down 60 s: 58/58 samples unhealthy, job restored from checkpoint 5 across 7 restore cycles, recovered in 14.9 s, and a `read_committed` consumer saw **328 distinct window scores with 0 duplicates**. Still one kill, one job, one TaskManager. | `docs/CHAOS.md` section 2.1, `docs/CORRECTNESS.md` section 3a |
| G-2 | ~~Zero drift is measured over minutes, not hours.~~ **Closed 2026-09-07.** The 4-hour soak ran: **5,749,412 readings, 240.0 minutes, drift 0**, missing 0, duplicates 0, reordered 0, zero at all sixteen checkpoints. | `docs/CORRECTNESS.md` section 4a |
| G-14 | The soak's **broker audit** reports +77, because both processes were given the same fixed duration and the consumer's clock expired while the producer was still producing -- 77 records had landed and not yet been polled. The ledger claim is unaffected, and shorter runs that drain the consumer report +0, which is the cleaner protocol. A re-run that stops the producer first would make the audit exactly zero. | `docs/CORRECTNESS.md` section 4a |
| G-3 | The chaos suite's producer absorbed every outage from its retry buffer, so the hold length at which loss becomes unavoidable has not been found. Finding that boundary would be a stronger result than not reaching it. | `docs/CHAOS.md` section 5 |
| G-4 | Single broker at replication factor 1: no leader election, no ISR shrink, no partial-availability case. Recovery times do not project to a cluster. | `docs/CHAOS.md` section 5 |
| G-5 | The TSB-AD benchmark scores at **window** resolution, which is a coarser task than point-level TSB-AD scoring. Numbers are not comparable to point-level leaderboards. | `benchmark.py` module docstring, `docs/EVALUATION.md` section 4 |
| G-6 | The multivariate fold is max-across-columns, so an anomaly that exists only in the *correlation* between features — where every column alone looks normal — cannot be detected. Such anomalies are in the corpus. | `benchmark.py` module docstring |
| G-11 | The benchmark scored **144 of 200 series**. The other 56 were excluded by the 20,000-point truncation because their labelled anomalies begin later, so the scored corpus is the early-onset half. An untruncated run is estimated at 4+ hours of CPU and has not been done. | `docs/EVALUATION.md` section 4.3 |
| G-12 | Only Chronos-Bolt-**tiny** was benchmarked, chosen to fit the laptop. A larger checkpoint may close the 141x-cost gap; the claim is about this model at this size, not about foundation models generally. | `docs/EVALUATION.md` section 4.3 |
| G-13 | **CI has never actually run.** The repository has no remote, so no GitHub Actions workflow has ever executed. Every step was run locally and passes -- lint, format, 722 tests, the secret scan, the repo-standards checks and the agent quality gate -- but "CI green" means "green when run by hand here", not "green on a runner". | `.github/workflows/ci.yml`, this table |
| G-7 | ~~Conditioning is applied as episodes close, so an episode closing early sees fewer potential corroborating siblings than one closing late.~~ **Closed 2026-09-07 (ADR-037).** The corroboration index is filled when an episode *opens*, and the verdict waits behind an event-time barrier keyed to the fleet watermark. Measured on the v4a control: in-scope siblings present somewhere in the episode's span in **56 of 83** scoped decisions, mean 2.47 -- evidence that was not there before. The consequence was not the expected one: with the evidence present the corroboration test fires three times as often and takes real faults with it, which is what ADR-038 exists to answer. | `docs/EVALUATION.md` section 3.8 |
| G-15 | **The generator that places the faults and the policy that reads the topology share a model of the world.** The blast-radius discriminator is scored on data built to have the structure it looks for. That measures whether the mechanism works given the premise -- that deploy rings cut across failure domains and that a machine's metrics fail together -- not whether the premise holds in a real fleet. The premise is an operational claim about deploy practice and is stated in ADR-038 so it can be argued with. | ADR-038, `docs/EVALUATION.md` section 3.9 |
| G-16 | The recorded **verdict** was not the verdict the corroboration test reached. When several context events overlap an episode, the policy kept the last non-out-of-scope rejection, and pipeline health events sort last and answer `implausible` -- so an `isolated` conclusion from the deploy test was overwritten in the record every time a health event also overlapped, which is almost always. That is why every published run reports `isolated=0`. The decision was never affected (both verdicts raise the episode) and no measured number changes, but the diagnosis in sections 3.4 and 3.7 rested on a field that could not report the thing it was read as reporting. | `docs/EVALUATION.md` section 3.8 |
| G-17 | **The v6 runs cannot produce a clean v4-to-v6 delta on the context half.** The advisory (ablation) pass runs the v4 policy and scores +6.0% at 12 channels where v4 published +18.9% for the same policy on the same command. Two causes are confounded and this run separates neither: the verdict barrier now also waits for the second detector (ADR-051), so verdicts are taken later and the corroboration index holds different evidence when it is read; and the scenario is not bit-reproducible across runs because deploy timing is anchored to wall clock -- the same seed produced 50 shadow false pages here against v4's 53, and 30 artifact-driven against 35. The **ablation itself is unaffected**, because both of its passes are inside one run on byte-identical records, so the isolated contribution of cross-detector agreement stands. Separating the two would need a v4 re-run under the v6 barrier, which is a third pass nobody has run. | `docs/EVALUATION.md` section 3.12 |
| G-18 | **The v6 ablation is byte-identical in its records and not quite in its evidence.** Off-path scoring is wall-clock asynchronous, so a window can land either side of a decision point even with the same records and the same barrier. At 24 channels the two passes agreed on the second detector's opinion for **144 of 145** decisions and differed on one (`abstained` 22 against 21); at 12 channels they were identical on all 76. One decision does not carry the 25.7-point difference the ablation measures, but an ablation described as identical should be identical, and this one is identical in records rather than in evidence. Making it exact would mean scoring the second detector synchronously, which ADR-017 forbids on the hot path and ADR-051 rejects for this reason. | `docs/EVALUATION.md` section 3.13 |
| G-8 | The evaluation's deploy density is deliberately high (14 overlapping deploys over 900 s). It is a hard case, not a representative one. | `docs/EVALUATION.md` section 3.4 |
| G-9 | The scale sweep's dip at 4 consumers is unexplained. Uneven partition assignment (2,2,1,1 over 6 partitions, with the drain ending when the slowest consumer finishes) predicts exactly that shape, but the harness records only the total, not the per-consumer spread, so it is a hypothesis. | `docs/SCALE.md` section 3 |
| G-10 | NFR-5 asks for near-linear scaling and the measurement does not show it: 6 consumers buy 1.80x and efficiency falls to 30%. The plateau is at 3 consumers, half the partition count, so partitions are not what binds it. The likely cause -- broker and consumers sharing ten cores -- is named but not isolated. | `docs/SCALE.md` sections 2 and 3 |

## Resolved

| # | Item | Resolution |
|---|---|---|
| R-1 | Docker daemon not running at session start | Lives under `%LOCALAPPDATA%\Programs\DockerDesktop`; started it |
| R-2 | Whether Flink could run at all on this machine | It runs. Job submitted, 1,056 windows scored, output bit-identical to the Python detector, 30 checkpoints completed at an average of 283 ms |
| R-3 | B-1, how to serve the VLM | Answered 2026-09-06: hosted endpoint behind env vars |
| R-4 | B-2, whether to attempt QLoRA | Answered 2026-09-06: a GPU will be rented, so it proceeds as planned |
| R-5 | B-3, the GPU run the fine-tune needed | Ran 2026-09-13 on Northeastern Explorer (V100-SXM2-32GB), not rented hardware. The adapter beat both baselines -- 97.7% exact-match against 20.0% and 0.0%, forbidden actions 1.0% against 61.3%. Setup constraint recorded: Python 3.12 + torch 2.5.1+cu121 for bitsandbytes/triton |
