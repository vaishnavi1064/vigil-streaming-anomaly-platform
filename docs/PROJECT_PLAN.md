# Real-Time Anomaly Detection & Self-Remediating Agent Platform
### Master Project Document — v1.1

> **What this document is.** The single source of truth for the project: the problem it
> solves, who it serves, what it must do, how it is built, the order in which it gets
> built, and how it sits against prior work. Living document — belongs at
> `docs/PROJECT_PLAN.md`. Everything after Section 5 is meant to be revised as the build
> teaches you things.
>
> **v1.1 changelog.** Added Section 15 (Related work, prior art & novelty positioning)
> from a dedicated literature review, and expanded Section 16 (References) into a
> categorized list. Supersedes the v1.0 plan.

---

## 1. Executive summary

A production-shaped platform that ingests a **high-velocity real-time data stream**,
detects anomalies in it under **strict correctness guarantees**, **explains** each flagged
anomaly in human-readable terms, and uses a **safety-gated AI agent** to diagnose and
remediate issues automatically.

The detection design is the distinctive part. It uses **two detectors with different jobs**:

- **Fast spine — a zero-shot time-series foundation model** (Chronos-/TimesFM-class) scores
  *every* sliding window in real time. It keeps up with the firehose and needs no labeled
  training data.
- **Slow explainer — a vision-language model (VLM)** runs *only on the windows the spine
  already flagged*. It renders that window as a chart and reads it like a human analyst,
  producing a plain-language "here is what looks wrong." Because it fires only on rare
  anomalies, it never becomes a throughput bottleneck.

The VLM's explanation feeds the agent's diagnoser as grounded evidence, so detection,
explanation, and remediation are one connected pipeline.

**One-line pitch:** *"A real-time streaming platform with provable exactly-once correctness
that detects anomalies zero-shot with a time-series foundation model, explains them with a
vision-language model, and auto-remediates through a safety-gated agent — all chaos-tested
and continuously evaluated in CI."*

---

## 2. Problem statement

Operational systems that emit high-velocity telemetry (industrial sensors, market data,
service metrics) fail in ways that are **rare, unlabeled, and time-sensitive**. Three
problems compound:

1. **You cannot label the future.** A live stream has no answer key. Supervised detectors
   that need labeled failures cannot be trained on data nobody has seen yet, so they are a
   poor fit for a genuinely live feed.
2. **Detection alone is not actionable.** A raw anomaly score ("window 4,182 is unusual")
   does not tell an operator *what* is wrong or *what to do*. The gap between "something is
   off" and "here is the fix" is where downtime lives.
3. **Correctness is usually assumed, not proven.** Streaming systems routinely lose or
   double-count events under node failures and network partitions, silently corrupting
   exactly the signal an anomaly detector depends on.

**This project addresses all three:** it detects anomalies zero-shot (no labels required),
explains and diagnoses them automatically, remediates them through a gated agent, and does
so on a streaming backbone whose correctness is continuously *proven*, not assumed.

---

## 3. Goals & success criteria

### 3.1 Engineering goals
- A running end-to-end pipeline from live ingestion to auto-remediation.
- **Provable** exactly-once processing inside the streaming core, with a reconciliation
  harness that continuously demonstrates zero data loss.
- Chaos-tested recovery: the system returns to a consistent state and bounded lag after
  injected broker kills, processor kills, and network partitions.
- A grounded, safety-gated agent whose output quality is regression-tested in CI.

### 3.2 Career goal (the real reason this exists)
This is a **portfolio project built to land a job.** Target markets, in priority order:

- **Primary — Streaming / real-time data-platform engineer (US).** Screens for Flink
  internals (checkpointing, state backends, watermarks) and exactly-once. This is the
  high-volume market and the correctness backbone is aimed squarely at it.
- **Secondary — AI-infra / applied-AI engineer (US).** Values model serving, RAG, and eval
  pipelines. The foundation-model detector, vLLM serving, agent, and eval stack open this
  door.
- **Concrete near-term goal:** use the project to get selected into a Meta-sponsored
  data-engineering expo program (lead with the correctness backbone and the scale story).

### 3.3 Success criteria (measurable)
- [ ] End-to-end demo runs from a single command and survives a live-injected fault.
- [ ] Reconciliation panel shows zero drift across a multi-hour run.
- [ ] Chaos suite: at least 3 failure modes injected, each recovering to a verified
      consistent state with bounded lag.
