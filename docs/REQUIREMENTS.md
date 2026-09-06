# Requirements Specification
### Real-Time Anomaly Detection & Self-Remediating Agent Platform
**SDLC phase:** Requirements — **v1.0** — companion to `PROBLEM_STATEMENT.md` and `USER_STORIES.md`

> Functional requirements are traced to their user story. Non-functional requirements carry
> **measurable targets** and a verification method. Numeric targets are **initial** — set
> here to make the system testable, and validated/tuned in Sprint 2 (Phase 2) against the
> reference laptop. Report measured values against these; don't retrofit the targets to the
> results.

---

## Functional requirements

| ID | Requirement | Story | Priority |
|---|---|---|---|
| FR-1 | Ingest the stream into Kafka with gap-detection and REST backfill at the edge | A1 | Must |
| FR-2 | Process in Flink with event-time, watermarks, and exactly-once via 2PC | A2 | Must |
| FR-3 | Reconciliation harness proves ingested = processed with zero drift via per-stage identity invariants | B1 | Must |
| FR-4 | Emit a structured, queryable pipeline-health signal (drift, lag, gap-fills, dupes) per window | B2 | Must |
| FR-5 | Rolling z-score baseline detector over sliding windows (permanent benchmark) | C1 | Must |
| FR-6 | Zero-shot foundation-model detector scoring windowed batches in real time | C2 | Must |
| FR-7 | **Context-conditioned detection:** suppress/re-label anomalies coinciding with a proven pipeline disturbance | D1 | Must |
| FR-8 | Condition detection on deploy/change markers | D2 | Must |
| FR-9 | Conditioning exposed as a pluggable interface for additional context signals | D3 | Should |
| FR-10 | VLM explanation generated only for flagged windows | E1 | Should |
| FR-11 | Runbook-grounded agent proposes remediation; deterministic gate approves/rejects; sandbox-only execution; full trace | F1 | Should |
| FR-12 | Persist detected episodes (score, window bounds, attribution, explanation) and expose for query | E1/G1 | Must |
| FR-13 | Live dashboard: health, reconciliation/drift panel, episodes + explanations, agent action log | G1 | Must (recon panel) / Should (rest) |
| FR-14 | Load generator + throughput measurement (target rate and blast modes) | H1 | Must |
| FR-15 | Chaos/fault-injection suite (broker/processor kills, partitions) | H2 | Must |
| FR-16 | Benchmark detection vs. baseline on a labeled set, including where it loses | I1 | Must |
| FR-17 | CI gate on agent output quality (build fails on regression) | I2 | Should |

## Non-functional requirements (measurable)

| ID | Quality | Target (initial) | Verification |
|---|---|---|---|
| NFR-1 | Detection latency (spine) | Score a window in **≤ 250 ms p99** | Benchmark harness percentile report |
| NFR-2 | Explanation latency (VLM) | **≤ 5 s** per flagged window (rare path, off the hot path) | Timed on flagged episodes |
| NFR-3 | End-to-end detect latency | Event → flag in **≤ 2 s p99** | Trace timestamps end to end |
| NFR-4 | Throughput | Sustain **≥ 20,000 events/s** on the reference laptop with bounded lag (validate; report actual) | Load test + consumer-lag metric |
| NFR-5 | Scalability | Near-linear throughput vs. parallelism up to core saturation; name the plateau | Parallelism sweep → curve |
| NFR-6 | Correctness | Exactly-once inside Flink/Kafka; effectively-once at serving; **reconciliation drift = 0** over a **≥ 4-hour** run | Reconciliation harness; restart test |
| NFR-7 | Recovery | After each injected fault, return to a consistent state with bounded lag in **≤ 60 s**; **≥ 3** fault modes covered | Chaos suite with recovery assertions |
| NFR-8 | **Effectiveness (core)** | **Measurable false-positive reduction** under injected pipeline + deploy events vs. the unconditioned baseline — initial target **≥ 50%** of artifact-driven FPs suppressed; report the measured delta honestly (incl. any regressions) | Controlled fault injection; FP count vs. baseline |
| NFR-9 | Observability | Metrics, dashboards, and alerts for **every** service; reconciliation panel live | Manual + smoke checks |
| NFR-10 | Reproducibility | One-command bring-up; infra-as-code; **no** hardcoded secrets | Clean-clone run; secret scan |
| NFR-11 | Safety | **100%** of agent actions pass the deterministic gate before running; execution sandbox-only; no unsandboxed side effects | Gate unit tests; sandbox isolation test |
| NFR-12 | Maintainability | Tests for every component; CI green; an ADR per real decision | CI status; `docs/DECISIONS.md` |
| NFR-13 | Resource fit | Runs within one laptop's resources (heavy model serving one-at-a-time; hosted endpoint permitted for the VLM) | Local run under resource monitor |

## Constraints & assumptions
- Single laptop / small local cluster; a demonstration of correctness and scaling **patterns**, not hyperscale.
- Live data feed is a free, public, always-on source (TBD); development proceeds on the synthetic load generator.
- Foundation model scores windowed batches, not raw ticks, to meet NFR-1.

## Traceability
Every FR maps to a user story (column above); every user story maps to the problem
statement's in-scope items; NFR-6/7/8 are the measurable form of the problem statement's
success criteria (correctness proven, recovery bounded, false positives reduced).

## Out of scope (v2)
Alert correlation/flooding; drift/staleness adaptation; additional conditioning signals via
FR-9's interface. (Rationale: see the ADR in `PROBLEM_STATEMENT.md` §8.)

---
*v1.0 — Requirements artifact. Targets validated and revised in Sprint 2.*
