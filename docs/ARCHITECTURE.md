# Architecture & Design
### Real-Time Anomaly Detection & Self-Remediating Agent Platform
**SDLC phase:** Design — **v1.1** — companion to `REQUIREMENTS.md`; decisions recorded in `DECISIONS.md`

## 1. Design goals
Optimize for the one hard thing: **context-conditioned detection** on a provably-correct
backbone. Everything else (explanation, agent) is thin and isolated so it can't jeopardize
the core. Design principles: correctness is load-bearing, failures are designed for, and the
conditioning mechanism is extensible.

## 2. Component view

![Platform architecture: the data plane runs top to bottom (ingestion, Kafka, Flink, detection, serving, dashboard); the two teal nodes (reconciliation harness to conditioning) are the core contribution. Chaos/load exercises the backbone; the rare path (VLM explanation plus safety-gated agent) branches off only for real anomalies.](architecture.svg)

> Diagram source: `docs/architecture.svg` (portable, light/dark). Regenerate from this doc when the architecture changes.

Responsibilities: ingestion guarantees durability at the edge; Flink owns exactly-once and
windowing; **reconciliation both proves correctness and emits the health signal**; detection
scores windows and applies conditioning; serving/store persist; explanation and agent are
rare-path, off the hot path.

## 3. Data flow (with the conditioning wire)
1. Feed → ingestion (gap-fill) → Kafka.
2. Flink windows the stream (event-time, watermarks, exactly-once).
3. **Detection** scores each window (baseline + foundation model).
4. **Reconciliation** emits a per-window pipeline-health signal in parallel.
5. **Conditioning** joins each anomaly flag with overlapping context signals (pipeline health,
   deploy markers) and decides: *real* vs. *attributed-to-context*.
6. Real anomalies → episode → (VLM explanation) → agent (diagnose/gate/execute) → dashboard.
   Attributed anomalies → recorded, not paged.

## 4. Core mechanism: context-conditioned detection
The novelty lives in one join and one policy.

- **Context signal (contract).** `{window_id, t_start, t_end, kind: pipeline|deploy|…, severity, detail}`. Sources: the reconciliation harness (pipeline) and a deploy-marker feed. Extensible — new kinds implement the same `ContextSignalSource` interface (FR-9).
- **Conditioning policy.** Input: an anomaly flag for window *T* plus all context signals overlapping *T*. Output: `{status: real | attributed, attributed_to, adjusted_score}`. v1 policy: if a pipeline disturbance or active deploy overlaps *T*, mark the flag attributed (not paged); else real.
- **Fail-open rule (safety).** If the health signal is **unavailable**, conditioning falls back to *unconditioned* — flags are raised normally, never silently suppressed. Missing context must not hide a real anomaly. (See ADR-007.)

## 5. Key interfaces & schemas
- **Pipeline-health signal** (Kafka topic / API): per window — drift count, lag, gap-fills, duplicates, `disturbed: bool`, severity.
- **ContextSignalSource** (pluggable): `get_signals(window) -> [ContextSignal]`.
- **Episode** (Postgres): see §6.

## 6. Data model
- **context_events**: `id, kind, t_start, t_end, severity, detail, created_at`.
- **episodes**: `id, t_start, t_end, detector, raw_score, status(real|attributed|suppressed), attributed_to (nullable → context_events.id), explanation (nullable), agent_action_id (nullable), created_at`.
- **agent_actions**: `id, episode_id, diagnosis, proposed_action, gate_verdict, execution_result, trace, created_at`.
- Raw readings are **not** stored in Postgres (serving → ClickHouse; durable lake → Iceberg).

## 7. Failure-first design (the senior signal)
- **Blast radius.** VLM and agent are isolated rare-path services; if either is down, detection + reconciliation are unaffected — the system degrades to "flag without explanation / without auto-remediation," not to "down."
- **Backpressure.** Flink backpressure propagates to the source; bounded state (RocksDB); the load harness verifies behavior under overload.
- **Degradation modes.** Health signal down → fail-open (raise flags). Foundation model down → fall back to the z-score baseline. Serving store down → episodes still persist to Postgres.
- **Recovery.** Checkpoints restore Flink state; the reconciliation harness confirms a consistent state and bounded lag post-fault (NFR-7).

## 8. Correctness model (scoped)
At-least-once at the WebSocket edge; **exactly-once** inside Flink/Kafka (2PC + epoch fencing);
effectively-once at serving. Reconciliation proves the interior guarantee continuously. Claims
are scoped to where they hold.

## 9. Deployment view
Docker Compose on a laptop: Kafka (KRaft), Flink, Postgres, ClickHouse, Iceberg (object store),
FastAPI, React, Prometheus/Grafana. Heavy model serving (vLLM) runs one phase at a time; the VLM
may use a hosted endpoint (rare path) to fit resources (NFR-13).

## 10. Decisions
The tradeoffs behind this design are recorded as ADRs in `DECISIONS.md` (detector choice, the
conditioning mechanism, VLM-on-flagged-only, Flink, storage split, fail-open, scope).

---
*v1.1 — Design artifact. Component view is now a rendered diagram (architecture.svg). Revise as sprints expose reality.*
