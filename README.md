# Vigil

Real-time anomaly detection on a streaming backbone, where the detector is conditioned on
operational context -- pipeline health and deploy markers -- so that genuine incidents are
separated from artifacts of the pipeline itself.

**Status: phases 1-4 built, phase 3's target missed and published as missed.** The
detection spine, the correctness backbone (Flink with two-phase commit, the reconciliation
harness, a chaos suite, the scaling curve) and the safety-gated agent all run. The
conditioning core -- the actual contribution -- has been measured twice against its target
and missed it twice; both results and the diagnosis are in `docs/EVALUATION.md` section 3.4
rather than filed away. ClickHouse, Iceberg, the VLM explainer and the production wrapper
are not built. This README describes what exists, not what is planned; `BUILD.md` has the
roadmap and `docs/BLOCKERS.md` has what is waiting on a human.

---

## Results so far

Measured on the reference laptop: Intel Core i7-12650H (10 cores / 16 threads), 15.6 GB RAM,
Windows 11, Docker Desktop with 16 CPU / 8.1 GB to the VM, Python 3.12.10. Every figure
below comes from a command recorded in `docs/EVALUATION.md`.

| | |
|---|---|
| Ingest throughput, single process, blast mode | **76,556 events/s** (NFR-4 target: 20,000) |
| Consumer throughput, one process draining a 3.9 M backlog | **94,495 readings/s** |
| Consumer throughput, plateau | **170,414 readings/s** at 3-6 consumers over 6 partitions |
| Scaling | 1.80x at six consumers, efficiency 30%. **NFR-5's near-linear claim: not met** |
| Hot-path detection latency, p99 | **0.49 ms** (NFR-1 budget: 250 ms) |
| Foundation model, off critical path, p99 | 34.8 ms amortised per window |
| Reconciliation drift | **0** over 360,000 readings, verified by independent broker-offset audit |
| Chaos | **5/5** fault modes recovered to a verified consistent state, each proven to have disrupted something |
| Flink parity | 1,056 windows scored, differences of exactly **0.0000** against the Python detector, 30 checkpoints at 283 ms average |
| Flink exactly-once, **across a crash** | TaskManager SIGKILLed mid-checkpoint: restored from checkpoint 5, recovered in **14.9 s**, and a `read_committed` consumer saw **328 window scores with 0 duplicates** |
| Detection benchmark, 144 labelled series | z-score median AUC-PR **0.198** against Chronos-Bolt-tiny **0.152**, 78 wins to 56, at **141x less compute**. The foundation model loses |
| Agent quality gate | 400 episodes, 1,267 actions: **100%** grounded, gate-approved and sandboxed; 97.1% carrying a runbook citation |
| Context conditioning vs. the unconditioned baseline | +9.0% false-page reduction against a 40% target, -10.0% recall against a 5% tolerance. **NFR-8: not met, twice** |
| Live feed | 1,387 MQTT messages -> 11,089 readings across 336 channels in 45 s, 0 gaps |
| Tests | **524** (unit, plus integration against real Kafka, Postgres and a live Flink cluster) |

What these numbers are **not**. The producer and consumer figures were taken separately, so
neither is an end-to-end throughput claim. Zero drift is measured over 15 minutes, not the
four hours NFR-6 asks for. The exactly-once evidence is one kill of one job on one
TaskManager, not an exhaustive test. The benchmark scored 144 of 200 series -- truncation
excluded the late-onset half -- and used Chronos-Bolt at its smallest size. And the
conditioning result is a **failure, published as one**: the second attempt turned out to be
testing something other than what it claimed, which is written up in full rather than retried
until it passed. `docs/CORRECTNESS.md` says where each guarantee starts and stops;
`docs/BLOCKERS.md` lists every gap of this kind in one table.

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

python -m venv .venv && .venv/Scripts/pip install -e ".[dev,foundation,explain]"
```

Nothing has a default. Compose refuses to start on an unset variable and the config loader
raises with the variable's name, so a half-filled `.env` fails loudly instead of quietly
connecting somewhere unintended.

One command, end to end, with a fault injected while it runs:

```bash
python demo.py            # ~90 s: produce, pause the broker mid-stream, reconcile, detect, remediate
```

It produces a labelled scenario with deploy markers, **pauses the Kafka broker for 8 seconds
while the producer is running**, reconciles the stream against the broker's own offsets,
detects with conditioning on, and hands the sharpest episode to the safety-gated agent. Then
it prints each claim and whether it held, and exits non-zero if any did not. A recent run:

```
  [ok] produced: delivered 48,002  failed 0
  [ok] reconciliation reports zero drift: 48,000 readings across 8 channels | drift 0
  [ok] broker offset audit agrees: 48,000 retained | consumed 48,000 | offset drift +0
  [ok] a fault was injected while the stream ran: broker paused for 8s mid-production
  [ok] episodes were raised: 8 episodes, sharpest peak 74.5 on pump-00.vibration_mm_s
  [ok] conditioning recorded a verdict for every episode: 8 decided against 8 episodes
  [ok] every agent action passed the safety gate: 4 actions on episode 4
  7/7 claims held
```

It runs in its own Postgres schema and drops it afterwards, so it neither reads nor deletes
anything else. `--keep` leaves the schema behind and prints how to point the dashboard at it.

Or drive the pieces yourself, in three terminals:

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

Reproduce the measurements. Each writes its raw result under `docs/results/` and each
number in this README came from one of them:

```bash
# producer throughput, blast mode
python loadgen.py --rate 0 --duration 30 --channels 32

# consumer throughput against parallelism -- the curve in docs/SCALE.md
python scale.py --fill 300000 --parallelism 1,2,3,4,6,8 --report-json docs/results/scale-sweep.json

# reconciliation: per-channel sequence identity plus an independent broker-offset audit
python reconciler.py --from-beginning --stop-after-idle-s 12

# the chaos suite: four fault modes, each required to prove it disrupted something
python chaos.py --all --report-json docs/results/chaos.json

# the paired evaluation: shadow, conditioned and fail-open passes over byte-identical data
python evaluate.py --duration 900 --rate 400 --channels 12     --deploys-per-hour 60 --faults-per-hour 120     --report-json docs/results/paired.json

# the labelled benchmark, detector against baseline
python benchmark.py --report-json docs/results/benchmark.json
```

Tests:

```bash
python -m pytest                       # all 524, needs docker compose up for the integration ones
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
| `docs/DECISIONS.md` | 31 ADRs -- what was decided, what was rejected, what it cost |
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
