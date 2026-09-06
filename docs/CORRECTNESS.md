# Correctness

> Every guarantee here is scoped to exactly where it holds, and says what it does **not**
> cover. A claim appears in this file only once something tests it. Phase 2 adds Flink,
> true two-phase-commit exactly-once, and the reconciliation harness; until then this
> document describes a narrower system honestly rather than describing the intended one.

---

## 1. The guarantee, stage by stage

| Boundary | Guarantee | Why it is not stronger | Status |
|---|---|---|---|
| Public MQTT feed -> bridge | **at-most-once** | QoS 0, and the feed publishes no history API, so a message lost between the broker and us cannot be detected as missing nor recovered (ADR-011) | Built |
| Synthetic generator -> Kafka | **exactly-once into the log** | Producer idempotence is on, so a retry cannot duplicate a record | Built |
| Kafka -> detector | **at-least-once** | Offsets commit after episodes are durable, so a crash replays rather than loses | Built |
| Detector -> Postgres | **effectively-once** | The sink upserts on `(channel, t_start_ms, raised_by)`, so a replay re-derives the same rows instead of duplicating them | Built |
| Inside Flink | exactly-once via 2PC + epoch fencing | — | **Phase 2, not built** |
| Reconciliation drift = 0 over a multi-hour run | — | — | **Phase 2, not built** |

The end-to-end claim today is therefore: **at-most-once at the edge, exactly-once into the
Kafka log, effectively-once at the episode sink.** It is *not* "exactly-once end to end",
and it is not a zero-drift claim.

---

## 2. What identity means here

Counting messages cannot distinguish "processed a million events" from "processed one event
a million times". So every reading carries a **per-channel monotonic sequence number**
assigned at the ingestion boundary. Downstream, a missing number is a gap and a repeated
number is a duplicate. That is the identity the Phase 2 reconciliation harness will check
per-stage invariants against, rather than comparing row counts.

Readings are keyed by channel on the Kafka partitioner, so all of a channel's readings land
in one partition. That preserves per-channel sequence order and lets the harness check
invariants partition-locally instead of globally sorting a stream.

Because the sequence is assigned *at* the boundary, anything lost upstream of it is by
construction invisible to it. That is precisely why the edge guarantee is stated separately
and narrowly.

---

## 3. Event time, watermarks and lateness

The windower is pure and clock-free: everything it decides is a function of the event
timestamps it is handed. That is what makes the out-of-order behaviour testable directly
rather than only observable in a running pipeline, and it is what lets Phase 2 hand the same
semantics to Flink and compare the two.

- **Watermark** = highest event time seen minus the allowed lateness, tracked **per
  channel**. Per channel because these are independent devices: one inverter falling silent
  must not stall the watermark of the whole fleet. The cost is that a channel which stops
  publishing leaves its last windows open until shutdown; the ingest gap watch is what
  notices the silence.
- **A window closes** when the watermark passes its end. It is emitted, scored, and never
  reopened.
- **A late event** -- one behind the watermark -- is counted and dropped. It is not folded
  into a window that has already been scored. A second, different score for a window the
  detector has already acted on is exactly the quiet inconsistency reconciliation exists to
  catch, so admitting late data silently would undermine the thing being built.
- **A thin window** (fewer than `min_points` samples) is dropped rather than scored.
  Two or three samples cannot support a statement about dispersion.

Measured on the Phase 1 gate run (252,000 readings, 7 minutes): **0 late readings, 0 thin
windows dropped, 360 windows emitted.**

---

## 4. Why the sink must be idempotent

The detector commits Kafka offsets only after the episodes derived from those windows are
durably written. Committing first would mean a crash in between loses episodes the platform
has already claimed to have found.

That ordering makes replay normal rather than exceptional, which in turn makes an idempotent
sink mandatory. The unique constraint `(channel, t_start_ms, raised_by)` is what carries it.
Without it, a restart would duplicate episodes, and a reconciliation harness reporting drift
afterwards would be reporting our own bookkeeping rather than a pipeline fault -- a false
alarm in the very component whose job is to be trustworthy.

The conflict resolution deliberately *widens* rather than skips: a replay can carry more
information than the first pass, because the off-critical-path model may have scored windows
the hot path had already raised on (ADR-017). An episode's end time and peak score only ever
move outward.

`raised_by` is part of the identity so the two detectors can raise on the same span
independently, which is what makes their disagreement visible instead of one silently
overwriting the other.

---

## 5. What is verified, and how

| Property | Verified by | Result |
|---|---|---|
| Window/lateness/watermark semantics | 19 unit tests in `tests/test_windows.py` | pass |
| Sequence identity and gap detection | 9 unit tests in `tests/test_ingest_source.py` | pass |
| Sink idempotence under replay | integration tests against real Postgres, `tests/test_episode_store.py` | pass |
| Attribution cannot dangle | foreign key to `context_events`, asserted by test | pass |
| Raw readings never reach Postgres | test asserts the schema contains exactly the four intended tables | pass |
| Produced count equals consumed count | Phase 1 gate run | 252,000 = 252,000 |

None of the above is the zero-drift claim. The gate run is a single-consumer, single-run
observation over seven minutes. **Zero drift over a multi-hour run requires the Phase 2
reconciliation harness and is not claimed here.**

---

## 6. Known gaps

- No Flink yet, so no checkpointing, no RocksDB state backend, and no true two-phase commit.
- No reconciliation harness, so no continuous proof and no pipeline-health signal -- which
  also means the core conditioning mechanism has only its deploy-marker input so far.
- No chaos suite, so recovery after a fault is untested.
- Edge loss on the live feed is detectable but not repairable (ADR-011). The synthetic
  source has perfect sequence integrity by construction, which is why every correctness and
  chaos test runs against it rather than against the public feed.