- [ ] Detection benchmarked against a cheap baseline on a **labeled** dataset, with results
      (including where the foundation model *loses*) reported honestly.
- [ ] Scale: measured sustained throughput + a throughput-vs-parallelism curve + backpressure
      behavior, with a grounded projection to cluster scale.
- [ ] Agent passes a DeepEval CI gate; a quality regression blocks the build.
- [ ] README opens with an architecture diagram and a <60s demo (GIF or video).

---

## 4. Requirements

### 4.1 Stakeholders & personas
For a portfolio system the "users" are simulated, but defining them keeps the design honest.

| Persona | Cares about | The system gives them |
|---|---|---|
| **Ops / SRE operator** (simulated end user) | Catching failures early, knowing what to do | Live health panel, explained anomalies, auto-remediation |
| **Platform engineer** (you, the builder) | Correctness, throughput, recoverability | Exactly-once core, reconciliation harness, chaos suite |
| **Hiring manager** (the *actual* stakeholder) | Evidence of real skill under scrutiny | Provable claims, honest benchmarks, clean docs |

### 4.2 Functional requirements

| ID | Requirement |
|---|---|
| FR-1 | Ingest a live high-velocity stream over WebSocket, with gap-detection and REST backfill at the ingestion boundary. |
| FR-2 | Process the stream with exactly-once semantics inside Flink/Kafka (2PC + transactional-ID epoch fencing). |
| FR-3 | Score each sliding window for anomalies in real time using a zero-shot time-series foundation model. |
| FR-4 | For each *flagged* window only, render it to a chart image and produce a human-readable anomaly explanation via a VLM. |
| FR-5 | Persist detected episodes (with score, window, and explanation) and expose them for query. |
| FR-6 | Continuously reconcile ingested vs. processed event counts using per-stage identity invariants, proving zero drift. |
| FR-7 | Agent diagnoses each anomaly against retrieved runbooks, proposes remediation, passes it through a deterministic safety gate, and executes approved actions in a sandbox. |
| FR-8 | Serve a React dashboard: live health, reconciliation status, detected anomalies + explanations, and the agent's action log. |
| FR-9 | Evaluate agent output quality continuously in CI; a quality regression fails the build. |

### 4.3 Non-functional requirements

| ID | Requirement | Target (tune during Phase 2) |
|---|---|---|
| NFR-1 | Detection latency (spine) | Score a window within a defined budget (e.g. ≤ 250 ms) |
| NFR-2 | Explanation latency (VLM) | Seconds is acceptable — it fires only on flagged windows |
| NFR-3 | Throughput | Sustain the live feed's peak rate without unbounded lag |
| NFR-4 | Correctness | Exactly-once inside Flink/Kafka; effectively-once at serving; reconciliation proves zero drift |
| NFR-5 | Recovery | Return to consistent state + bounded lag after broker/processor kill and network partition (chaos-verified) |
| NFR-6 | Observability | Metrics, dashboards, and alerts for every service |
| NFR-7 | Reproducibility | Infra-as-code; one-command bring-up; no hardcoded secrets |
| NFR-8 | Safety | Every agent action passes a deterministic non-LLM gate before running; actions run only in a sandbox |

### 4.4 Constraints & assumptions
- Runs on a **laptop / local machine** (single-node or small local cluster). This is a
  demonstration of correctness and scaling *patterns*, not a hyperscale deployment. Scale is
  proven by measured throughput + a parallelism curve + a grounded projection (Section 10),
  not by running big.
- The live feed is a **free, public, always-on** source (see Section 9).
- The foundation model runs on windowed batches, **not** every raw tick, to stay within the
  latency budget. Heavy model serving (vLLM, VLM) runs one phase at a time, or via a hosted
  endpoint for the VLM, to fit laptop resources.

