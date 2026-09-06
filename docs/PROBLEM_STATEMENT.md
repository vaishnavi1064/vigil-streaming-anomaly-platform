# Problem Statement
### Real-Time Anomaly Detection & Self-Remediating Agent Platform
**SDLC phase:** Inception / Requirements — **v1.0** — status: agreed

> Companion to `docs/PROJECT_PLAN.md`. This is the first SDLC artifact; each subsequent
> phase (requirements/user stories, architecture & ADRs, sprints, test/eval, release)
> gets its own document.

---

## 1. Context
Operational systems — industrial sensors, market-data feeds, service metrics — emit
high-velocity, **unlabeled**, **time-sensitive** streams that teams must watch in real
time. Timely, trustworthy detection of genuine problems on these streams is the goal.

## 2. Problem
A real-time anomaly detector fires on things that are not real, actionable incidents,
because it sees only the **values** — never the operational **context** that would explain
them. The sharpest and costliest case: when the pipeline itself hiccups (broker death,
partition lag, backfill double-count), the data spikes or drops and the detector alarms
even though nothing in the world went wrong. The correctness layer that *knows* the
pipeline faltered is architecturally **siloed** from the detector that raises the alarm, so
that knowledge never reaches the decision.

## 3. Affected users / stakeholders
- **On-call / SRE and operations engineers** — act on the alerts; bear the noise.
- **Platform / data-platform team** — owns the pipeline and the trust placed in its signals.
- **(Downstream) the business** — pays for downtime when real incidents are missed in the noise.

## 4. Impact
Context-blind false alarms cause **alert fatigue**, **wasted remediation effort**, and
ultimately **missed real incidents** — because teams learn to ignore or switch off a noisy
detector. Lowered trust in detection is the terminal failure mode.

## 5. Why current approaches fall short
- Data-quality / reconciliation and anomaly detection are built as **separate checks** that
  never inform each other.
- Detectors are **value-only** (no operational context) and **one-way** (they detect; they
  do not diagnose or act).
- A genuinely live stream has **no labels** to train a supervised detector on.

## 6. Problem validation (practitioner + research evidence)
The problem is not hypothetical. Reported, recurring pains — the same root cause
(context-blind detection) across several triggers:

| Pain (validated) | Source type |
|---|---|
| Deploys trigger false-positive storms ("every deployment → 20+ false alerts for ~2h until things stabilize"); models flag the post-deploy new-normal as anomalous | Practitioner (HN / r/sre, via DevOps guides) |
| "Not all anomalies are failures" — benign-but-unusual behavior floods operators and lowers trust | Research (robotic AD open-problems; microservice AD survey) |
| Alert flooding — one outage produces repeated alerts that bury the critical one; teams want *incidents*, not raw alerts | Research/industry (cloud-outage diagnosis) |
| Value-only detectors trip on seasonality (daily cycles, holidays) | Practitioner (DevOps/observability guides) |
| Detectors tuned on last month go stale and miss novel failures | Practitioner (r/sre) |
| Explainability is tightly coupled to excluding false positives — operators need *why* to dismiss a false alarm | Research (microservice AD survey) |

*Insight:* pipeline artifacts are one **subclass** of a broader problem — the detector
fires on things it lacks the context to explain. That reframing defines both the core
contribution and the roadmap.

## 7. Proposed approach
**Context-conditioned detection.** Condition the detector on the operational context it
normally cannot see, so real events are separated from context-explained non-events:
- **Correctness signals from the reconciliation layer** (drift, lag, gap-fills, duplication)
  — the novel, *free-signal* core, uniquely enabled by owning both halves in one system.
- **Deploy / change markers** — the natural generalization of the same mechanism (same wire,
  one extra input).

This runs on a **provably-correct streaming backbone** (exactly-once + continuous
reconciliation), with flagged events **explained** (VLM on flagged windows only) and routed
to a **safety-gated remediation agent** — turning an anomaly into an action, not just a
dashboard blip.

## 8. Scope

### In scope — v1, solved deeply
- Context-conditioned false-positive suppression on **pipeline-artifact + deploy** signals.
- **Proven**, not claimed: inject pipeline/deploy faults (chaos suite) and measure the
  false-positive reduction versus an *unconditioned* baseline.
- Anomaly → action via the agent loop; seasonality largely absorbed by the foundation-model
  detector (demonstrated in the benchmark vs. a naive threshold).

### Partially addressed (by design choices, not a dedicated subsystem)
- **Anomaly ≠ actionable incident** — the diagnose → safety-gate → remediate loop moves from
  raw anomaly toward action.
- **Seasonality** — handled better by the zero-shot foundation-model detector than by a naive
  threshold; shown in the honest benchmark.

### Out of scope — v2 / future work (acknowledged, not built)
- **Alert correlation & flooding** — its own subsystem and a mature AIOps research/product area.
- **Concept-drift / staleness adaptation** — its own research problem.

**Scope-decision rationale (ADR-style).** *Decision:* solve context-conditioned detection
deeply; defer alert-correlation and drift-adaptation to v2. *Why:* the senior signal is one
hard thing proven, not five shallow ones; each deferred item is a deep problem whose shallow
version would invite failure under scrutiny. *Cost accepted:* the system does not de-duplicate
or self-adapt in v1. *Mitigation:* the conditioning mechanism is designed to be **extensible**
to additional signals, and the deploy-marker case demonstrates that generality — so the
roadmap is credible without building it now.

## 9. Success criteria (measurable — targets finalized in the requirements phase)
- [ ] Measurable **false-positive reduction** under injected pipeline + deploy events, vs. an
      unconditioned baseline (report the delta).
- [ ] **Zero reconciliation drift** across a multi-hour run.
- [ ] **Latency / throughput SLOs** met (budgets set in Phase 2), with a throughput-vs-
      parallelism curve.
- [ ] **Bounded recovery** to a consistent state after ≥3 injected failure modes.
- [ ] Each flagged incident carries an **explanation**; agent actions pass a deterministic
      safety gate.

## 10. Open questions → next SDLC step
- Choose the live data feed (deferred; development proceeds on the synthetic load generator).
- Set concrete SLO targets and the false-positive-reduction target.
- **Next artifact:** requirements / user stories + acceptance criteria.

---
*v1.0 — Inception artifact. Revise as later phases inform it.*
