# Blockers, deferrals and assumptions

> Anything that needs a human decision, is stubbed, or was defaulted under BUILD.md's
> blocker protocol. Each entry states what was assumed, so the assumption can be overturned
> cheaply. "None" is a valid state for this file.

## Open — needs a human

| # | Question | Why it needs a person | Options |
|---|---|---|---|
| B-1 | **How should the VLM explainer be served?** | Every option costs something the builder has to choose between, and none is obviously right. The reference machine has 4 GB of VRAM, which will not hold a useful vision-language model, and no API key is present in the environment. | (a) A hosted endpoint behind `VLM_ENDPOINT`/`VLM_API_KEY` — works, costs money per flagged window, and makes the demo depend on a third party. (b) A small quantized local VLM on CPU — free and self-contained, but explanation latency will breach NFR-2's 5 s budget and the quality will be poor enough to be worth reporting honestly. (c) Ship the explainer interface with no model behind it and say so. **Nothing is stubbed to look present in the meantime.** |
| B-2 | **Is the QLoRA fine-tune worth attempting on this hardware?** | Phase 5 calls for fine-tuning a 7–8B tool-calling model. 4 GB of VRAM makes that a multi-day proposition at best, and the agent currently uses a deterministic planner that needs no model at all. | (a) Skip it, document why, and keep the deterministic planner as the honest baseline. (b) Fine-tune something much smaller (1–2B) and report that it is not the model the plan specified. (c) Rent a GPU for a few hours. |

## Defaulted (proceeding under a recorded assumption)

| # | Item | Default taken | Recorded in | Reversible by |
|---|---|---|---|---|
| D-1 | The plan left the live feed "TBD" | Public TDengine solar-fleet MQTT feed, topic `inverters` | ADR-010 | Change `MQTT_*` in `.env` and add a `TopicMapping` |
| D-2 | The feed offers no history API, so FR-1's REST backfill cannot be built | Gap **detection** only; edge guarantee restated as at-most-once | ADR-011 | Only by switching to a feed with history |
| D-3 | Inverters publish no expected-power field | Use `PR_Local` as the normalised residual for that topic | ADR-012 | Swap the `primary=True` metric in the mapping |
| D-4 | PyFlink publishes no wheel for Windows on Python 3.12 | Run Flink as compose services behind a profile, job submitted to the cluster | ADR-022 | Nothing to reverse; this is also the deployment ARCHITECTURE.md describes |
| D-5 | A faithful VUS-PR is subtle enough that a half-right version is a real risk | Dropped it; report detection latency and event-level rate beside point recall instead | ADR-023 | Implement the decayed-buffer weighting properly and re-add |
| D-6 | The agent's planner needs a model that will not fit here | Deterministic rule-based planner behind a `Planner` protocol | ADR-026 | Implement the protocol with a model; the gate is unchanged either way |

## Known constraints on this machine

| # | Constraint | Affects | Status |
|---|---|---|---|
| C-1 | RTX 3050 Ti Laptop, 4 GB VRAM. A 7–8B model will not fit at fp16. | Phase 4 VLM, Phase 5 QLoRA | **Now blocking** — escalated to B-1 and B-2 |
| C-2 | No API key of any kind in the environment. | Phase 4 VLM | **Now blocking** — part of B-1 |
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
