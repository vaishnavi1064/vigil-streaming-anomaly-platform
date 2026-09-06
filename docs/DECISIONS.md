# Architecture Decision Records (ADRs)
### Real-Time Anomaly Detection & Self-Remediating Agent Platform
**SDLC phase:** Design — companion to `ARCHITECTURE.md`

> One decision per record: context, decision, alternatives, consequences (including the cost
> accepted). Add a new ADR when a real tradeoff is made; never overwrite history — supersede.

---

## ADR-001 — Zero-shot time-series foundation model as the detection spine
**Status:** accepted
**Context:** The stream is unlabeled and live; a detector must work without training labels.
**Decision:** Use a zero-shot foundation-model detector (Chronos-/TimesFM-class) on windowed batches.
**Alternatives:** train-from-scratch deep model (needs labels; heavy); supervised detector (no labels available); classical only (chosen instead as the *baseline*, not the spine).
**Consequences:** no labels needed; fits the serving stack. Cost: heavier per-window inference (mitigated by batching + a small/quantized model) and foundation-model AD is contested — hence the honest benchmark (ADR-008 / NFR-8).

## ADR-002 — Condition detection on reconciliation signals (the core mechanism)
**Status:** accepted
**Context:** Detectors fire on pipeline artifacts because they see only values, not pipeline health.
**Decision:** Feed the reconciliation harness's per-window pipeline-health signal into the detector as a conditioning input; attribute anomalies overlapping a proven disturbance to the pipeline.
**Alternatives:** keep DQ/reconciliation and detection separate (the status quo — the problem); post-hoc alert suppression rules (brittle, no provenance).
**Consequences:** the novel contribution; provable false-positive reduction. Cost: the reconciliation harness becomes **load-bearing for detection**, so it must be genuinely solid (this is the intended depth).

## ADR-003 — Generalize conditioning to deploy/change markers via a pluggable interface
**Status:** accepted
**Context:** Deploys are a top real-world false-positive trigger, distinct from pipeline faults.
**Decision:** Model conditioning as a `ContextSignalSource` interface; pipeline-health and deploy-markers are two instances. New signals implement the same interface.
**Alternatives:** hard-code only the pipeline signal (less general); build a bespoke path per signal (rework).
**Consequences:** one mechanism, extensible; the v2 roadmap (maintenance windows, feature flags) plugs in without redesign. Cost: a small amount of upfront interface design.

## ADR-004 — VLM explanation on flagged windows only
**Status:** accepted
**Context:** Operators need *why* to dismiss/triage; running a VLM on every window is too costly.
**Decision:** Run the VLM only on windows already flagged (the VLM4TS screen-then-verify pattern), producing an explanation attached to the episode.
**Alternatives:** VLM on every window (blows latency/cost); no explanation (less actionable).
**Consequences:** explainability with no hot-path cost. Cost: explanation latency on the rare path (acceptable, NFR-2). **Cite VLM4TS — this pattern is prior art, not our invention.**

## ADR-005 — Apache Flink for stateful exactly-once processing
**Status:** accepted
**Context:** Need event-time, large keyed state, and exactly-once — the data-platform signal.
**Decision:** Flink (event-time, watermarks, RocksDB state, checkpointing, 2PC exactly-once).
**Alternatives:** Kafka Streams (weaker for large state / event-time); Spark Structured Streaming (micro-batch, higher latency).
**Consequences:** strong exactly-once + windowing; the skill target for the primary role. Cost: operational complexity and a steeper learning curve.

## ADR-006 — Storage split: Postgres (episodes) · ClickHouse (serving) · Iceberg (source of truth)
**Status:** accepted
**Context:** Different needs: app state, fast dashboard queries, and a durable reconciliation reference.
**Decision:** Postgres for detected episodes/app state; ClickHouse for OLAP serving; Iceberg as the durable lake and reconciliation source of truth. Raw readings never go to Postgres.
**Alternatives:** one store for everything (wrong performance/durability tradeoffs).
**Consequences:** each store fits its job; reconciliation has a durable reference. Cost: more moving parts to run.

## ADR-007 — Conditioning fails open when the health signal is unavailable
**Status:** accepted
**Context:** If conditioning suppressed flags whenever context was missing, a signal outage could hide real anomalies.
**Decision:** When a context signal is unavailable, fall back to *unconditioned* detection — raise flags normally; never silently suppress.
**Alternatives:** fail-closed (suppress on missing context) — unsafe; block detection until context returns — unavailable when most needed.
**Consequences:** missing context can never hide a real anomaly. Cost: during a health-signal outage, artifact-driven false positives are not suppressed (correct tradeoff — safety over noise).

