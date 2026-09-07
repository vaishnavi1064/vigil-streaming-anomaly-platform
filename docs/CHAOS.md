# Chaos and recovery

> Every fault here interrupts a real process. A mocked broker outage proves the mock
> behaves; killing the container proves the pipeline does. Every result states how the
> disruption was verified, because the first version of this suite reported four passes of
> which three meant nothing.

**Reference hardware:** Intel Core i7-12650H (10 cores / 16 threads), 15.6 GB RAM, Windows 11,
Docker Desktop with 16 CPU / 8.1 GB to the VM.

---

## 1. What a pass means here

A scenario passes only if **all four** hold:

1. The fault was injected without error.
2. **The system was measurably unserviceable during the hold.** Serviceability is sampled
   once a second; a scenario where the system never once failed to answer is reported as a
   failure, whatever its drift figure says.
3. It returned to service within the NFR-7 budget of 60 s.
4. An **independent replay** of the whole log afterwards finds every channel's sequence
   still dense.

Point 2 exists because the first full run of this suite was green and meaningless. Point 4
is the one that matters most: recovery is not judged by the system's opinion of itself. A
separate process replays the log from offset zero and checks the identity invariant --
`readings == max_seq - min_seq + 1` per channel. The sequence numbers were assigned before
the fault and are checked after it, so loss or duplication has nowhere to hide. The
producer's idempotent delivery count closes the loop from the other end.

---

## 2. Results

`python chaos.py --all --duration 110 --settle-s 25 --hold-s 20`

Each scenario: 88,000 readings produced at 800 ev/s over 8 channels, fault held 20 s.

| Fault | What it does | Disruption observed | Recovery | Budget | Produced / consumed | Drift | Verdict |
|---|---|---|---|---|---|---|---|
| `broker-kill` | SIGKILL the Kafka container, then restart | **20/20 samples unserviceable** | **18.3 s** | 60 s | 88,000 / 88,000 | 0 | PASS |
| `broker-pause` | SIGSTOP the broker, sockets stay open, then resume | **20/20** | **0.1 s** | 60 s | 88,000 / 88,000 | 0 | PASS |
| `network-partition` | Detach the broker from the compose network, then reattach | **20/20** | **0.1 s** | 60 s | 88,000 / 88,000 | 0 | PASS |
| `consumer-kill` | SIGKILL the detector mid-window, restart it, wait for it to catch up | **20/20** | **25.1 s** | 60 s | 88,000 / 88,000 | 0 | PASS |

**4/4 recovered to a verified consistent state**, with zero missing readings, zero
duplicates and zero reordering in every case. Raw results: `docs/results/chaos.json`.

NFR-7 asks for at least three fault modes recovering with bounded lag. Four are covered and
all four clear the budget.

### 2.1 The fifth: killing the Flink TaskManager mid-checkpoint

Run separately, because it needs a job already running and checkpointing and therefore
cannot reset the topic underneath itself:

```
docker compose --profile flink up -d
docker compose --profile flink exec flink-jobmanager flink run -d -py /opt/vigil/scoring_job.py
python loadgen.py --rate 400 --duration 420 --channels 8
python chaos.py --fault flink-taskmanager-kill --no-reset --no-loadgen     --settle-s 15 --hold-s 60 --budget-s 120 --report-json docs/results/chaos-flink.json
python flink_parity.py --report-json docs/results/flink-parity-after-kill.json
```

| | |
|---|---|
| Fault | SIGKILL the TaskManager while the job is checkpointing, hold it down 60 s |
| Disruption observed | **58/58 serviceability samples unhealthy** |
| Job behaviour | failed, restarted, and **restored from checkpoint 5**; 7 restore cycles in total while no slots were available |
| Recovery | **14.9 s** from container restart to redeployed and running (budget 120 s) |
| Reconciliation over the whole stream afterwards | 168,000 readings, **drift 0**, missing 0, duplicates 0, reordered 0 |
| Committed window scores | **328 distinct (channel, window), 0 duplicates** |
| Agreement with the Python detector | 328/328 within tolerance, maximum absolute difference **7.7e-12** |

The topic's end offset is 388 against 328 delivered records; the 60-record difference is
transaction control markers, which occupy offsets but are never handed to a `read_committed`
consumer. That gap is what the transactional sink looks like from outside.

**This is the scenario that exercises two-phase commit**, and it is the one that was missing
when `docs/CORRECTNESS.md` said the exactly-once claim rested on configuration rather than on
evidence from this deployment. A job that crashed seven times, restored from a checkpoint,
and still delivered exactly one committed score per window is that evidence.

