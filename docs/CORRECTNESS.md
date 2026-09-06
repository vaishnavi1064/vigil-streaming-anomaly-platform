# Correctness

> Every guarantee here is scoped to exactly where it holds, and says what it does **not**
> cover. A claim appears in this file only once something tests it, and the gaps in section 6
> are as much the point as the guarantees in section 1.

---

## 1. The guarantee, stage by stage

| Boundary | Guarantee | Why it is not stronger | Status |
|---|---|---|---|
| Public MQTT feed -> bridge | **at-most-once** | QoS 0, and the feed publishes no history API, so a message lost between the broker and us cannot be detected as missing nor recovered (ADR-011) | Built |
| Synthetic generator -> Kafka | **exactly-once into the log** | Producer idempotence is on, so a retry cannot duplicate a record | Built |
| Kafka -> detector | **at-least-once** | Offsets commit after episodes are durable, so a crash replays rather than loses | Built |
| Detector -> Postgres | **effectively-once** | The sink upserts on `(channel, t_start_ms, raised_by)`, so a replay re-derives the same rows instead of duplicating them | Built |
| Inside Flink | **exactly-once** via 2PC: source offsets in the checkpoint, sink writes in a Kafka transaction committed on checkpoint completion, stable transactional-id prefix for epoch fencing | — | Built, running |
| Reconciliation drift = 0 over a multi-hour run | — | — | **Not yet run.** Measured at zero over a 15-minute run; the >= 4-hour soak NFR-6 asks for has not been done |

The end-to-end claim today is therefore: **at-most-once at the edge, exactly-once into the
Kafka log, exactly-once through the Flink job, effectively-once at the episode sink.** It is
*not* "exactly-once end to end" -- the edge is weaker and is stated as such -- and the
zero-drift claim is scoped to the durations actually measured, which is minutes, not hours.

---

## 2. What identity means here

Counting messages cannot distinguish "processed a million events" from "processed one event
a million times". So every reading carries a **per-channel monotonic sequence number**
assigned at the ingestion boundary. Downstream, a missing number is a gap and a repeated
number is a duplicate. That is the identity the reconciliation harness checks
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
rather than only observable in a running pipeline, and it is what let the same semantics be
handed to Flink and the two compared directly (section 3a).

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

## 3a. The Flink job, and what it changes

`flink/scoring_job.py` runs the same detection the Python consumer does, with a genuinely
stronger guarantee. The chain has three links and all three must hold or it is not a chain:

1. The Kafka source reads `isolation.level=read_committed`, so it never sees records from
   transactions that were later aborted.
2. Source offsets live **in Flink's checkpoint**, not in a consumer group. A restart rewinds
   to the checkpoint rather than to whatever was last committed out of band.
3. The sink writes inside a Kafka transaction that commits when the checkpoint completes.
   The two are the same two-phase commit: a failure between them replays from the checkpoint
   and aborts the uncommitted transaction, so a `read_committed` consumer sees each window
   score exactly once.

The transactional-id prefix is stable across restarts on purpose. Kafka fences zombie
producers by epoch, and it is that prefix that lets a restarted job reclaim and abort the
transactions its previous incarnation left open, rather than leaving them to block consumers
until they time out. The transaction timeout is set to 900 s against a 10 s checkpoint
interval, because a transaction that expires before the checkpoint that would have committed
it is **silent data loss**, not an error.

State is RocksDB with incremental checkpoints. Per-channel detector state is one entry per
channel held for the life of the job, so heap state would make the job's memory a function
of fleet size; RocksDB keeps it a function of the working set. Checkpoints are unaligned, so
barriers do not wait behind backpressured buffers -- which is what keeps them completing
under exactly the load the scale and chaos harnesses generate.

### Did the migration preserve the semantics?

Asked two ways, because either alone is insufficient.

**The arithmetic** (`tests/test_flink_parity.py`, 14 tests). The job's scoring rule is
re-implemented from its source and compared against `vigil.detectors.zscore` on identical
input; they agree to 1e-9. The tests also read the job's source and fail if the constants
drift apart, so the two cannot silently diverge. This proves transcription, not execution.

**The execution** (`flink_parity.py`). Both were run over the same Kafka topic and their
outputs joined on `(channel, window_start_ms)`:

| | |
|---|---|
| Windows scored by both | **1,056** |
| Agreement within +/-0.5 | **1,056 / 1,056 (100%)** |
| Median absolute difference | **0.0000** |
| Maximum absolute difference | **0.0000** |
| Signed bias (Flink - Python) | **+0.0000** |
| Windows only Flink scored | 0 |
| Windows only Python scored | 36 |

The scores are identical, not merely close. The 36 windows only Python scored are the tail
of the run, which Flink's watermark had not yet closed when the comparison was taken -- the
expected difference between a job still running and a replay that ran to completion, and the
reason the comparison is over the intersection rather than the union.

Checkpointing over the same run: **30 completed, 2 failed**, average end-to-end duration
283 ms, maximum 2,395 ms, average state size 7.99 MB. The two failures were during job
startup, before the first successful checkpoint.

**What this does not prove.** The comparison was taken from a job that had not been killed.
A checkpoint-recovery scenario -- kill the TaskManager mid-checkpoint, verify the
uncommitted transaction is aborted and replayed, and re-run this comparison afterwards -- is
the test that would actually exercise two-phase commit. It is **not yet run**, and is listed
in `docs/CHAOS.md` section 5 as a gap.

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

- **The Flink exactly-once path has not been fault-tested.** The job runs and its output is
  bit-identical to the reference implementation, but no scenario has yet killed it
  mid-checkpoint to verify the transaction is aborted and replayed. Until that runs, the
  two-phase-commit claim rests on configuration and on Flink's own guarantees, not on
  evidence from this deployment.
- **Zero drift is measured over minutes, not hours.** NFR-6 asks for >= 4 hours. The longest
  clean run so far is 15 minutes (360,000 readings, drift 0, offset drift 0).
- The reconciliation harness audits the Python consumer's view of the log. It does not yet
  audit the Flink job's output topic against its input.
- Edge loss on the live feed is detectable but not repairable (ADR-011). The synthetic
  source has perfect sequence integrity by construction, which is why every correctness and
  chaos test runs against it rather than against the public feed.
