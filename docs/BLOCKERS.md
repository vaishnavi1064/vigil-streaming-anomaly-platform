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

## Decided by the architect

| # | Question | Decision | Consequence |
|---|---|---|---|
| B-1 | How should the VLM explainer be served, given 4 GB of VRAM and no API key? | **Hosted endpoint behind env vars** (`VLM_ENDPOINT`, `VLM_API_KEY`, `VLM_MODEL`). | The explainer is built in full against that interface. With no key set it reports itself unavailable and detection is unaffected -- the documented degradation, not a stub pretending to work. Explanations appear the moment a key is supplied, and NFR-2's 5 s budget is measured against the real endpoint rather than assumed. Cost accepted: a per-flagged-window API cost and a third-party dependency on the rare path only. |
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
| C-2 | No API key of any kind in the environment. | Phase 4 VLM | **Waiting on a key.** The explainer is built and its unavailable path is tested; it produces explanations as soon as `VLM_API_KEY` is set |
| C-3 | Docker VM has 8.1 GB of the machine's 15.6 GB. | Phases 2–3 | Managed: Flink is behind a compose profile so JobManager + TaskManager (~2.5 GB) are started deliberately rather than always. ClickHouse and MinIO will need the same treatment. |
| C-4 | Long-running measurements (the >= 4 h soak, the 200-series benchmark, the parallelism sweep) each need the machine to themselves. | Phases 2, 5 | Sequenced rather than parallel. The soak must run after chaos and scale, both of which deliberately break or saturate the stack. |

## Known gaps in what has been proven

Recorded here rather than only in the docs that would flatter themselves by omitting them.

| # | Gap | Where it is stated |
|---|---|---|
| G-1 | The Flink exactly-once path has **never been fault-tested**. The job runs and its output is bit-identical to the reference, but nothing has killed it mid-checkpoint. The 2PC claim currently rests on configuration and on Flink's own guarantees, not on evidence from this deployment. The scenario exists (`chaos.py --fault flink-taskmanager-kill`) and has not been run. | `docs/CORRECTNESS.md` section 6, `docs/CHAOS.md` section 5 |
| G-2 | **Zero drift is measured over minutes, not hours.** NFR-6 asks for >= 4 hours; the longest clean run is 15 minutes (360,000 readings, drift 0, offset drift 0). | `docs/CORRECTNESS.md` sections 1 and 6 |
| G-3 | The chaos suite's producer absorbed every outage from its retry buffer, so the hold length at which loss becomes unavoidable has not been found. Finding that boundary would be a stronger result than not reaching it. | `docs/CHAOS.md` section 5 |
| G-4 | Single broker at replication factor 1: no leader election, no ISR shrink, no partial-availability case. Recovery times do not project to a cluster. | `docs/CHAOS.md` section 5 |
| G-5 | The TSB-AD benchmark scores at **window** resolution, which is a coarser task than point-level TSB-AD scoring. Numbers are not comparable to point-level leaderboards. | `benchmark.py` module docstring, `docs/EVALUATION.md` section 4 |
| G-6 | The multivariate fold is max-across-columns, so an anomaly that exists only in the *correlation* between features — where every column alone looks normal — cannot be detected. Such anomalies are in the corpus. | `benchmark.py` module docstring |
| G-7 | Conditioning is applied as episodes close, so an episode closing early sees fewer potential corroborating siblings than one closing late. A batch pass over completed windows would remove the asymmetry, at the cost of latency. | `docs/EVALUATION.md` section 3.4 |
| G-8 | The evaluation's deploy density is deliberately high (14 overlapping deploys over 900 s). It is a hard case, not a representative one. | `docs/EVALUATION.md` section 3.4 |

## Resolved

| # | Item | Resolution |
|---|---|---|
| R-1 | Docker daemon not running at session start | Lives under `%LOCALAPPDATA%\Programs\DockerDesktop`; started it |
| R-2 | Whether Flink could run at all on this machine | It runs. Job submitted, 1,056 windows scored, output bit-identical to the Python detector, 30 checkpoints completed at an average of 283 ms |
| R-3 | B-1, how to serve the VLM | Answered 2026-09-06: hosted endpoint behind env vars |
| R-4 | B-2, whether to attempt QLoRA | Answered 2026-09-06: a GPU will be rented, so it proceeds as planned |
