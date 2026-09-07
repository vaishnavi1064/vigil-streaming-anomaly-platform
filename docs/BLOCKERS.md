# Blockers, deferrals and assumptions

> Anything that needs a human decision, is stubbed, or was defaulted under BUILD.md's
> blocker protocol. Each entry states what was assumed, so the assumption can be overturned
> cheaply. "None" is a valid state for this file.

## Open -- needs a human

### B-3. The tool-calling fine-tune would be distilling a five-way lookup. Is it still worth the rented GPU?

**What was measured.** The training set is built and curated (1,200 examples, all four
populations, every target gate-approved and licence-checked). Counting distinct target
sequences over those 1,200 examples gives **five**:

| Target | Rows | Produced when |
|---|---|---|
| describe, fetch_recent, silence_channel, raise_ticket | 351 | variance burst |
| describe, fetch_recent, request_recalibration, raise_ticket | 307 | level shift |
| describe, fetch_recent, annotate_episode | 309 | isolated spike |
| describe_channel, escalate_to_human | 118 | safety channel |
| escalate_to_human | 115 | abstention, and safety where escalate is the only licence |

The target is a deterministic function of the diagnosed symptom, and the symptom is computed
by `Diagnoser` **before** the prompt is rendered -- it is in the prompt. So a model trained
on this set is learning a five-way classification whose answer is already an input. It can
approach the deterministic planner and cannot beat it. B-2 was answered before this was
measured.

**Options.**

1. **Drop the fine-tune; publish the measurement as the finding.** Keep the dataset builder,
   the schema and the curation as evidence the work was done properly, and spend the time on
   the agent eval (Ragas/DeepEval/TruLens) and the CI quality gate that Phase 5 also asks
   for. Costs the "QLoRA" line; gains a defensible answer to "why didn't you fine-tune".
2. **Make the task genuinely harder, then fine-tune.** Remove the diagnosis summary and
   symptom from the prompt so the model must infer the symptom from the score evidence and
   select actions from the retrieved licences. Still distillation, but the answer is no
   longer handed to the model in its own prompt. Costs a rewrite of the prompt renderer and
   a re-measure; the deterministic planner remains the baseline.
3. **Fine-tune as planned and report it as distillation.** Cheapest in effort, and the
   honest write-up would have to say the model cannot exceed the rules on this task.

**Recommendation: 1, or 2 if the fine-tune matters for the portfolio.** Not chosen
autonomously because it trades an interview talking point against effort, which is the
architect's call, and because it spends money on hardware.

**Not blocking anything.** The deterministic planner is in place and the agent loop is
complete; work continues elsewhere.

### B-4. Both conditioning measurements asked a coincidence question, not a synchrony question. Fix the episode record and measure a third time, or stop and publish the negative result?

**What was measured.** v2 ran on 2026-09-06 (run `753ddb71`): false-positive reduction
**+9.0%** against a >= 40% target, recall loss **10.0%** against a <= 5% tolerance. NFR-8
not met, for the second time and in the opposite direction from v1 -- v1 over-suppressed
(39 attributions, -36.7% recall), v2 barely suppresses (6 attributions, +9.0% FP reduction).

**Why it failed.** An episode's `t_start_ms` is the start of the *window* that first flagged
it, and windows slide by 10 s. The only start gaps two episodes can have are 0, 10, 20 ...
seconds, so v2's 5 s synchrony tolerance selected exactly one of them: zero. v2 measured
"first flagged in the same window bucket", not "moved within 5 seconds". v1 measured the
same coincidence at a 30 s bucket. **The synchrony hypothesis has not been tested.**

Ground truth for the same run says the signal is there to be found: in-scope artifact onsets
within one deploy have a median consecutive gap of **1.8 s**, and 70% of consecutive pairs
are within 5 s -- all of it below the 10 s grid the episode record rounds to.

**The fork.**

1. **Stop here and publish the negative result.** Two attempts, two honest failures, one
   diagnosis each; `docs/EVALUATION.md` already carries all of it. This is what
   `docs/PROGRESS.md` section 5 said to do if v2 missed, and it is a defensible place to
   stop. The core mechanism that *did* work -- the plausibility check, 72 of 78 episodes
   correctly raised rather than attributed -- stands on its own.
2. **Fix the episode record, then measure v3.** Give `Episode` an onset time taken from the
   sample that actually crossed the threshold rather than from the window boundary, then
   re-run unchanged in every other respect. This is a defect fix rather than a policy tweak:
   an episode that only knows which window noticed it is under-recording what it observed,
   and the same field would sharpen detection-latency reporting and the dashboard. Cost:
   a change to the episode schema and its store, plus one more 30-minute measurement.

**Recommendation: 2, then publish v1, v2 and v3 together with this diagnosis.** Not taken
autonomously because `docs/PROGRESS.md` section 5 explicitly said to stop after two attempts,
and because a third attempt after two failures needs to be visibly a defect fix rather than a
knob turn. If the answer is 1, nothing is lost: the diagnosis is already published.

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