## ADR-008 — Solve context-conditioned detection deeply; defer alert-correlation & drift to v2
**Status:** accepted
**Context:** Five validated pains exist; solving all shallowly reads junior and invites scrutiny failures.
**Decision:** v1 solves context-conditioned false-positive suppression (pipeline + deploy) deeply and proves it; alert-correlation/flooding and drift/staleness are documented v2 roadmap.
**Alternatives:** build all five (breadth-over-depth); ignore the others (looks naive).
**Consequences:** one proven mechanism + a credible roadmap. Cost: v1 does not de-duplicate alerts or self-adapt to drift (accepted; the conditioning interface makes both reachable later).

---

## ADR-009 - JSON on the wire, no schema registry
**Status:** accepted (Phase 1)
**Context:** Every stage between ingestion and detection needs one agreed record format. The obvious production answer is Avro or Protobuf behind a schema registry.
**Decision:** Compact JSON, with the encode/decode pair defined once in `vigil.readings` and covered by round-trip tests.
**Alternatives:** Avro + Confluent Schema Registry (another always-on service on a 15.6 GB laptop, plus a codegen step, for a five-field record); Protobuf (same codegen cost, far less debuggable on the console consumer); MessagePack (compact, but no better schema story than JSON).
**Consequences:** `kafka-console-consumer` output is readable, which matters constantly while debugging the correctness story, and there is no extra service to keep alive. Cost: no enforced schema, larger payloads. Mitigated by the single codec module -- a field can only change in one place -- and measured: at the 20k events/s target the format costs roughly 2 MB/s before lz4, which is nowhere near the bottleneck (the producer sustained 76,556 events/s).

## ADR-010 - The public TDengine solar-fleet MQTT feed as the live source
**Status:** accepted (Phase 1); supersedes the "live feed TBD" note in PROJECT_PLAN section 9
**Context:** The plan called for a free, public, always-on feed and left the choice open; development had been proceeding on the synthetic generator.
**Decision:** `mqtt.tdengine.com:1883`, anonymous, QoS 0. Topics `sites`, `inverters`, `strings`, `weather`, `grid`. Default working set is `inverters`; `strings` is the high-rate topic for scale tests; `#` is exploration only. Configured via `MQTT_HOST` / `MQTT_PORT` / `MQTT_TOPIC`.
**Alternatives:** a crypto-exchange trade WebSocket (high rate, but a price series has no notion of "expected", so there is no physically meaningful residual and no operational context to condition on -- it would have gutted the core contribution); public transit GTFS-RT (low rate, polling rather than streaming); staying synthetic-only (no claim that the pipeline works on genuinely unseen data).
**Consequences:** This feed has the one property the project needs and a price stream does not: it publishes *expected* alongside *actual* (`Expected_Power_MW` vs `AC_Power_MW`, `Deviation_%` per string), so detection runs on a residual with physical meaning rather than on an arbitrary threshold. It also carries real operational context -- curtailment, soiling, alarms, weather -- which is the material the conditioning layer consumes in Phase 3. Measured 2026-09-06: strings ~504 msg/s, inverters ~30 msg/s, sites/weather/grid ~3.6 msg/s each. Cost: no SLA, no history, and a rate far below the throughput target, so the synthetic generator is retained as the scale and correctness harness (ADR-014). Citizenship: one connection per process, exponential reconnect backoff to a 60 s ceiling, identifying client id, QoS 0 as published.

