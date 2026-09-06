# Vigil

Real-time anomaly detection on a streaming backbone, where the detector is conditioned on
operational context -- pipeline health and deploy markers -- so that genuine incidents are
separated from artifacts of the pipeline itself.

**Status: Phase 1 of 6 complete.** The detection spine runs end to end. Phases 2-6
(Flink and exactly-once, the reconciliation harness and the conditioning core, ClickHouse
and Iceberg, the VLM explainer and remediation agent, evaluation and CI) are not built.
This README describes what exists, not what is planned; `BUILD.md` has the roadmap.

---

## Results so far

Measured on the reference laptop: Intel Core i7-12650H (10 cores / 16 threads), 15.6 GB RAM,
Windows 11, Docker Desktop with 16 CPU / 8.1 GB to the VM, Python 3.12.10. Every figure
below comes from a command recorded in `docs/EVALUATION.md`.

| | |
|---|---|
| Ingest throughput, single process, blast mode | **76,556 events/s** (NFR-4 target: 20,000) |
| Consumer throughput, replaying a filled topic | **43,160 readings/s** |
| Hot-path detection latency, p99 | **0.49 ms** (NFR-1 budget: 250 ms) |
| Foundation model, off critical path, p99 | 34.8 ms amortised per window |
| End-to-end gate run | 252,000 readings produced, **252,000 consumed**, 0 late, 0 dropped |
| Live feed | 1,387 MQTT messages -> 11,089 readings across 336 channels in 45 s, 0 gaps |
| Tests | **202 passing** (unit, plus integration against real Kafka and Postgres) |

Two things these numbers are **not**: the producer and consumer figures were taken
separately, so neither is an end-to-end throughput claim; and the gate run is a single
seven-minute observation, not the zero-drift proof, which needs the Phase 2 reconciliation
harness. See `docs/CORRECTNESS.md` for where each guarantee starts and stops.

![The Phase 1 dashboard: KPI tiles for episode count and per-detector latency against the
250 ms hot-path budget, a bar comparison of episodes raised by each detector, and a table of
detected episodes with channel, detector, peak score, duration and ground-truth
labels.](docs/images/dashboard-light.png)

---

## What it does

A high-velocity stream of unlabelled sensor telemetry is ingested into Kafka, assigned to
event-time sliding windows per channel, and scored by two detectors at once:

- a **rolling z-score baseline** on the hot path -- cheap, well understood, and the
  permanent bar every later claim is measured against;
- a **zero-shot time-series foundation model** (Chronos-Bolt) running batched off the
  critical path, scoring windows by forecast residual against its own predicted uncertainty.

Consecutive flagged windows on a channel are merged into a single **episode**, because an
operator is paged once per incident, and stored in Postgres.

The part that is the actual contribution -- conditioning those detections on pipeline-health
signals and deploy markers, so an artifact is attributed to its cause instead of paged -- is
Phase 3. What exists today is the wire it runs on: a dedicated context topic, the marker
schema, and a load generator built so that the eventual claim can be *disproved*.

### The generator is adversarial on purpose

The easy way to report a large false-positive reduction is to mute everything during a
deploy window, and a naive evaluation cannot tell that apart from correct attribution. So
`loadgen.py --scenario` schedules four populations deliberately: deploys that perturb
telemetry, deploys that perturb nothing, real faults outside every window, and real faults
*inside* windows including the quiet ones. That last group is the trap -- during a quiet
deploy there is no artifact at all, so anything suppressed there could only be blanket
muting. Deploys touch a subset of channels, so a policy ignoring scope over-suppresses and
gets caught too.

The success criterion is a **pair**, never a single number: at least 40% false-positive
reduction **and** approximately zero recall loss, measured against a read-only shadow pass
over byte-identical data (ADR-016).

---

## Run it

Prerequisites: Docker, Python 3.12.

```bash
cp .env.example .env          # then fill in POSTGRES_PASSWORD and KAFKA_CLUSTER_ID
python -c "import base64,uuid;print(base64.urlsafe_b64encode(uuid.uuid4().bytes).rstrip(b'=').decode())"

docker compose up -d --wait   # Kafka in KRaft mode + Postgres, topics created explicitly

python -m venv .venv && .venv/Scripts/pip install -e ".[dev,foundation]"
```

Nothing has a default. Compose refuses to start on an unset variable and the config loader
raises with the variable's name, so a half-filled `.env` fails loudly instead of quietly
connecting somewhere unintended.

Then, in three terminals:

```bash
# 1. the detection spine
python detector.py --from-beginning

# 2. a source -- either the synthetic harness...
python loadgen.py --rate 600 --duration 420 --scenario --write-plan docs/results/plan.json

#    ...or the live public solar-fleet feed
python mqtt_bridge.py --topic inverters

# 3. the dashboard, at http://127.0.0.1:8000
python -m uvicorn vigil.api:app --port 8000
```

Measure throughput instead:

```bash
python loadgen.py --rate 0 --duration 30 --channels 32     # blast mode, reports events/s
```

Tests:

```bash
python -m pytest                       # all 202, needs docker compose up for the integration ones
python -m pytest -m "not integration"  # unit only
```

---

## Data

Two sources for two jobs, because a live stream has no answer key.

**Live (the showcase).** The public TDengine solar-fleet MQTT feed,
`mqtt.tdengine.com:1883`, anonymous, QoS 0. Chosen over a market-data stream for one reason:
it publishes *expected* alongside *actual* power, so detection runs on a residual with
physical meaning rather than an arbitrary threshold -- and it carries the real operational
context (curtailment, soiling, alarms, weather) the conditioning layer will consume.

Detection runs on that residual, not on raw output. Generation collapses every evening
across the whole fleet, so a value-only detector on raw power reads sunset as a fleet-wide
incident; the residual sits near zero at noon and near zero at midnight. Raw power is still
published as its own channel deliberately, as the control that shows what the unconditioned
detector does at sunset.

**Labelled (the proof).** TSB-AD-M: 200 labelled multivariate series, fetched on demand by
`python scripts/fetch_tsb_ad.py`. Scored with threshold-independent, time-aware measures --
**never point-adjusted F1**, which is the specific inflation TSB-AD exists to expose
(ADR-013). Our numbers will therefore look worse than papers that point-adjust, and are not
comparable to them.

---

## Documentation

| File | What it holds |
|---|---|
| `docs/PROGRESS.md` | Status board, work log, error log, and the exact next action |
| `docs/DECISIONS.md` | 17 ADRs -- what was decided, what was rejected, what it cost |
| `docs/CORRECTNESS.md` | The guarantee at each boundary, and what it does not cover |
| `docs/EVALUATION.md` | Methodology, measured numbers, and the metrics deliberately refused |
| `docs/BLOCKERS.md` | Anything deferred, stubbed, or needing a human |
| `docs/ARCHITECTURE.md` | Design and the core mechanism |
| `docs/PROJECT_PLAN.md` | Full spec, stack, phases, and the prior-art review |

---

## Honesty rules held throughout

Every number states the hardware and the command that produced it. A target that is missed
is reported as missed. Losses are reported as prominently as wins. "Not yet measured" is
used rather than an estimate, and a component that is not built is absent rather than
stubbed to look present -- which is why the dashboard has no reconciliation panel yet and
says so.
