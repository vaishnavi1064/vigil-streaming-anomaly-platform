# BUILD.md — Autonomous Build Brief
### Real-Time Anomaly Detection & Self-Remediating Agent Platform

You (Claude Code) are building this project end to end. This file is your execution
playbook. Work through it autonomously — plan, reason about choices, implement, test,
self-verify, document — and keep going phase by phase. You are **done only when the Final
Acceptance Checklist (§8) fully passes.** Do not stop early because a phase "seems" complete;
prove it against that phase's gate first.

---

## 1. Read these first (canonical, in order)
1. `CLAUDE.md` — operating rules and code standards.
2. `docs/PROJECT_PLAN.md` — full spec, stack, phases.
3. `docs/PROBLEM_STATEMENT.md` — the problem, scope, and the ADR on what's deferred.
4. `docs/USER_STORIES.md` — backlog, acceptance criteria, sprint mapping.
5. `docs/REQUIREMENTS.md` — FRs (traced) and measurable NFRs.
6. `docs/ARCHITECTURE.md` + `docs/architecture.svg` — the design and the core mechanism.
7. `docs/DECISIONS.md` — existing ADRs; append to this as you make choices.

If anything in this file conflicts with `CLAUDE.md`, follow `CLAUDE.md`.

## 2. What you are building (one line)
A provably-correct streaming platform whose novelty is **context-conditioned detection**:
the reconciliation layer's pipeline-health signal (plus deploy markers) conditions the
detector so real anomalies are separated from pipeline artifacts. Real anomalies are
explained (VLM, flagged windows only) and routed to a safety-gated remediation agent.

## 3. Prime directives (non-negotiable)
- **Depth over breadth.** The context-conditioned detection core is the one hard thing; build
  it deeply. VLM and agent are thin and isolated. **Do not build the v2 items** (alert
  correlation/flooding, drift/staleness) — they are deferred by ADR-008.
- **Correctness proven, not claimed.** The reconciliation harness + chaos results are the
  point. Scope every guarantee to where it holds.
- **Numbers everywhere.** Latency, throughput, the scaling curve, false-positive reduction —
  measure and record. Never assert a number you didn't measure.
- **Failure-first.** Blast radius, backpressure, degradation, recovery — designed and tested.
- **Document as you go.** For every non-trivial decision, append an ADR to `docs/DECISIONS.md`
  (what, why, alternatives, cost). Keep `docs/EVALUATION.md`, `docs/CORRECTNESS.md`,
  `docs/CHAOS.md`, `docs/SCALE.md` current. A human must be able to read the repo and defend
  every choice in an interview — write for that reader.
- **Code standards** (from `CLAUDE.md`): no emoji; comment the *why*; domain-specific names;
  small reviewable commits; secrets via `.env` only, never hardcoded; tests for every component.

## 4. Execution protocol (your loop, every phase)
For each phase in §7, run this loop:
1. **Plan.** Restate the phase goal and its acceptance gate. List the tasks. Identify choices
   with real tradeoffs.
2. **Decide + record.** For each real choice, reason through the alternatives briefly and
   append an ADR. Prefer the option already specified in the docs; deviate only with a recorded
   reason.
3. **Implement** in small commits, each with a real message.
4. **Test.** Write unit/integration/correctness tests as appropriate; run them.
5. **Self-verify** against the phase's acceptance gate. If it fails, fix and repeat — do not
   advance on a failing gate.
6. **Document.** Update the relevant `docs/*` and record measured numbers.
7. **Regression-check.** Re-run the full test suite before moving to the next phase.
Only when a phase's gate passes and the suite is green do you proceed.

## 5. Environment & how to run
- Local / laptop. Start infra with `docker compose up -d` (Kafka in KRaft + Postgres are
  scaffolded in `docker-compose.yml`); add services (ClickHouse, Iceberg, etc.) as phases need
  them. `loadgen.py` is the synthetic source and the throughput harness.
- Python 3.12. Externalize all config via `.env` (copy from `.env.example`, which you create).
- Heavy model serving (vLLM) runs one phase at a time; the VLM may use a hosted endpoint via an
  env var. Use a small/quantized foundation-model detector so it runs on a laptop.

## 6. Blocker protocol (stay autonomous)
When a step needs something you don't have, **do not hard-stop unless you truly cannot proceed
safely.** Instead:
- **Missing external resource** (live feed not chosen, model weights, cloud creds): take the
  sensible default already specified — use the synthetic `loadgen` as the source; use a small
  local/quantized model or a hosted endpoint behind an env var; run single-node. Record the
  assumption in an ADR and continue.