Hold length matters here and 60 s is not arbitrary: Flink notices a dead TaskManager by
heartbeat timeout, which is tens of seconds. A shorter hold restarts the container before the
JobManager has registered the failure, and the job never redeploys -- so the scenario would
be testing container restart, not checkpoint recovery. Section 4 has what that looked like
when it went wrong.

---

## 3. Why these four, and how they differ

They are deliberately different *kinds* of failure, not four flavours of "the broker is
down":

- **`broker-kill`** is crash recovery. SIGKILL rather than a graceful stop, so the broker
  gets no chance to flush or hand off. This is the only one with a substantial recovery time
  (18.3 s), because the broker has to restart, replay its log and re-elect itself.
- **`broker-pause`** is a freeze. The processes stop but the TCP connections stay open, so
  clients see *silence* rather than a reset -- the shape of a GC pause, a hung disk or a
  saturated host. This is the case where the client's own timeouts decide the outcome rather
  than the broker's, and the producer's retry buffer absorbed it entirely.
- **`network-partition`** leaves the broker running and holding all its state; it simply
  becomes unreachable. Unlike a kill, there is no restart afterwards to paper over an
  inconsistency, so the state that comes back is exactly the state that went away.
- **`consumer-kill`** is the only one that tests *our* recovery rather than Kafka's. It
  lands while windows are open and episodes are half-built, and it exercises the specific
  claim in `docs/CORRECTNESS.md`: offsets commit only after episodes are durable, and the
  sink upserts on `(channel, t_start_ms, raised_by)`, so a replay re-derives the same
  episodes rather than duplicating them.

---

## 4. Three bugs this suite found in itself

Recorded because they are the reason the numbers above can be trusted, and because a chaos
suite that cannot be wrong is not measuring anything.

All three had the same shape: a scenario reporting success for something it had not
measured.

**The first run reported 4/4 and three of them were meaningless.** Only `broker-kill` had
actually disrupted anything: the producer logged no errors at all during the broker pause or
the network partition, and recovery was measured at 0.0 s in both cases. Injecting a fault
and then reporting a pass without checking it bit is a green result that means nothing.
Serviceability is now sampled throughout the hold and a scenario with zero unhealthy samples
fails. See ADR-021.

**The network-partition health check was testing the wrong thing.** It called the broker
healthy if a Kafka client could reach it -- but Docker's published-port proxy keeps
answering the TCP handshake after a container leaves its network, so the check reported
healthy throughout a partition that had genuinely been applied. It now inspects the
container's network attachments directly as well.

**`consumer-kill` measured nothing about recovery.** Its health check returned true the
instant the process exited, so "recovery" was the 0.0 s it took to notice a dead process was
dead. It now restarts the detector through a caller-supplied factory and defines recovery as
**consumer-group lag returning to near zero** -- a restarted consumer that never catches up
has not recovered, and a liveness check would have called it recovered. Unknown lag is
reported as unknown rather than treated as zero. That change is why its recovery time went
from a meaningless 0.0 s to a real 25.1 s.

**The Flink scenario reported 0.1 s recovery, and PASS, for a job that had not recovered.**
Its health check asked the JobManager whether the job was RUNNING with every task running.
It was -- because Flink detects a dead TaskManager by heartbeat timeout, and for the tens of
seconds before that fires, the JobManager's view of a job whose tasks are already dead is
indistinguishable from a healthy one. Every condition the check tested was true, and none of
them meant anything. The job actually redeployed about 50 seconds later, restoring from
checkpoint 6, long after the scenario had declared victory.

The fix is to record the vertex deployment timestamps at injection and require them to have
*moved* before calling it recovered: a job that never redeployed has not recovered, however
healthy it claims to be. Injecting onto a cluster with no running job is now refused outright
for the same reason -- killing an idle TaskManager proves nothing about checkpoint recovery.
Three regression tests cover the stale RUNNING state, tasks still deploying, and the empty
cluster.

---

## 5. What this does not cover

Stated plainly, because the gaps matter as much as the results:

- **Single broker, replication factor 1.** There is no leader election to test, no ISR to
  shrink, and no partial-availability case where some partitions survive and others do not.
  The recovery times above are single-node recovery times and do not project to a cluster.
- **No disk-full or corruption faults.** The suite kills and isolates processes; it does not
  corrupt a log segment or exhaust a volume.
- **The Flink scenario was run once, on one job, at one hold length.** It is real evidence
  (section 2.1) but it is a single observation: one kill, one checkpoint restore, 328
  windows. It does not establish behaviour under repeated kills, under a kill during the
  commit itself rather than between checkpoints, or with more than one TaskManager.
- **The producer absorbed every outage from its retry buffer.** Zero readings were lost in
  any scenario, which is the correct result at these durations, but it means the suite has
  not yet found the hold length at which the producer's buffer overflows and loss becomes
  unavoidable. Finding that boundary would be a stronger result than not reaching it.
