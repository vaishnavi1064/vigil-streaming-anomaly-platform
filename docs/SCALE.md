# Scale: throughput against parallelism

What the consumer side of the platform does as consumers are added, where it stops scaling,
and what binds it there. NFR-4 asks for 20,000 events/s sustained; NFR-5 asks for the curve
and for the plateau to be *named* rather than assumed.

**Reference hardware.** Intel Core i7-12650H (10 cores / 16 threads), 15.6 GB RAM, Windows
11, Docker Desktop with 8.1 GB to the VM. Single-node Kafka in KRaft mode, replication
factor 1, 6 partitions. Every consumer is a full `detector.py` process -- windowing,
z-score scoring, episode merging and Postgres writes -- not a counting loop.

## 1. Method

    python scale.py --fill 300000 --parallelism 1,2,3,4,6,8 --report-json docs/results/scale-sweep.json

Two rules, because breaking either produces a curve flattering to the system:

- **Pre-fill, then drain.** Running a producer against the consumers measures whichever is
  slower. Each sweep point instead drains a backlog that is already on the broker, so the
  number is the consumers' own rate.
- **The same backlog every time.** Each point replays the identical topic from offset zero
  in a fresh consumer group. The points differ only in consumer count.

The idle timeout that ends a drain (10 s) is subtracted from every point, since every
configuration pays it and it would otherwise penalise the fastest.

**The backlog was 3,928,127 readings, not the 300,000 the command asks for.** In blast mode
the harness had no rate to divide into a count and fell back to producing for 30 seconds,
which at blast speed is 3.9 million. The measurement is sound -- every point drained the same
3,928,127 records, and a larger backlog means a longer steady state -- but the flag did not
mean what it said. `loadgen.py` now takes `--max-readings` and the harness passes it, so the
next sweep will fill the amount requested.

## 2. The curve

| Consumers | Partitions held | Throughput | Speedup | Efficiency | Per consumer |
|---|---|---|---|---|---|
| 1 | 6 | 94,495/s | 1.00x | 100% | 94,495/s |
| 2 | 3 each | 124,334/s | 1.32x | 66% | 62,167/s |
| 3 | 2 each | **167,775/s** | 1.77x | 59% | 55,925/s |
| 4 | 2,2,1,1 | 159,827/s | 1.69x | 42% | 39,957/s |
| 6 | 1 each | **170,414/s** | 1.80x | 30% | 28,402/s |
| 8 (2 idle) | 1 each, 2 idle | 170,126/s | 1.80x | 22% | 21,266/s |

```
   1 | ######################                       94,495/s
   2 | #############################               124,334/s
   3 | #######################################     167,775/s
   4 | #####################################       159,827/s
   6 | ########################################    170,414/s
   8 | #######################################     170,126/s
```

**NFR-4 is met with room to spare**: a single consumer sustains 94,495 readings/s against a
20,000 target, and six sustain 170,414/s.

**NFR-5 is not met as stated.** "Near-linear up to core saturation" is not what happened.
Scaling is sub-linear from the second consumer onward and effectively finished by the third:
6 consumers buy 1.80x, not 6x, and efficiency falls from 100% to 30% across the sweep.

## 3. Where it stops, and what binds it

The plateau is **~170,000 readings/s, reached at 3 consumers** -- half the partition count.
Partitions are therefore *not* the binding constraint; if they were, throughput would have
kept climbing to 6.

What is left, on this hardware, in rough order of likelihood:

1. **The broker and the consumers share ten cores.** Kafka runs in the Docker VM on the same
   machine as the consumers. Past three consumers, adding a process takes CPU from the broker
   that is feeding it. This is a property of running the whole platform on one laptop and it
   is the first thing that would change on real hardware.
2. **One broker, one disk, replication factor 1.** All six partitions are served by one
   process reading one log directory. There is no second broker to spread fetch load across.
3. **Per-consumer Postgres writes.** Every consumer holds its own connection and writes
   episodes as they close. Not measured separately here, and it is a candidate rather than a
   finding.

Beyond 6 consumers the cause is not a mystery: Kafka assigns whole partitions, so consumers
7 and 8 are assigned nothing and sit idle. Throughput is flat across that step (170,414 to
170,126) exactly as it should be. The sweep deliberately runs past the partition count so
this is measured rather than asserted.

**The dip at 4 consumers is unexplained, and the obvious explanation is untested.** Six
partitions over four consumers assigns 2, 2, 1, 1 -- and a drain ends when the *slowest*
consumer finishes, so the two double-partition consumers set the pace while the other two
idle. That would predict exactly this shape, but the harness records only the total, not the
per-consumer spread, so the hypothesis has not been checked. Recording per-consumer elapsed
time would settle it (gap G-9).

## 4. What this measurement does not establish

- **It is consumer-side only.** The producer figure (76,556 events/s single-process blast,
  `docs/EVALUATION.md`) was taken separately. Neither is an end-to-end throughput claim.
- **It does not project to a cluster.** One broker, one machine, replication factor 1. A
  three-broker cluster would move the plateau, and nothing here predicts where.
- **The workload is plain synthetic telemetry**, 16 channels, no injected anomalies. The
  paired-evaluation runs drain scenario data at around 15,500 readings/s -- an order of
  magnitude lower -- because that stream is dense with excursions and produces far more
  flagged windows, episode merges and Postgres writes per reading. Neither number is wrong;
  they are different workloads, and the honest headline is the one that names its workload.
- **The foundation model was off** (`--no-foundation-model`), per ADR-017: it runs batched
  off the critical path, so including it would measure the batching worker rather than the
  spine.

## 5. A side effect worth recording: consumption is at-least-once

Every multi-consumer point read **more** readings than were produced:

| Consumers | Read against 3,928,127 produced |
|---|---|
| 1 | exactly 3,928,127 |
| 2 | +2,100 |
| 3 | +1,460 |
| 4 | +809 |
| 6 | +702 |
| 8 | +303 |

A single consumer never rebalances and reads each record once. Add members and the group
rebalances during the drain; records fetched but not yet committed are re-delivered to the
new owner and processed twice. That is Kafka's consumer contract working as designed, and it
is a direct, measured statement of why the Python detector path is **at-least-once** and why
the exactly-once claim in `docs/CORRECTNESS.md` is scoped to the Flink job's two-phase
commit rather than to this path. Episode writes are idempotent, so duplicate *processing*
does not become duplicate episodes -- but duplicate processing is real and it is measurable
here.

---
*Raw results: `docs/results/scale-sweep.json`.*
