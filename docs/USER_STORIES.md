# User Stories & Product Backlog
### Real-Time Anomaly Detection & Self-Remediating Agent Platform
**SDLC phase:** Requirements — **v1.0** — companion to `docs/PROBLEM_STATEMENT.md`

> Format: *As a [persona], I want [capability], so that [value]* + testable acceptance
> criteria (AC). Priority uses MoSCoW (Must / Should / Could / Won't-this-release).
> Reflects the agreed scope: solve context-conditioned detection deeply; keep VLM/agent
> thin; defer alert-correlation and drift-adaptation to v2.

## Personas
- **Operator** — on-call / SRE / ops engineer who acts on alerts. Wants low noise, actionable, explained signals.
- **Platform engineer** — owns the pipeline and its correctness (this is also the builder). Wants correctness proven, scale known, failure handled.

---

## Epic A — Ingestion & streaming backbone
**A1 — Durable ingestion.** *As a platform engineer, I want the stream ingested into Kafka with gap-detection and REST backfill, so no events are silently dropped at the edge.* — **Must**
- AC: gaps at the WebSocket edge are detected and backfilled; edge guarantee is at-least-once; behavior documented.

**A2 — Exactly-once processing.** *As a platform engineer, I want Flink to process the stream with event-time, watermarks, and exactly-once (2PC), so interior processing neither loses nor duplicates events.* — **Must**
- AC: exactly-once verified across a forced restart (no loss, no dupes); late events handled via watermarks; checkpointing enabled.

## Epic B — Correctness & reconciliation (load-bearing)
**B1 — Reconciliation harness.** *As a platform engineer, I want continuous proof that ingested = processed with zero drift via per-stage identity invariants, so correctness is demonstrated, not assumed.* — **Must**
- AC: drift = 0 across a multi-hour run; per-stage invariants (not just row counts) checked; failure surfaces immediately.

**B2 — Pipeline-health signal.** *As a platform engineer, I want reconciliation to emit a structured, queryable pipeline-health signal (drift, lag, gap-fills, dupes) per window, so downstream components can consume it.* — **Must**
- AC: signal available per time window via API/topic; schema documented. *(This is the wire that enables the core.)*

## Epic C — Detection
**C1 — Baseline detector.** *As a platform engineer, I want a rolling z-score baseline over sliding windows, so there is a cheap, permanent benchmark to measure against.* — **Must**
- AC: scores windows; unit-tested on a known series; retained as the benchmark baseline.

**C2 — Foundation-model detector.** *As a platform engineer, I want a zero-shot foundation-model detector scoring windows in real time within the latency budget, so detection needs no labels.* — **Must**
- AC: scores windowed batches (not raw ticks); per-window latency ≤ budget; runs on a laptop (small/quantized model).

## Epic D — Context-conditioned detection (the core contribution)
**D1 — Reconciliation-gated detection.** *As an operator, I want anomalies that coincide with a proven pipeline disturbance suppressed or re-labeled, so I am not paged for pipeline artifacts.* — **Must**
- AC: when B2 shows a disturbance in window T, flags in T are attributed to the pipeline (suppressed/annotated), not raised as real; **measured false-positive reduction vs. the unconditioned baseline** under injected faults.

**D2 — Deploy-marker conditioning.** *As an operator, I want the detector to condition on deploy/change markers, so a deployment does not trigger an alert storm.* — **Must**
- AC: with a deploy marker active, post-deploy flags are attributed to the deploy; measured FP reduction during simulated deploys.

**D3 — Pluggable conditioning interface.** *As a platform engineer, I want conditioning to be a pluggable interface accepting multiple context signals, so v2 signals can be added without rework.* — **Should**
- AC: adding a signal = implementing one interface; extension point documented (this is the v2 roadmap hook).

## Epic E — Explanation (kept thin)
**E1 — Explained anomaly.** *As an operator, I want a plain-language explanation of a flagged real anomaly, so I can triage fast.* — **Should**
- AC: VLM runs only on flagged windows; explanation attached to the episode; fires on a small fraction of windows (rare-path, off the hot path).

## Epic F — Remediation agent (kept thin)
**F1 — Safety-gated remediation.** *As an operator, I want a suggested remediation grounded in runbooks and safety-checked, so an anomaly moves toward action.* — **Should**
- AC: agent retrieves runbook context, proposes an action, a deterministic non-LLM gate approves/rejects, execution only in a sandbox; full decision trace logged.

## Epic G — Observability & dashboard
**G1 — Live dashboard.** *As an operator, I want a live view of health, the reconciliation/drift panel, detected episodes + explanations, and agent actions, so I can read system state at a glance.* — **Must** (reconciliation panel) / **Should** (rest)
- AC: panels render live; the reconciliation panel visibly shows the zero-drift proof.

## Epic H — Scale & resilience
**H1 — Throughput harness.** *As a platform engineer, I want a load generator and throughput measurement, so I can quantify sustained throughput and a parallelism curve.* — **Must**
- AC: sustained events/sec reported with the hardware; throughput-vs-parallelism curve produced; backpressure behavior shown.

**H2 — Chaos suite.** *As a platform engineer, I want fault injection (broker/processor kills, partitions), so recovery is proven.* — **Must**
- AC: ≥3 fault modes injected; system recovers to a consistent state with bounded lag; evidence documented.

## Epic I — Evaluation & CI
**I1 — Honest detection benchmark.** *As a platform engineer, I want detection benchmarked against the baseline on a labeled set (including where it loses), so claims are honest.* — **Must**
- AC: precision/recall and false-alarm rate at matched alarm budgets; results including losses documented in `docs/EVALUATION.md`.

**I2 — CI quality gate.** *As a platform engineer, I want agent output quality gated in CI, so a quality regression blocks the build.* — **Should**
- AC: DeepEval gate runs in CI; a quality drop fails the build.

---

## v2 backlog (Won't-this-release — documented roadmap)
- **Alert correlation & flooding** → aggregate anomalies into incidents.
- **Drift / staleness adaptation** → detector adapts to concept drift.
- **Additional context signals** via the D3 interface (maintenance windows, feature flags).

## Prioritization → sprint mapping
| Sprint | Focus | Stories |
|---|---|---|
| 1 | Thin spine (runs end-to-end) | A1, A2, C1, C2, G1 (minimal) |
| 2 | Correctness + resilience | B1, B2, H1, H2 |
| 3 | **The core contribution** + serving | D1, D2, D3 |
| 4 | Explanation + agent (thin) | E1, F1 |
| 5 | Evaluation + CI | I1, I2 |
| 6 | Production wrapper + polish | hardening, README, demo |

## Definition of Done (every story)
Runs end-to-end from documented commands · tests pass · config/secrets externalised ·
a `docs/DECISIONS.md` (ADR) entry for each real choice · reviewed and understood by the builder.

---
*v1.0 — Requirements artifact. Backlog is living; re-prioritize per sprint.*
