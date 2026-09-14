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
   deploy markers) and decides: *real* vs. *attributed-to-context*. The verdict is held
   behind an event-time barrier keyed to the fleet watermark, so it is taken once every
   channel has passed the span it asks about rather than against whichever episodes closed
   first (ADR-037).
6. Real anomalies → episode → (VLM explanation) → agent (diagnose/gate/execute) → dashboard.
   Attributed anomalies → recorded, not paged.

## 4. Core mechanism: context-conditioned detection
The novelty lives in one join and one policy.

- **Context signal (contract).** `{window_id, t_start, t_end, kind: pipeline|deploy|…, severity, detail}`. Sources: the reconciliation harness (pipeline) and a deploy-marker feed. Extensible — new kinds implement the same `ContextSignalSource` interface (FR-9).
- **Conditioning policy.** Input: an episode plus every context signal overlapping it, and the fleet inventory. Output: `{status: real | attributed, attributed_to, verdict, reason}`. The naive rule this section originally specified -- attribute anything overlapping a deploy -- is blanket suppression, and it was built, measured and rejected: it destroyed recall on exactly the population ADR-015 exists to catch (`docs/EVALUATION.md` section 3.4, v1). What runs is four tests, each of which can only ever *refuse*:
  1. **Scope** (ADR-024). A deploy that never touched this channel cannot explain it.
  2. **Mechanism** (ADR-024). A pipeline event that lost nothing, duplicated nothing and reordered nothing has no way to have distorted a value, whatever its severity says.
  3. **Corroboration** (ADR-024, ADR-025). The channel's in-scope siblings must have departed within a few seconds of it, compared on true onsets rather than window boundaries (ADR-035).
  4. **Blast radius** (ADR-038). The channels that moved must have the *shape* of the rollout rather than of a failure domain: spread across machines and cabinets, not confined to one. A machine failing is synchronous inside its own scope, so tests 1 to 3 cannot separate it from a rollout and this is what does.
- **Fleet inventory.** `channel -> {node, rack, deploy ring}`, read from a CMDB (here, written by the load generator). Operational fact, carrying nothing about whether anything is wrong. Absent, the blast-radius test raises no objection rather than refusing everything.
- **Fail-open rule (safety).** If the health signal is **unavailable**, conditioning falls back to *unconditioned* — flags are raised normally, never silently suppressed. Missing context must not hide a real anomaly. (See ADR-007.)

## 5. Key interfaces & schemas
- **Pipeline-health signal** (Kafka topic / API): per window — drift count, lag, gap-fills, duplicates, `disturbed: bool`, severity.
- **ContextSignalSource** (pluggable): `get_signals(window) -> [ContextSignal]`.
- **FleetTopology** (inventory): `footprint(channels) -> {nodes, racks, rings, unplaced}`.
- **Episode** (Postgres): see §6.

## 6. Data model
- **context_events**: `id, kind, t_start, t_end, severity, detail, created_at`.
- **episodes**: `id, t_start, t_end, detector, raw_score, status(real|attributed|suppressed), attributed_to (nullable → context_events.id), explanation (nullable), agent_action_id (nullable), created_at`.
- **agent_actions**: `id, episode_id, diagnosis, proposed_action, gate_verdict, execution_result, trace, created_at`.
- Raw readings are **not** stored in Postgres (serving → ClickHouse; durable lake → Iceberg). **Both are built and running** (ADR-043 to ADR-046).
- **readings** (ClickHouse, `ReplacingMergeTree`): `channel, seq, event_ts, value, injected, origin, ingested_at`, ordered by `(channel, seq, event_ts)`. Plus `window_scores` and a per-minute `AggregatingMergeTree` rollup that the dashboard's series panel reads instead of scanning raw rows.
- **readings** (Iceberg, Parquet on MinIO, partitioned by event day): the durable record and the thing reconciliation audits against. Its catalog is a JDBC catalog in the Postgres above, so the lake needs no extra service.
- **Identity is `(channel, seq, event_ts)`, not `(channel, seq)`** (ADR-046). The per-channel sequence is dense within one producer lifetime and restarts when the producer does, so the pair alone is not unique across a stored table's history.

## 7. Failure-first design (the senior signal)
- **Blast radius.** VLM and agent are isolated rare-path services; if either is down, detection + reconciliation are unaffected — the system degrades to "flag without explanation / without auto-remediation," not to "down."
- **Backpressure.** Flink backpressure propagates to the source; bounded state (RocksDB); the load harness verifies behavior under overload.
- **Degradation modes.** Health signal down → fail-open (raise flags). Foundation model down → fall back to the z-score baseline. Serving store down → episodes still persist to Postgres, and `/health` reports the two stores separately so a reader can see which half is broken. Lake sink down → Kafka retains six hours of runway, and the sink resumes from the offsets in its own last snapshot rather than from a position something else recorded.
- **Recovery.** Checkpoints restore Flink state; the reconciliation harness confirms a consistent state and bounded lag post-fault (NFR-7).

## 8. Correctness model (scoped)
At-least-once at the WebSocket edge; **exactly-once** inside Flink/Kafka (2PC + epoch fencing);
effectively-once at serving. Reconciliation proves the interior guarantee continuously. Claims
are scoped to where they hold.

## 9. Deployment view
**Two topologies, and only one of them has ever run.**

**Local -- runs, and is the one-command bring-up.** Docker Compose: Kafka (KRaft), Postgres,
ClickHouse and MinIO start by default and were measured together at 1.2 GB of the 8.13 GB
Docker VM. Flink is behind a compose profile because its ~2.5 GB does not fit alongside them,
which makes Flink-plus-storage the one combination this file cannot run at once. The API and
the four consumer processes run on the host against those containers. Heavy model serving
(vLLM) runs one phase at a time; the VLM uses a hosted endpoint on the rare path (NFR-13).
Prometheus and Grafana are **not** in compose -- their configuration lives in `monitoring/`
and nothing scrapes locally today.

**Kubernetes -- manifests validated, never applied.** `k8s/` carries 25 resources: StatefulSets
for Kafka, Postgres, ClickHouse and MinIO with `volumeClaimTemplates`; a 2-replica Deployment
plus Service for the API; single-replica Deployments for the detector, reconciler and the two
storage sinks; Jobs for topic and bucket creation; and, outside the default apply set, Flink
and a CronJob for the agent quality gate. `terraform/` owns the namespace, a ResourceQuota, a
LimitRange and the Secret (ADR-047). Everything passes static validation -- kubeconform,
terraform validate, promtool, amtool, 10 checks and 0 failures -- and **no pod has ever been
scheduled from it**. `docs/DEPLOYMENT.md` has the apply procedure and the full list of what
static validation does not establish.

Three differences from the local topology are deliberate rather than incidental: Kafka is
reachable only in-cluster (no host listener), the lake sink is pinned to one replica with
`strategy: Recreate` because its offsets live in the Iceberg snapshot (ADR-044), and the four
consumers carry no probes because every available check was worse than none (ADR-048).

## 10. Decisions
The tradeoffs behind this design are recorded as ADRs in `DECISIONS.md` (detector choice, the
conditioning mechanism, VLM-on-flagged-only, Flink, storage split, fail-open, scope).

---
*v1.1 — Design artifact. Component view is now a rendered diagram (architecture.svg). Revise as sprints expose reality.*
