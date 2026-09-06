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
*Living log. Supersede, don't rewrite.*
