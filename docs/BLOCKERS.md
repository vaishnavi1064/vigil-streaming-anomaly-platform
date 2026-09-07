# Blockers, deferrals and assumptions

> Anything that needs a human decision, is stubbed, or was defaulted under BUILD.md's
> blocker protocol. Each entry states what was assumed, so the assumption can be overturned
> cheaply. "None" is a valid state for this file.

## Open -- needs a human

### B-3. The fine-tune: task reshaped and pipeline built. **Needs a GPU run on your cluster.**

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

**What to report back.** The output of step 4 is enough: it prints both baselines, the
adapter's score, the per-symptom breakdown and the exact-match delta. If the delta is
negative, that is the finding and it goes in `docs/EVALUATION.md` next to the rest.

### B-4. Answered: the episode record is fixed and v3 is being measured.

**Answered 2026-09-07 with option 2.** The defect is fixed (ADR-035): an episode now carries
`onset_ms`, the event time of the reading that drove its score, and conditioning compares
onsets rather than window boundaries.

**Why the earlier measurements were invalid.** An episode was timestamped with the start of
the window that flagged it, and windows slide by 10 s, so the only start differences two
episodes could express were 0, 10, 20 ... seconds. A 5 s synchrony tolerance selects exactly
one of them: identical bucket. v1 asked the same coincidence question at a 30 s bucket. The
synchrony hypothesis was **untested rather than refuted**, and ground truth for the same run
puts consecutive in-scope artifact onsets a median 1.8 s apart -- all of it below the grid.

**That the fix resolves anything is measured, not assumed.** Over a 420-second synthetic
scenario all 35 episodes now carry onsets strictly off the 10 s grid, spread from 0 to 27 s
within their window. A regression test states the defect as a difference in verdict: three
channels flagged in one window, two moving within a second and the third seventeen seconds
later, corroborate on window starts and do not on onsets.

**v3 is the same scenario, seed and density as v1, v2 and the low-density run**, with nothing
changed but the timestamp resolution, and whatever it says goes into
`docs/EVALUATION.md` section 3.6 beside the other three. v1 and v2 stay in the write-up.

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

## Decided by the architect

| # | Question | Decision | Consequence |
|---|---|---|---|
| B-1 | How should the VLM explainer be served, given 4 GB of VRAM and no API key? | **Hosted endpoint behind env vars** (`VLM_ENDPOINT`, `VLM_API_KEY`, `VLM_MODEL`). | Built against that interface on 2026-09-06 -- this row previously said so before it was true. With no key set it reports itself unavailable and detection is unaffected -- the documented degradation, not a stub pretending to work. Explanations appear the moment a key is supplied, and NFR-2's 5 s budget is measured against the real endpoint rather than assumed. Cost accepted: a per-flagged-window API cost and a third-party dependency on the rare path only. |
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
| C-2 | No API key of any kind in the environment. | Phase 4 VLM, Phase 5 judged eval | **Waiting on a key.** The explainer is now genuinely built (`src/vigil/explain/`, wired into the detector, 18 tests including the success path against a local fake endpoint) and reports itself unavailable with no key. Two things stay unmeasured until a key exists: NFR-2's 5 s budget against a real endpoint, and whether the explanations are any good, which needs a judge |
| C-3 | Docker VM has 8.1 GB of the machine's 15.6 GB. | Phases 2–3 | Managed: Flink is behind a compose profile so JobManager + TaskManager (~2.5 GB) are started deliberately rather than always. ClickHouse and MinIO will need the same treatment. |
| C-4 | Long-running measurements (the >= 4 h soak, the 200-series benchmark, the parallelism sweep) each need the machine to themselves. | Phases 2, 5 | Sequenced rather than parallel. The soak must run after chaos and scale, both of which deliberately break or saturate the stack. |

## Known gaps in what has been proven

Recorded here rather than only in the docs that would flatter themselves by omitting them.

| # | Gap | Where it is stated |
|---|---|---|
| G-1 | ~~The Flink exactly-once path has never been fault-tested.~~ **Closed 2026-09-06.** The TaskManager was SIGKILLed mid-checkpoint and held down 60 s: 58/58 samples unhealthy, job restored from checkpoint 5 across 7 restore cycles, recovered in 14.9 s, and a `read_committed` consumer saw **328 distinct window scores with 0 duplicates**. Still one kill, one job, one TaskManager. | `docs/CHAOS.md` section 2.1, `docs/CORRECTNESS.md` section 3a |
| G-2 | **Zero drift is measured over minutes, not hours.** NFR-6 asks for >= 4 hours; the longest clean run is 15 minutes (360,000 readings, drift 0, offset drift 0). | `docs/CORRECTNESS.md` sections 1 and 6 |
| G-3 | The chaos suite's producer absorbed every outage from its retry buffer, so the hold length at which loss becomes unavoidable has not been found. Finding that boundary would be a stronger result than not reaching it. | `docs/CHAOS.md` section 5 |
| G-4 | Single broker at replication factor 1: no leader election, no ISR shrink, no partial-availability case. Recovery times do not project to a cluster. | `docs/CHAOS.md` section 5 |
| G-5 | The TSB-AD benchmark scores at **window** resolution, which is a coarser task than point-level TSB-AD scoring. Numbers are not comparable to point-level leaderboards. | `benchmark.py` module docstring, `docs/EVALUATION.md` section 4 |
| G-6 | The multivariate fold is max-across-columns, so an anomaly that exists only in the *correlation* between features — where every column alone looks normal — cannot be detected. Such anomalies are in the corpus. | `benchmark.py` module docstring |
| G-11 | The benchmark scored **144 of 200 series**. The other 56 were excluded by the 20,000-point truncation because their labelled anomalies begin later, so the scored corpus is the early-onset half. An untruncated run is estimated at 4+ hours of CPU and has not been done. | `docs/EVALUATION.md` section 4.3 |
| G-12 | Only Chronos-Bolt-**tiny** was benchmarked, chosen to fit the laptop. A larger checkpoint may close the 141x-cost gap; the claim is about this model at this size, not about foundation models generally. | `docs/EVALUATION.md` section 4.3 |
| G-13 | **CI has never actually run.** The repository has no remote, so no GitHub Actions workflow has ever executed. Every step was run locally and passes -- lint, format, 524 tests, the secret scan, the repo-standards checks and the agent quality gate -- but "CI green" means "green when run by hand here", not "green on a runner". | `.github/workflows/ci.yml`, this table |
| G-7 | Conditioning is applied as episodes close, so an episode closing early sees fewer potential corroborating siblings than one closing late. A batch pass over completed windows would remove the asymmetry, at the cost of latency. | `docs/EVALUATION.md` section 3.4 |
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
