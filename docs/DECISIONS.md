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

---
*Living log. Supersede, don't rewrite.*