### 4.5 Explicitly out of scope (v1)
- Multi-tenant auth, RBAC, and production-grade security hardening.
- Horizontal autoscaling beyond a fixed small cluster.
- Retraining/fine-tuning the *foundation model itself* (it is used zero-shot; the fine-tuned
  small model is the **agent's** tool-calling model — a separate component).

---

## 5. System architecture

### 5.1 Layers (high level)

```
                         ┌─────────────────────────────────────────┐
                         │              React Dashboard             │
                         │  health · reconciliation · anomalies ·   │
                         │            agent action log              │
                         └───────────────────▲─────────────────────┘
                                             │ REST / WebSocket
                         ┌───────────────────┴─────────────────────┐
                         │        Backend API (FastAPI)             │
                         └───────────────────▲─────────────────────┘
                                             │
  Live feed ─► Ingestion ─► Kafka ─► Flink (exactly-once) ─► Detection ─► Storage
   (WS+REST)   (gap-fill)    │         │  · event-time         │  spine     · ClickHouse
                             │         │  · watermarks         │  (TSFM)    · Iceberg
                             │         │  · RocksDB state      │  ─►flag─►  · Postgres
                             │         │  · 2PC exactly-once   │  VLM explainer
                             │         └───► Reconciliation ◄──┘     │
                             │               harness                 ▼
                             │                                   AI Agent
                             │                       Supervisor→Diagnoser→Planner
                             │                       →Safety Gate→Executor (sandbox)
                             └───► Chaos / fault injection            │
                                                                  Runbook RAG
        Observability: Prometheus · Grafana · Alertmanager  (across all services)
        Delivery: Docker · Kubernetes · Terraform · GitHub Actions
        Agent quality: Ragas (dev) · DeepEval (CI gate) · TruLens (prod tracing)
```

### 5.2 End-to-end data flow
1. The live feed arrives over WebSocket; the ingestion boundary detects gaps and backfills
   via REST, then publishes to Kafka.
2. Flink consumes with event-time processing and watermarks, keeps state in RocksDB,
   checkpoints, and emits exactly-once via two-phase commit.
3. The **detection spine** (foundation model) scores each sliding window. Normal windows
   flow on to storage; **flagged** windows are handed to the VLM explainer.
4. The **VLM explainer** renders the flagged window as a chart and produces a human-readable
   explanation, attached to the detected episode.
5. Detected episodes land in Postgres; serving data lands in ClickHouse; the durable event
   lake (Iceberg) is the reconciliation source of truth.
6. The **agent** picks up the episode, retrieves relevant runbook context, diagnoses, plans a
   remediation, passes it through the safety gate, and — if approved — executes in a sandbox.
7. The **React dashboard** (via the FastAPI backend) shows all of it live.

### 5.3 The dual-detector design (the differentiator)
- **Why a foundation model as the spine:** zero-shot means no labels required (fits a live
  feed), it runs natively on your model-serving stack (vLLM), and it is a dominant research
  direction of 2025–2026.
- **Why a VLM only on flagged windows:** running a large vision model on every window would
  blow the latency budget; running it only on the rare anomaly gives you the explainability
  with no throughput cost. This is the screen-then-verify pattern from VLM4TS (Section 15).
- **How they connect:** the VLM's explanation becomes grounded input to the agent's
  diagnoser, so the flashy piece has a real functional job rather than being a demo bolt-on.

### 5.4 The agent loop
`Supervisor → Diagnoser → Planner → deterministic Safety Gatekeeper → non-LLM Executor`,
grounded in runbook retrieval, executing against a sandbox. Every decision is retrieved,
gated, and traced.

### 5.5 Correctness model (scoped honestly)
- **At-least-once** at the WebSocket edge (gap-detection + backfill).
- **Exactly-once** inside Flink/Kafka (2PC + epoch fencing).
- **Effectively-once** at the serving layer.
- Claims are scoped to where each guarantee actually holds — never overstated. The
  reconciliation harness *proves* the interior guarantee continuously.

---

## 6. Detailed technology stack

| Layer | Technology | Why it's here |
|---|---|---|
| **Ingestion** | WebSocket client + REST backfill | Live data with durability at the edge (gap-detect, backfill) |
| **Streaming** | Apache Kafka | Partitioned, replicated, transactional message bus |
| **Processing** | Apache Flink (event-time, watermarks, RocksDB, checkpointing, unaligned checkpoints, 2PC exactly-once) | Stateful stream processing with the exactly-once guarantee that lands data-platform roles |
| **Pattern detection** | Flink CEP | Temporal / complex-event patterns alongside the model detector |
| **Detection — spine** | Time-series foundation model, Chronos-/TimesFM-class, **zero-shot** | Real-time, label-free detection; native to the serving stack |
| **Detection — explainer** | Vision-language model (VLM4TS-style chart reading), **triggered on flagged windows only** | Human-readable anomaly explanations without throughput cost |
| **Serving store** | ClickHouse | Fast real-time OLAP for the dashboard |
| **Event lake** | Apache Iceberg on object storage | Durable lake + reconciliation source of truth |
| **App state** | PostgreSQL | Application state and detected-episode storage |
| **Correctness** | Reconciliation harness (per-stage identity invariants) | Continuously proves ingested = processed, zero drift |
| **Reliability** | Chaos / fault injection | Kill brokers/processors, inject partitions; verify recovery |
| **Scale harness** | Synthetic load generator + parallelism sweep | Measured throughput, throughput-vs-parallelism curve, backpressure |
| **Agent tools** | MCP server (typed interfaces) | The typed surface the agent is allowed to act through |
| **Agent model** | Small LM (Qwen-class 7–8B), fine-tuned via QLoRA/Unsloth | Reliable **structured tool-calling** (this is the fine-tuned model, distinct from the zero-shot detector) |
| **Model serving** | vLLM on Kubernetes | Efficient single-model inference serving |
| **Agent flow** | Supervisor → Diagnoser → Planner → Safety Gate → Executor + sandbox | Grounded, gated, auditable remediation |
| **Grounding** | Runbook retrieval (RAG) | Diagnoses grounded in operational docs, never free-form |
| **Eval — dev** | Ragas | Tune retrieval during development |
| **Eval — CI** | DeepEval | pytest-native gates; quality drift fails the build |
| **Eval — prod** | TruLens | Trace/observe agent outputs in production |
| **Backend API** | FastAPI | Serves the dashboard; exposes health, episodes, agent log |
| **Frontend** | React | Live health + reconciliation dashboard |
| **Orchestration** | Kubernetes | Runs all services |
| **IaC** | Terraform | Infrastructure-as-code |
| **Containers** | Docker | Containerization |
| **CI/CD** | GitHub Actions | Build, test, eval-gate, deploy |
| **Observability** | Prometheus / Grafana / Alertmanager | Metrics, dashboards, alerting |

> **Note on the two models — a common interview trap, so keep it straight.** The *detector*
> is a time-series **foundation model used zero-shot** (no fine-tuning). The *agent's* model
> is a **small LM fine-tuned with QLoRA** for tool-calling. Two different models, two
> different jobs. Do not conflate them.

---

## 7. Frontend design (React dashboard)

A single-page dashboard with a live health-and-reconciliation focus. Panels:

- **Live health strip** — ingestion rate, Flink lag, per-service up/down, current throughput.
- **Reconciliation panel** — ingested vs. processed counts and the drift figure (target:
  zero), updating live. This is the panel that proves the correctness story at a glance.
- **Anomaly feed** — a stream of detected episodes: timestamp, score, the window chart, and
  the VLM's plain-language explanation.
- **Agent action log** — for each episode: the diagnosis, the proposed action, the safety
  gate's verdict (approved/rejected), and the execution result.
- **Chaos control (demo mode)** — buttons to inject a fault live during a demo, so a viewer
  *watches* the system detect, explain, and recover.

Data reaches the frontend from the FastAPI backend over REST (snapshots) and a WebSocket
(live updates).

---

## 8. Backend / API design (FastAPI)

A thin API layer between the platform and the dashboard.

| Endpoint | Method | Returns |
|---|---|---|
| `/health` | GET | Per-service status, throughput, lag |
| `/reconciliation` | GET | Ingested/processed counts + drift |
| `/anomalies` | GET | Recent detected episodes (score, window, explanation) |
| `/anomalies/{id}` | GET | One episode with full detail + agent trace |
| `/agent/actions` | GET | Agent action log (diagnosis → gate → execution) |
| `/chaos/inject` | POST | Trigger a fault (demo mode only) |
| `/ws/live` | WS | Push live health, reconciliation, and new anomalies |

The API reads serving data from ClickHouse and episode/agent state from Postgres. It owns no
business logic beyond shaping data for the UI.

---

## 9. Data plan

Use **two data sources for two different jobs** — this resolves the "live data has no answer
key" problem cleanly:

- **Live stream (the showcase).** A free, public, always-on WebSocket feed — e.g. a live
  market-data trade stream or a public sensor firehose. The zero-shot foundation model runs
  on it with **no labels required**, proving the pipeline works on genuinely live, unseen
  data.
- **Labeled benchmark (the proof).** A dataset with documented ground-truth failures (e.g.
  MetroPT-3 or a standard TSAD benchmark). Because the foundation model is zero-shot, the
  *same detector* runs here too — but now you can compute real precision/recall and
  **benchmark honestly against a cheap baseline** (Section 10).

> Live feed still to be chosen. Development proceeds against the synthetic load generator so
> nothing is blocked; the real feed is wired in at the end of Phase 1.

---

## 10. Evaluation, benchmarking & scale plan

This is where you repeat the **intellectual-honesty pattern** that was the strongest part of
your previous project.

**Detection quality**
- **Baseline first.** Implement a cheap detector (rolling z-score over sliding windows with
  Welford's algorithm, or Isolation-Forest-over-window). This is the bar the foundation model
  must beat — and it stays as the permanent benchmark baseline.
- **Metrics that survive rare events.** Report precision/recall and false-alarm rate at
  matched alarm budgets — not accuracy/ROC-AUC, which mislead when anomalies are <1% of data.
- **Report where it loses.** Recent research shows foundation models do not universally beat
  simple methods on anomaly detection (Section 15). If yours loses on some regime, say so and
  show the boundary. "I measured where the trendy method is the wrong tool" is a stronger
  story than an unexamined win.

**Scale (laptop-honest)**
- **Measured throughput**, always reported with the hardware it ran on.
- **Throughput-vs-parallelism curve** across Kafka partitions / Flink parallelism; expect
  near-linear scaling to core saturation, then a plateau — name where and why it saturates.
- **Backpressure under overload**: flood it, show graceful backpressure and lag recovery.
- **Grounded projection**: from measured per-core throughput, project to cluster scale.

**Agent quality**
- Ragas during development; DeepEval as a CI gate that fails the build on output-quality
  drift; TruLens for production tracing.

---

## 11. SDLC & delivery plan

### 11.1 Methodology
**Iterative / incremental (Agile-style), phased.** The rule: *the system runs end-to-end at
every phase boundary and only gets deeper* — you are never sitting on a half-built pile that
doesn't run, and you always have something demoable if you pause.

### 11.2 Phases & milestones

| Phase | Goal | Definition of Done |
|---|---|---|
| **0 — Requirements & design** | This document; repo skeleton; pick the live feed | Plan reviewed; repo scaffolded; feed chosen |
| **1 — Thin spine** | loadgen → Kafka → consumer → windowing → z-score baseline detector → episodes in Postgres; then add the foundation-model detector; minimal dashboard | Data flows end-to-end; an anomaly appears; foundation model runs alongside the baseline |
| **2 — Correctness backbone** *(the hard, high-value part)* | Flink; event-time + watermarks; exactly-once (2PC); reconciliation harness; chaos suite; scale/throughput harness | Reconciliation shows zero drift; ≥3 injected faults recover to a verified consistent state; throughput curve produced |
| **3 — Serving & storage** | ClickHouse (fast queries) + Iceberg (durable lake / source of truth) | Dashboard reads from ClickHouse; reconciliation checks against Iceberg |
| **4 — Explainability + agent** | VLM explainer on flagged windows; MCP tools; Diagnoser→Planner→Safety Gate→Executor on a sandbox; runbook RAG; off-the-shelf model on vLLM | Flagged anomaly gets an explanation; agent proposes → gate approves/rejects → sandbox executes |
| **5 — Reliable & tuned agent** | QLoRA fine-tune of the small tool-calling model; Ragas/DeepEval/TruLens eval pipeline | DeepEval gate live in CI; a quality regression blocks the build |
| **6 — Production wrapper** | Harden Docker/K8s/Terraform; finish GitHub Actions; observability everywhere; polish README + demo | One-command bring-up; architecture diagram + <60s demo in README |

### 11.3 Documentation deliverables (write as you go, not at the end)
- `README.md` — headline result, architecture diagram, quickstart, <60s demo.
- `docs/PROJECT_PLAN.md` — this document, kept current.
- `docs/ARCHITECTURE.md` — the diagrams and the correctness model.
- `docs/EVALUATION.md` — benchmark methodology, metrics, honest findings, scale results.
- `docs/CORRECTNESS.md` — the exactly-once boundary and reconciliation design.
- `docs/CHAOS.md` — failure modes injected and recovery evidence.
- `docs/RELATED_WORK.md` — the prior-art review (Section 15) as a standalone file.
- `docs/DECISIONS.md` — an ADR-style log (why foundation model, why VLM-on-flag-only, etc.).

---

## 12. Testing strategy

| Level | What it covers |
|---|---|
| **Unit** | Ingestion gap-detection, windowing, detector scoring, safety-gate logic |
| **Integration** | Kafka→Flink→storage path; API↔stores; agent tool calls end-to-end |
| **Correctness** | Reconciliation invariants; exactly-once under restart |
| **Chaos** | Broker kill, processor kill, network partition, load spike → verified recovery |
| **Eval-in-CI** | DeepEval gates on agent output quality (build fails on drift) |

---

## 13. Risks & mitigations

| Risk | Mitigation |
|---|---|
| **Scope is huge; project stalls at 80%** | Strict phase boundaries; a demoable system at every boundary; ship v1 before chasing depth |
| **Foundation model too slow per window** | Score windowed batches, not raw ticks; quantize; keep the spine small |
| **VLM becomes a bottleneck** | It fires *only* on flagged windows, by design |
| **Detection results look weak on live data** | Use the labeled benchmark for provable metrics; report honestly |
| **"Exactly-once" claim challenged in interview** | Claims are scoped precisely; reconciliation harness is the evidence |
| **Two models confused (detector vs. agent)** | Documented explicitly in Section 6; keep the distinction crisp everywhere |
| **Claiming novelty that prior work owns** | Cite VLM4TS / Argos / AIOps work (Section 15); frame the contribution as integration + rigor, not a new algorithm |
| **Full stack too heavy for a laptop** | Run heavy model serving one phase at a time; hosted endpoint for the VLM; quantized detector |

---

## 14. Portfolio & interview positioning

- **Build once, position twice.** Lead the README and résumé bullet with the *streaming
  correctness backbone* for data-platform applications; lead with the *foundation-model
  detector + agent + eval stack* for AI-infra applications. Same repo, two front doors.
- **Depth beats breadth-that-collapses.** The correctness backbone (Flink, exactly-once,
  reconciliation, chaos) is the layer built *deepest* — it gives a specialist interviewer
  something meaty and is what the high-volume market screens for.
- **Honesty is the hook.** Benchmark against a baseline and report where the detector loses;
  scope every correctness claim. This turns the choices into a story, not a buzzword.
- **The memorable twist, framed honestly.** "I applied the VLM4TS screen-then-verify pattern
  so a vision-language model explains anomalies — but only on flagged windows, so it never
  slows the stream." Naming the prior work you built on is *more* credible, not less.

---

## 15. Related work, prior art & novelty positioning

A literature review across each component. **Headline: every individual component here has
strong, recent prior art. The contribution of this project is the end-to-end integration and
the engineering rigor (provable correctness + honest benchmarking) — not a new algorithm.**
State it that way; it is both true and defensible.

### 15.1 Foundation-model detectors (the spine)
A crowded, fast-moving area. Recent systems build anomaly detection directly on time-series
foundation models: **STAR** adds a state-aware adapter to boost TSFMs for detection;
**TimeRadar** is a domain-rotatable foundation model for time-series anomaly detection;
others exploit TSFM intermediate representations, foundation auto-encoders, or synthetic-data
zero-shot training. Important counterpoint: a 2025 paper (**"When Foundation Models are
One-Liners"**) argues the current way of applying TSFMs to anomaly detection — via
reconstruction or forecasting error — is flawed and proposes alternatives; separate work
(**Zhou & Yu, ICLR 2025**) reports limited gains from LLM-style approaches to anomaly
detection. *Implication:* leading with a TSFM detector is on-trend but contested — which is
exactly why the honest baseline benchmark (Section 10) matters.

### 15.2 Vision-language chart-reading (the explainer) — closest match, cite carefully
**VLM4TS** (He, Alnegheimish & Reimherr; AAAI 2026 oral; Penn State / MIT / Amazon) is almost
exactly this project's detection design: a two-stage vision framework that decouples
localization from verification — a lightweight vision encoder (ViT4TS) screens candidate
anomalies from short-window plots, then a vision-language model refines them using longer
horizons. Without any time-series training it reports a 24.6% F1-max improvement over the
best baseline while being ~36x more token-efficient, and it has public code. Earlier work
(**TAMA**) prompted VLMs on rolling-window plots but incurred high token costs; a 2026 line
("**Tiny but Trusted**") pushes efficient vision-language reasoning for TSAD. **The "cheap
screen first, VLM only on flagged windows" idea in this project IS the VLM4TS screen-then-
verify pattern.** That is good — the instinct matches an award paper — but it must be framed
as *applying* VLM4TS for explanation + agent grounding, never as an original invention.

### 15.3 Agentic time-series detection (detection + reasoning)
The closest single system is **Argos** (Gu et al., Microsoft; arXiv 2501.14170): an agentic
system that uses LLMs to autonomously generate explainable, reproducible anomaly rules via
multiple collaborative agents for low-cost online detection, improving F1 by up to 9.5% and
28.3% on public datasets. Notably its authors are candid that it is not suited to directly
replace existing detection systems — the same honesty posture this project aims for. Argos
differs from this project: it is rule-generation based, with no VLM and no exactly-once
streaming backbone.

### 15.4 Self-remediating incident agents (the agent)
A **mature** field — worth knowing so the agent is framed as standard-shaped, not novel.
There are multi-agent LLM frameworks for root-cause analysis targeting alert fatigue in
distributed systems; production studies of ReAct agents with retrieval tools on real
incidents (Roy et al., Microsoft, FSE 2024); LLM-based cloud-incident RCA (Chen et al.,
EuroSys 2024); and dedicated benchmarks/frameworks — **AIOpsLab** (MLSys 2025) and
**ITBench** (ICML 2025) for evaluating AI agents on autonomous cloud / IT tasks, **STRATUS**
(NeurIPS 2025) for autonomous reliability engineering, and **OpenRCA** (ICLR 2025). The
"grounded in runbooks" design is itself established: **agentic troubleshooting-guide
automation** (Mao et al., 2510.10074) and RAG-based incident-resolution recommendation. The
Supervisor→Diagnoser→Planner→Safety-Gate→Executor loop is a standard-shaped AIOps agent.

### 15.5 The streaming backbone (the primary differentiator)
The Kafka + Flink + exactly-once anomaly-detection pattern is thoroughly trodden: there are
end-to-end guides for production exactly-once streaming platforms on Kafka and Flink, and
streaming anomaly-detection tutorials on Kafka + Flink + PostgreSQL. Even the z-score
baseline is textbook — running mean/std via Welford's algorithm, z-score over sliding windows
keyed by entity and metric. The standard scaling rule is roughly one Flink task per Kafka
partition. There is even a Flink-Agents + Kafka approach for real-time industrial-equipment
anomaly detection. *Implication:* this backbone does the industry-standard thing well — good
for a portfolio, not research-novel — so the depth comes from *proving* correctness
(reconciliation + chaos) rather than from the pattern itself.

### 15.6 Novelty positioning (the defensible claim)
No prior system found combines **all four** of: a provably-correct exactly-once streaming
backbone (reconciliation harness + chaos testing), a zero-shot foundation-model spine, a VLM
explainer on flagged windows, *and* a safety-gated runbook-grounded remediation agent — as
one end-to-end pipeline. Argos does agentic detection but rule-based, no VLM, no exactly-once.
VLM4TS does detection + explanation but no streaming, no remediation. The AIOps systems do
RCA + remediation but *consume* metrics; they do not own the correctness layer. **The
integration is the contribution; the correctness backbone plus honest benchmarking is the
depth.** Interview-ready line: *"I'm not claiming a new algorithm — I built the system-level
integration on a provably-correct backbone, and I measured the detector honestly against a
baseline, including where it loses."*

---

## 16. References

**A. Detector — time-series foundation models & baselines**
- Liang, Wen, Nie, Jiang, Jin, Song, Pan, Wen. *Foundation Models for Time Series Analysis: A Tutorial and Survey.* KDD 2024.
- Ansari et al. *Chronos: Learning the Language of Time Series.* TMLR 2024.
- Shi et al. *Time-MoE: Billion-Scale Time Series Foundation Models with Mixture of Experts.* ICLR 2025.
- *STAR: Boosting Time Series Foundation Models for Anomaly Detection through State-aware Adapter.* arXiv:2510.16014, 2025.
- *TimeRadar: A Domain-Rotatable Foundation Model for Time Series Anomaly Detection.* arXiv:2602.19068, 2026.
- *Leveraging Intermediate Representations of Time Series Foundation Models for Anomaly Detection.* arXiv:2509.12650, 2025.
- *Towards Foundation Auto-Encoders for Time-Series Anomaly Detection.* arXiv:2507.01875, 2025.
- Lan et al. *Towards Foundation Models for Zero-Shot Time Series Anomaly Detection: Leveraging Synthetic Data and Relative Context Discrepancy.* arXiv:2509.21190, 2025.
- *When Foundation Models are One-Liners: Limitations and Future Directions for Time Series Anomaly Detection.* OpenReview, 2025.
- Zhou & Yu. *Can LLMs Understand Time Series Anomalies?* ICLR 2025.
- Zhou, Brif, Lourentzou. *mTSBench: Benchmarking Multivariate Time Series Anomaly Detection and Model Selection at Scale.* TMLR 2026.
- Xu, Wu, Wang, Long. *Anomaly Transformer: Time Series Anomaly Detection with Association Discrepancy.* ICLR 2022 (classic baseline).

**B. Detector — vision-language / multimodal**
- He, Alnegheimish, Reimherr. *Harnessing Vision-Language Models for Time Series Anomaly Detection (VLM4TS).* arXiv:2506.06836; AAAI 2026 (Oral). Code: github.com/ZLHe0/VLM4TS.
- *Tiny but Trusted: Efficient Vision-Language Reasoning for Time-Series Anomaly Detection.* arXiv:2605.30344, 2026.
- *Can Multimodal LLMs Perform Time Series Anomaly Detection?* arXiv:2502.17812, 2025.
- Liu et al. *Large Language Models can Deliver Accurate and Interpretable Time Series Anomaly Detection.* arXiv:2405.15370, 2024.
- Survey list: D2I-Group/awesome-vision-time-series; mala-lab/Awesome-Anomaly-Detection-Foundation-Models.

**C. Agentic time-series detection**
- Gu et al. *Argos: Agentic Time-Series Anomaly Detection with Autonomous Rule Generation via Large Language Models.* arXiv:2501.14170, 2025. Microsoft; code: github.com/microsoft/argos.
- *LLM-Assisted Logic Rule Learning: Scaling Human Expertise for Time Series Anomaly Detection.* arXiv:2601.19255, 2026.

**D. Incident RCA & auto-remediation agents (AIOps)**
- Chen et al. *Automatic Root Cause Analysis via Large Language Models for Cloud Incidents.* EuroSys 2024.
- Roy et al. *Exploring LLM-Based Agents for Root Cause Analysis.* FSE 2024 (Companion).
- *AIOpsLab: A Holistic Framework for Evaluating AI Agents for Enabling Autonomous Cloud.* MLSys 2025.
- *ITBench: Evaluating AI Agents across Diverse Real-World IT Automation Tasks.* ICML 2025.
- *STRATUS: A Multi-agent System for Autonomous Reliability Engineering of Modern Clouds.* NeurIPS 2025.
- Mao et al. *Agentic Troubleshooting Guide Automation for Incident Management.* arXiv:2510.10074, 2025.
- Xu et al. *OpenRCA: Can Large Language Models Locate the Root Cause of Software Failures?* ICLR 2025.
- Curated list: Jun-jie-Huang/awesome-LLM-AIOps.

**E. Streaming systems, exactly-once & practitioner references**
- Aiven. *Streaming Anomaly Detection with Apache Flink, Apache Kafka and PostgreSQL* (tutorial).
- Streamkap. *Anomaly Detection in Streaming Data with Flink* (z-score / Welford over sliding windows).
- *Real-time Event Joining in Practice with Kafka and Flink.* arXiv:2410.15533, 2024 (exactly-once vs at-least-once tradeoffs at scale).

**F. Datasets**
- Veloso et al. *The MetroPT Dataset for Predictive Maintenance.* Scientific Data, 2022 (candidate labeled benchmark).

---
*v1.1 — living document. Revise per phase.*