- **A component that can't run on the laptop yet:** implement it behind a clean interface, add a
  minimal working stub that satisfies the contract and tests, note it in `docs/BLOCKERS.md`, and
  continue. Never fake outputs to pass a test — a stub is declared as a stub.
- **A genuine hard blocker** (e.g. a required secret with no safe default, or a decision only a
  human should make): stop that thread only, write the exact question and options to
  `docs/BLOCKERS.md`, continue with any other unblocked work, and surface the blocker clearly at
  the end. Keep making progress everywhere you can.

## 7. Build phases (each has a gate — see §4)
Sprint mapping and acceptance criteria live in `docs/USER_STORIES.md`; NFR targets in
`docs/REQUIREMENTS.md`. Summary and per-phase gates:

- **Phase 1 — Thin spine.** loadgen → Kafka → consumer → windowing → z-score baseline →
  episodes in Postgres; then add the zero-shot foundation-model detector; minimal dashboard.
  *Gate:* runs end to end from documented commands; an anomaly appears; the foundation model
  runs alongside the baseline; tests pass.
- **Phase 2 — Correctness + resilience.** Flink (event-time, watermarks, RocksDB, 2PC
  exactly-once); the reconciliation harness (per-stage invariants) emitting the pipeline-health
  signal; the chaos suite; the throughput/scale harness.
  *Gate:* reconciliation drift = 0 over a long run; ≥3 injected faults recover to a consistent
  state with bounded lag; a throughput-vs-parallelism curve is produced and recorded.
- **Phase 3 — The core contribution.** Context-conditioned detection: consume the health
  signal + deploy markers through the pluggable `ContextSignalSource` interface; conditioning
  policy attributes anomalies overlapping a disturbance; fail-open when the signal is missing
  (ADR-007). Add ClickHouse (serving) + Iceberg (source of truth).
  *Gate:* **measured false-positive reduction vs. the unconditioned baseline** under injected
  pipeline + deploy events, recorded in `docs/EVALUATION.md`; fail-open verified.
- **Phase 4 — Explanation + agent (thin).** VLM explanation on flagged windows only; MCP tools;
  Diagnoser → Planner → Safety Gate → Executor on a sandbox; runbook RAG; model on vLLM.
  *Gate:* a flagged anomaly gets an explanation; agent proposes → gate approves/rejects →
  sandbox executes; full trace persisted.
- **Phase 5 — Evaluation + CI.** Honest detection benchmark (incl. where it loses) on a labeled
  set; optional QLoRA fine-tune of the tool-calling model; Ragas/DeepEval/TruLens; CI quality
  gate.
  *Gate:* benchmark results (with losses) documented; DeepEval gate fails the build on a
  quality regression.
- **Phase 6 — Production wrapper + polish.** Docker/K8s/Terraform hardening; GitHub Actions;
  observability across services; README with the architecture diagram and a <60s demo.
  *Gate:* one-command bring-up; README + diagram + demo present.

## 8. Final Acceptance Checklist (the definition of "done")
The project is complete only when ALL pass:
- [ ] End-to-end demo runs from a single documented command and survives a live-injected fault.
- [ ] Reconciliation shows zero drift across a multi-hour run.
- [ ] Chaos suite: ≥3 fault modes, each recovering to a verified consistent state with bounded lag.
- [ ] Measured false-positive reduction under injected pipeline + deploy events vs. the
      unconditioned baseline, with the delta recorded honestly (including any regressions).
- [ ] Throughput measured with hardware noted; throughput-vs-parallelism curve produced.
- [ ] Detection benchmarked against the baseline on a labeled set, results (incl. losses) documented.
- [ ] Every flagged incident carries an explanation; every agent action passed the safety gate;
      actions ran only in the sandbox.
- [ ] CI green, including the DeepEval quality gate.
- [ ] All `docs/*` current: ADRs for real decisions, EVALUATION/CORRECTNESS/CHAOS/SCALE filled,
      README with diagram + demo.
- [ ] `docs/BLOCKERS.md` lists anything deferred or needing a human, or states "none".

## 9. Honesty rules (hold these throughout)
- Measure; report losses; scope claims to where they hold.
- Never fabricate numbers or fake tool/command outputs. A stub is labeled a stub.
- Cite prior art where the docs say to (e.g., VLM4TS for the flagged-window VLM pattern).
- Keep the scope: v1 core deep; v2 items stay unbuilt and documented as roadmap.

## 10. What "finished" looks like
A running system + a green test suite + measured results + complete docs + a README with the
architecture diagram and a short demo — and a `docs/BLOCKERS.md` that either says "none" or
clearly lists what needs a human. Then stop and summarize what was built, the key ADRs, the
measured numbers, and any blockers.

---
*Give this file plus the `docs/` set to Claude Code to run the build.*