## ADR-011 - The edge guarantee is at-most-once, with gap detection but no backfill
**Status:** accepted (Phase 1); corrects FR-1 and PROJECT_PLAN section 5.5
**Context:** The design assumed a WebSocket edge with gap detection *and REST backfill*, giving at-least-once at the boundary. The chosen feed is MQTT QoS 0 and publishes no history API.
**Decision:** State the edge guarantee as **at-most-once** and build detection without backfill. `IngestGapWatch` learns each channel's own cadence from the median of recent inter-arrival times and reports silence beyond a multiple of it. The interior guarantee (exactly-once inside Kafka/Flink) is unaffected and is where the correctness claim lives.
**Alternatives:** QoS 1 (the public broker's session behaviour is not ours to depend on, and it still recovers nothing dropped before we connected); claiming at-least-once anyway (false); dropping the live feed to preserve the original claim (loses the unseen-data property for a guarantee that only ever covered the edge).
**Consequences:** An honest, narrower claim. Cadence must be learned per channel rather than fixed globally because the same feed carries 500 msg/s string telemetry and 3.6 msg/s site rollups -- one threshold would either miss real outages on the fast channels or cry wolf on the slow ones. Median rather than mean, so one scheduling hiccup does not move the estimate; a detected gap is excluded from the estimate, so one long stall cannot raise the threshold enough to hide every later stall. Cost: edge loss is detectable but not repairable on the live source. Mitigation: the synthetic source has perfect sequence integrity by construction, so every correctness and chaos test runs against a stream where loss is unambiguous.

## ADR-012 - Fan messages out per (entity, metric); detect on the expected-vs-actual residual
**Status:** accepted (Phase 1)
**Context:** One MQTT message describes one entity at one instant with a dozen fields. The detector consumes univariate channels. Something must decide the mapping, and which field is *the* signal.
**Decision:** One reading per (entity, metric), channel named like `SITE_001.ac_power_mw`, identical in shape to the synthetic fleet so nothing downstream knows which source it is reading. The primary detection metric is the expected-vs-actual residual, not raw output: `Expected_Power_MW - AC_Power_MW` for sites, the feed's own `Deviation_%` for strings, `PR_Local` for inverters (which publish no expected-power field), `Setpoint_MW - Active_Power_MW` for grid meters.
**Alternatives:** publish each message as one multivariate record (forces every detector to know the feed's field names; TSB-AD-M already covers the multivariate benchmark case); detect on raw `AC_Power_MW` (fires on every sunset, fleet-wide, forever).
**Consequences:** The **sunset quirk** is handled by the choice of signal rather than by a special case: generation collapses every evening on every channel at once, but the residual sits near zero at noon *and* near zero at midnight, so nightfall is not an excursion. Raw power is still published as its own channel, deliberately -- it is the control that shows what an unconditioned detector does at sunset, and that comparison is the measurement in `docs/EVALUATION.md`. Any ratio this bridge *derives* is withheld below a production floor rather than emitted as a number invented by dividing near-zero by near-zero; fields the feed itself provides are always passed through, because judging them meaningless is the detector's job, informed by the irradiance channel, not the bridge's. Cost: roughly 8x message amplification (a measured 45 s run turned 1,387 MQTT messages into 11,089 readings across 336 channels).

## ADR-013 - TSB-AD-M as the labelled benchmark; threshold-independent metrics, never point-adjusted F1
**Status:** accepted (Phase 1, consumed in Phase 5)
**Context:** The live feed is unlabelled by construction, so precision and recall cannot be computed on it. A labelled corpus is required for the honest benchmark in NFR-8 and PROJECT_PLAN section 10.
**Decision:** TSB-AD-M, the multivariate track: 200 labelled series, 2.4 GB extracted, fetched on demand by `scripts/fetch_tsb_ad.py` into a git-ignored `Datasets/` (archive sha256 `7de86ac27f30eeb48d833bb061055670e3f3de07defd995cf2bd5db10ccc9a0d`). Report point-wise AUC-PR, VUS-PR and VUS-ROC, plus precision/recall/F1 at a *matched alarm budget*. **Point-adjusted F1 is not reported at all.**
**Alternatives:** MetroPT-3 alone (a single asset with one failure mode -- too thin to generalise from); deriving labels from the solar feed's own `Status` and `Alarm_Messages` (those are another detector's opinion, so scoring against them measures agreement with an unknown algorithm, not correctness); reporting point-adjusted F1 because most papers do.
**Consequences:** Point-adjustment marks an entire ground-truth range as detected if the detector fires even once inside it, which lets a near-random scorer post a high F1. That specific inflation is the reliability problem TSB-AD was built to expose, so adopting the benchmark and then using the metric it exists to discredit would be self-defeating. Accuracy and ROC-AUC are excluded for the same family of reasons: anomalies are well under 1% of points, so both are dominated by the negative class. Cost: our numbers will look *worse* than papers that point-adjust, and are not directly comparable to them. That is the intended trade. Environment note: TSB-AD's own package targets Python 3.11; we consume only the labelled CSVs and compute the metrics ourselves, so the platform stays on 3.12 and no second interpreter is needed.

## ADR-014 - Keep the synthetic generator as the harness, with AR(1) noise
**Status:** accepted (Phase 1)
**Context:** With a live feed adopted, the synthetic generator could have been deleted.
**Decision:** Keep it behind the same `ReadingSource` interface. It is the only source that can be seeded, replayed exactly, driven at an exact rate or unpaced, and that carries ground-truth labels inline. Its noise is AR(1) with the innovation scaled by sqrt(1 - phi^2), not white.
**Alternatives:** delete it and test against the live feed (throughput capped near 250 readings/s, no reproducibility, no labels, and a chaos suite whose results depend on a third party's uptime); white noise (simpler to write).
**Consequences:** Throughput, chaos and exactly-once tests are reproducible and independent of anyone else's availability. The AR(1) choice is deliberate methodology: independent samples keep a rolling standard deviation well behaved, so almost any excursion is caught and the z-score baseline looks far stronger than it is. Real sensor noise is autocorrelated, which inflates the running sigma and is what actually makes cheap detectors miss things. Scaling the innovation holds the stationary variance fixed, so changing the persistence cannot silently change the signal-to-noise ratio the benchmark runs at. Cost: a harder synthetic signal makes our own baseline numbers look worse, which is the point.

## ADR-015 - Schedule the evaluation adversarially, so blanket suppression fails visibly
**Status:** accepted (Phase 1, consumed in Phase 3)
**Context:** The core claim is a measured false-positive reduction during deploys and pipeline disturbances. There is a trivial way to post a large number: suppress everything inside a context window. A naive evaluation cannot distinguish that from correctly attributing an artifact to its cause, which would make the headline result unfalsifiable and worthless under scrutiny.
**Decision:** Build the generator so the cheat fails. `loadgen.py --scenario` schedules four populations deliberately: (1) deploys that **do** perturb telemetry, whose artifacts are the legitimate suppression target; (2) deploys that perturb **nothing** (`--quiet-deploy-fraction`, default 0.35), so suppression triggered by the marker alone has nothing to hide behind; (3) real faults **outside** every deploy window, as the control; (4) real faults **inside** deploy windows including quiet ones (`--fault-in-window-fraction`, default 0.40), which must still be detected. Deploys touch a **subset** of channels, and an artifact is only ever placed on a channel the deploy actually touched, so a policy that ignores scope over-suppresses and is caught. Markers go to a dedicated context topic (`ops.context`) and are published as the run reaches them, never dumped up front -- a policy that could see the whole future would be solving an easier problem than the real one. The same design is applied to pipeline artifacts through the chaos suite in Phase 2.
**Alternatives:** inject artifacts only, and report the suppression rate (the unfalsifiable version); inject real faults only outside windows (leaves recall-under-a-window untested, which is exactly where blanket muting hides); label everything one way and rely on manual inspection (does not scale and is not reproducible).
**Consequences:** Every reported false-positive reduction comes with a recall number computed on faults that were deliberately placed where a cheating policy would lose them, so the pair of numbers is meaningful rather than decorative. The plan is seeded and written to disk (`--write-plan`) before scoring, so ground truth is fixed before any detector sees the data. Cost: a substantially more complex generator, and a scenario run needs a fixed `--duration` because the plan is scheduled up front. Poisson anomalies are disabled under a scenario, since unplanned excursions would appear as detections the ground truth cannot account for.

## ADR-016 - Report false-positive reduction and recall as a pair, never a single number
**Status:** accepted (Phase 1); revises NFR-8
**Context:** NFR-8 was written as "at least 50% of artifact-driven false positives suppressed". Optimising a suppression rate on its own rewards exactly the failure mode ADR-015 was built to expose: the metric is maximised by suppressing everything.
**Decision:** The success criterion is a **pair**, always reported together and always against the *unconditioned* baseline on the same data: **at least 40% false-positive reduction during simulated deploys and pipeline faults, with approximately zero recall loss on real faults**. Report the precision/recall trade-off, not one figure. Recall is broken out for faults inside context windows and outside them, because the inside population is the one a cheating policy loses. Scoring uses threshold-independent, time-aware measures (VUS-style) consistent with the TSB-AD choice in ADR-013; **no point-adjustment**. A run of consecutive alarms on the same channel counts **once**, since an operator is paged once per incident and counting each window separately would let a chatty detector inflate both the false-positive count and the apparent reduction. A read-only **shadow pass** runs first to establish the unconditioned baseline on the identical data, so the comparison is a true A/B rather than two runs over different streams.
**Alternatives:** keep the single 50% target (rewards over-suppression); report F1 alone (hides which side of the trade moved); point-adjusted metrics (already rejected in ADR-013 for the same family of reasons).
**Consequences:** The headline claim becomes defensible: a reviewer can ask "what did it cost you in recall" and the answer is already in the table. The target moves from 50% to 40% because it is now a genuinely harder, paired target rather than an unconstrained suppression rate -- lowering it while adding the constraint is the honest direction. Cost: every evaluation run must be executed twice, once shadow and once conditioned. All deltas are reported including regressions.

## ADR-017 - The latency budget binds the hot path, not the foundation model
**Status:** accepted (Phase 1); revises NFR-1
**Context:** NFR-1 set 250 ms p99 per window. Pinning that to a time-series foundation model on this laptop is not achievable honestly: a 200M-class model runs roughly 200-500 ms per batch on CPU and effectively wants a GPU, and this machine has 4 GB of VRAM. Writing a target that can only be met by fudging the measurement is worse than writing an accurate one.
**Decision:** The 250 ms p99 budget applies to the **hot path** -- the detector on the critical path that every window must clear before an episode can be raised. The cheap z-score baseline stays there, optionally joined by a distilled or tiny detector (Chronos-Bolt or Tiny Time-Mixers class) if it measures within budget. The foundation model runs **batched and off the critical path**, contributing a second opinion and the benchmark comparison rather than gating the stream. Actual per-detector latency is measured and recorded, and the model is chosen to fit the budget rather than the budget stretched to fit the model. Final latency and throughput targets are locked only after Phase 2 produces the baseline and the scaling curve.
**Alternatives:** keep one budget covering every detector (either unachievable, or achievable only by quietly excluding the model from the measurement); drop the foundation model (abandons a stated goal and the honest benchmark that goes with it); require a GPU (violates the laptop constraint in NFR-13).
**Consequences:** The stream keeps a hard, measured latency bound that does not depend on model choice, and the foundation model can be evaluated on quality without its cost being hidden. Degradation is explicit and already in the design: if the model path is down or slow, the hot path is unaffected. Cost: the model's verdict arrives later than the baseline's, so an episode can be raised on the baseline and enriched afterwards -- the episode record must therefore carry per-detector scores rather than a single fused one. Numbers stated as targets here are provisional until Phase 2 measures them.

## ADR-018 - Build the reconciliation harness before Flink
**Status:** accepted (Phase 2)
**Context:** Phase 2 contains both the Flink migration and the reconciliation harness. The obvious order is Flink first, since the harness is described as auditing it.
**Decision:** Build the harness first, against the existing Python consumer.
**Alternatives:** Flink first, then a harness to audit it (the natural reading of the plan); build them together (neither would be trustworthy while the other was in flux).
**Consequences:** Two payoffs. First, the harness is **load-bearing for the core contribution**, not merely for the correctness claim -- the per-window health signal it emits is what conditioning consumes in Phase 3 -- so building it first unblocks the thing the project exists to demonstrate. Second, it turns the Flink migration into a *checkable* change: the harness records what the semantics were before, and the same harness run against the Flink job afterwards says whether they survived. Migrating first and auditing afterwards would have left no baseline to compare against. Cost: the harness had to be written against an interface that was about to change, which is why the ledger is pure and knows nothing about Kafka.

## ADR-019 - Watermarks take the minimum across sources, with an idleness timeout
**Status:** accepted (Phase 2)
**Context:** The harness closes a window once the stream has moved past it. The first implementation used the highest event time seen anywhere in the stream.
**Decision:** The watermark is the **minimum** event time across all non-idle sources, where a source is a Kafka partition. Partitions are registered on assignment rather than on first delivery, and a registered-but-silent source blocks the watermark entirely. A source that has said nothing for a configurable interval stops holding it back.
**Alternatives:** maximum across the stream (what was tried); a fixed grace period generous enough to absorb the skew (fragile -- the right value depends on consumption order, which is not knowable in advance); per-partition windows that are never combined (loses the fleet-wide view the health signal exists to give).
**Consequences:** Measured, on a 7-minute replay of 252,000 readings: the maximum-based version stranded **74%** of readings as arriving after their window had closed, and emitted **187,631** health windows where 15 were correct, because each late reading re-opened and re-emitted a window that had already been published. Switching to the minimum fixed the direction but still stranded **51%**, because sources were discovered lazily -- a partition Kafka had not yet served was indistinguishable from one that did not exist, so the minimum was taken over a subset. Registering on assignment took it to **0%**. The idleness timeout is required because Kafka partitions go quiet routinely and one silent partition would otherwise stall every window forever; this is the same problem Flink solves the same way, and the Flink job uses `withIdleness` for exactly this reason. Cost: a window cannot close until the slowest partition reaches it, so a badly lagging partition delays the health signal -- which is the correct trade, since publishing a health record for a window we have not finished reading would be a false statement.

## ADR-020 - Lag grading is separable from loss grading
**Status:** accepted (Phase 2)
**Context:** The health signal grades a window's severity from missing readings, duplicates, reordering, and lag. Lag is measured against the wall clock. Replaying a topic recorded minutes ago therefore reports minutes of lag and graded every window of every replay `critical`.
**Decision:** Keep measuring lag always, but make **grading** on it switchable (`--ignore-lag`). Replays and benchmarks turn it off and say so; live runs leave it on. Loss and duplicate grading are never affected.
**Alternatives:** raise the lag thresholds for replays (arbitrary, and hides genuine lag in live runs that happen to use the same config); drop lag from the signal (it is the most useful early warning of a pipeline falling behind); measure lag against ingestion time rather than event time (measures nothing -- ingestion time is when we read it, so the answer is always near zero).
**Consequences:** A replay no longer marks everything disturbed, which matters because a conditioning policy fed that signal would suppress every anomaly in the run. The measurement stays in the record either way, so nothing is hidden -- only the severity judgement changes, and the run states which mode it used. Cost: one more flag whose meaning has to be understood to read a result correctly.

## ADR-021 - A chaos scenario must prove it disrupted something
**Status:** accepted (Phase 2)
**Context:** The first full chaos run reported four passes. Inspecting the logs showed that two of them -- the broker pause and the network partition -- produced no observable disruption at all: the producer never logged an error, and the recovery check reported the broker serving again in 0.0 s. The runs were green and meaningless.
**Decision:** Every scenario samples serviceability once a second throughout the fault hold and records how many samples found the system unserviceable. **A scenario with zero unhealthy samples fails**, with a note saying the fault did not disrupt anything. The network-partition check was also fixed: it had been testing only the client path, and Docker's published-port proxy keeps answering the TCP handshake after a container leaves the network, so the check reported healthy throughout a partition that had genuinely been applied.
**Alternatives:** trust that injecting implies disrupting (what produced the false greens); assert on producer error counts alone (a client can ride out a short outage entirely from its retry buffer, which is correct behaviour and would look like a no-op); lengthen every hold until something visibly breaks (slow, and tunes the test until it passes rather than measuring the system).
**Consequences:** A green chaos result now means something specific: the system was measurably unserviceable, and it still recovered to a state the identity invariant accepts. Cost: some faults are genuinely hard to make bite on a single-node laptop broker, and those will now report failure rather than a comfortable pass. That is the intended direction -- `docs/CHAOS.md` reports what actually happened, including the modes that could not be made to disrupt this deployment.

## ADR-022 - Run Flink as containers with PyFlink, not as a host process or a Java job
**Status:** accepted (Phase 2)
**Context:** ADR-005 chose Flink for event-time processing, large keyed state and exactly-once via two-phase commit. It has to actually run on this machine.
**Decision:** A JobManager and a TaskManager as compose services behind a `flink` profile, built from `flink:1.20.1-scala_2.12-java17` with Python and PyFlink added. The job is `flink/scoring_job.py`, submitted to the cluster.
**Alternatives:** PyFlink in the host virtualenv (no wheel is published for Windows on Python 3.12, so it would mean a source build against a toolchain this machine does not have); a Java job (Java 17 is present, but it would mean a Maven build and a second implementation of the detector, so the migration could no longer be checked by comparing identical computations); Flink SQL only (does not exercise keyed state or a custom process function, which are the parts worth demonstrating).
**Consequences:** This is the deployment `ARCHITECTURE.md` section 9 already described, so it is the intended shape rather than a workaround. The job deliberately mirrors `vigil.detectors.zscore` computation for computation -- same Welford update, same decay, same score -- because the point of the migration is to show the semantics survived it, and that can only be shown if the two are the same calculation. Behind a profile because JobManager plus TaskManager want about 2.5 GB and the Docker VM has 8.1 GB with Kafka and Postgres already in it, so it is brought up deliberately rather than always running. Cost: an image build, a second Python runtime to keep in step with the first, and a job that cannot import the `vigil` package directly.

---
*Living log. Supersede, don't rewrite.*
