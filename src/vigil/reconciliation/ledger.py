"""Per-stage identity invariants over the reading stream.

Row counts cannot tell "processed a million events" from "processed one event a million
times". So reconciliation here is built on **identity**: every reading carries a per-channel
sequence number assigned at the ingestion boundary, and a channel's sequence is dense by
construction. That gives an invariant that needs no second source to check against:

    for a healthy channel:  readings_seen == max_seq - min_seq + 1

A shortfall is missing readings; an excess is duplicates. Neither is inferable from a count
alone, which is exactly the point.

**Bounded state.** Tracking which sequence numbers were seen would cost memory proportional
to the stream. Instead each channel keeps O(1) state -- the next sequence expected, plus
counters -- which works because readings are keyed by channel, so all of a channel's
readings land in one partition and arrive in order. Out-of-order arrival within a channel is
therefore itself a finding, not a case to absorb silently.

This module is pure: no Kafka, no clock, no database. The harness drives it. That is what
lets the invariants be tested directly, and it is what will let the same ledger be carried
onto Flink in the migration and compared against these results unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from vigil.readings import Reading


class HealthSeverity(StrEnum):
    """How badly a window departed from its invariants."""

    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class ChannelLedger:
    """O(1) sequence-identity state for one channel."""

    channel: str
    first_seq: int | None = None
    last_seq: int | None = None
    expected_next: int | None = None
    readings: int = 0
    missing: int = 0
    duplicates: int = 0
    regressions: int = 0

    def observe(self, seq: int) -> None:
        self.readings += 1
        if self.first_seq is None:
            self.first_seq = seq
            self.last_seq = seq
            self.expected_next = seq + 1
            return

        if seq == self.expected_next:
            self.last_seq = seq
            self.expected_next = seq + 1
            return

        if seq > self.expected_next:
            # A run of sequence numbers never arrived. Counted as missing now rather than
            # waiting: with per-channel ordering there is nothing still in flight that
            # could fill the hole, so deferring the finding would only delay it.
            self.missing += seq - self.expected_next
            self.last_seq = seq
            self.expected_next = seq + 1
            return

        # seq < expected_next. Either a redelivery or genuine reordering. Both are
        # findings; neither is silently absorbed.
        if seq == self.last_seq:
            self.duplicates += 1
        else:
            self.regressions += 1

    @property
    def span(self) -> int:
        """How many readings a dense sequence over this range would contain."""
        if self.first_seq is None or self.last_seq is None:
            return 0
        return self.last_seq - self.first_seq + 1

    @property
    def drift(self) -> int:
        """span - readings. Zero means the identity invariant holds.

        Positive means readings are missing; negative means more arrived than the range can
        account for, which is duplication.
        """
        return self.span - self.readings

    @property
    def healthy(self) -> bool:
        return self.drift == 0 and self.duplicates == 0 and self.regressions == 0


@dataclass(frozen=True, slots=True)
class PipelineHealth:
    """The per-window health signal. This is the wire the core contribution runs on.

    Emitted whether or not anything is wrong: a consumer of this signal needs to be able to
    tell "the window was clean" from "no signal arrived", and those must not look the same.
    Conditioning fails open on a missing signal (ADR-007), so the distinction is load-bearing.
    """

    window_start_ms: int
    window_end_ms: int
    channels: int
    readings: int
    missing: int
    duplicates: int
    regressions: int
    max_lag_ms: int
    severity: HealthSeverity

    @property
    def disturbed(self) -> bool:
        return self.severity is not HealthSeverity.OK

    @property
    def drift(self) -> int:
        return self.missing - self.duplicates

    def summary(self) -> str:
        return (
            f"[{self.window_start_ms}, {self.window_end_ms}) "
            f"{self.channels} ch | {self.readings:,} readings | "
            f"missing {self.missing} | dupes {self.duplicates} | "
            f"reordered {self.regressions} | lag {self.max_lag_ms} ms | {self.severity}"
        )


@dataclass
class HealthThresholds:
    """Where a departure stops being noise and starts being a disturbance.

    Lag is graded separately from loss because they mean different things: lag says the
    pipeline is behind, loss says it dropped something. A detector conditioned on this
    signal should treat a badly lagging window differently from a lossy one, so collapsing
    them into one number here would destroy information downstream.
    """

    # Any loss at all is a disturbance -- the whole premise is that the sequence is dense.
    missing_warning: int = 1
    missing_critical: int = 100
    duplicates_warning: int = 1
    duplicates_critical: int = 100
    lag_info_ms: int = 5_000
    lag_warning_ms: int = 30_000
    lag_critical_ms: int = 120_000
    # Lag is measured against the wall clock, so replaying a topic recorded an hour ago
    # legitimately reports an hour of lag -- the data really is that old. That is a true
    # statement about the read, not a disturbance in the pipeline, and grading on it would
    # mark every window of every replay critical and suppress everything downstream. A
    # replay or benchmark run turns lag grading off and says so; a live run leaves it on.
    grade_lag: bool = True

    def grade(
        self, missing: int, duplicates: int, regressions: int, max_lag_ms: int
    ) -> HealthSeverity:
        lag = max_lag_ms if self.grade_lag else 0
        if (
            missing >= self.missing_critical
            or duplicates >= self.duplicates_critical
            or lag >= self.lag_critical_ms
        ):
            return HealthSeverity.CRITICAL
        if (
            missing >= self.missing_warning
            or duplicates >= self.duplicates_warning
            or regressions > 0
            or lag >= self.lag_warning_ms
        ):
            return HealthSeverity.WARNING
        if lag >= self.lag_info_ms:
            return HealthSeverity.INFO
        return HealthSeverity.OK


@dataclass
class _SourceWatermark:
    """How far one input source (a Kafka partition) has advanced in event time.

    `high_event_ts_ms` is None while the source is assigned but has delivered nothing yet.
    A pending source holds the watermark back completely -- it is not the same as a source
    at time zero, and treating it as absent instead was worth 51% of readings arriving late
    on a replay, because partitions get discovered lazily as they first deliver.
    """

    high_event_ts_ms: int | None
    last_seen_wall_s: float


@dataclass
class _WindowState:
    missing: int = 0
    duplicates: int = 0
    regressions: int = 0
    readings: int = 0
    max_lag_ms: int = 0
    channels: set[str] = field(default_factory=set)


@dataclass
class ReconciliationLedger:
    """Accumulates identity invariants and emits a health signal per window.

    Windows here are tumbling and aligned to the same grid the detector uses, so a health
    record and an anomaly flag for the same instant refer to the same span of time. If the
    two used different grids, "does this disturbance overlap that anomaly" would become an
    approximation, and the core mechanism would rest on it.
    """

    window_ms: int = 30_000
    thresholds: HealthThresholds = field(default_factory=HealthThresholds)
    # A source that has said nothing for this long stops holding the watermark back. Kafka
    # partitions go quiet routinely -- a channel stops publishing, a partition has no
    # traffic -- and without this one idle partition would stall every window forever.
    # This is the idle-source problem Flink solves the same way.
    source_idle_timeout_s: float = 30.0

    channels: dict[str, ChannelLedger] = field(default_factory=dict, init=False)
    _windows: dict[int, _WindowState] = field(default_factory=dict, init=False, repr=False)
    _highest_window: int | None = field(default=None, init=False, repr=False)
    _sources: dict[str, _SourceWatermark] = field(default_factory=dict, init=False, repr=False)
    # Everything below this has already been emitted. A single low-water mark is enough
    # because windows close in order, and it costs O(1) rather than a growing set.
    _closed_below: int | None = field(default=None, init=False, repr=False)
    total_readings: int = field(default=0, init=False)
    late_readings: int = field(default=0, init=False)

    def observe(
        self,
        reading: Reading,
        ingest_wall_ms: int | None = None,
        source: str | None = None,
    ) -> None:
        """Fold in one reading.

        `ingest_wall_ms` is when this process saw it. Lag is the gap between that and the
        reading's event time -- how far behind the pipeline is running.

        `source` names the input this arrived on, normally a Kafka partition. Windows close
        on the *minimum* event time across sources, so a partition racing ahead cannot close
        a window that a slower partition has not reached yet.
        """
        if source is not None:
            wm = self._sources.get(source)
            wall = (ingest_wall_ms or 0) / 1000.0
            if wm is None:
                self._sources[source] = _SourceWatermark(reading.event_ts_ms, wall)
            else:
                wm.high_event_ts_ms = (
                    reading.event_ts_ms
                    if wm.high_event_ts_ms is None
                    else max(wm.high_event_ts_ms, reading.event_ts_ms)
                )
                wm.last_seen_wall_s = max(wm.last_seen_wall_s, wall)

        ledger = self.channels.get(reading.channel)
        if ledger is None:
            ledger = ChannelLedger(channel=reading.channel)
            self.channels[reading.channel] = ledger

        before = (ledger.missing, ledger.duplicates, ledger.regressions)
        ledger.observe(reading.seq)
        after = (ledger.missing, ledger.duplicates, ledger.regressions)

        window_start = (reading.event_ts_ms // self.window_ms) * self.window_ms
        if self._closed_below is not None and window_start < self._closed_below:
            # Its window was emitted already. Re-opening it would publish a second, smaller
            # health record for a span a consumer has already been told about -- during a
            # replay, where partitions are consumed at wildly different event-time offsets,
            # that produced hundreds of thousands of one-reading phantom windows. The
            # channel ledger above still counted it, so the drift figure is unaffected;
            # only the per-window attribution is lost, and that is reported as lateness.
            self.late_readings += 1
            self.total_readings += 1
            return

        state = self._windows.get(window_start)
        if state is None:
            state = _WindowState()
            self._windows[window_start] = state
        if self._highest_window is None or window_start > self._highest_window:
            self._highest_window = window_start

        state.readings += 1
        state.channels.add(reading.channel)
        state.missing += after[0] - before[0]
        state.duplicates += after[1] - before[1]
        state.regressions += after[2] - before[2]
        if ingest_wall_ms is not None:
            state.max_lag_ms = max(state.max_lag_ms, ingest_wall_ms - reading.event_ts_ms)

        self.total_readings += 1

    def register_source(self, source: str, wall_s: float) -> None:
        """Declare a source assigned but not yet heard from.

        Registering on assignment rather than on first message is what stops a partition
        that has simply not been served yet from being mistaken for one that does not exist.
        """
        self._sources.setdefault(source, _SourceWatermark(None, wall_s))

    def forget_source(self, source: str) -> None:
        """Drop a source that is no longer assigned, so a rebalance does not stall us."""
        self._sources.pop(source, None)

    def watermark_ms(self) -> int | None:
        """The minimum event time across all non-idle sources.

        Taking the *maximum* would be the classic mistake: during a replay Kafka hands
        partitions over in bursts, so one races minutes ahead and closes windows the others
        have not reached, and every reading from the laggards then arrives late. Measured on
        a 7-minute replay, max stranded 74% of readings; min with lazily-discovered sources
        still stranded 51%, because a partition not yet served looked like no partition.
        """
        if not self._sources:
            return None if self._highest_window is None else self._highest_window

        newest_wall = max(w.last_seen_wall_s for w in self._sources.values())
        active = [
            w
            for w in self._sources.values()
            if newest_wall - w.last_seen_wall_s <= self.source_idle_timeout_s
        ]
        if not active:
            active = list(self._sources.values())

        # Any active source that has not spoken yet blocks the watermark entirely. It has
        # made no claim about how far event time has advanced, and assuming one would be
        # inventing the very information the watermark exists to establish.
        if any(w.high_event_ts_ms is None for w in active):
            return None
        return min(w.high_event_ts_ms for w in active)

    def close_due(self, grace_windows: int = 1) -> list[PipelineHealth]:
        """Emit health for windows every source has moved past.

        `grace_windows` holds a window open that long beyond the watermark, so readings
        still arriving are not scored as missing purely because the harness was impatient.
        """
        watermark = self.watermark_ms()
        if watermark is None:
            return []
        highest = (watermark // self.window_ms) * self.window_ms
        cutoff = highest - grace_windows * self.window_ms
        due = sorted(start for start in self._windows if start < cutoff)
        if due:
            self._closed_below = max(self._closed_below or 0, max(due) + self.window_ms)
        return [self._emit(start) for start in due]

    def close_all(self) -> list[PipelineHealth]:
        starts = sorted(self._windows)
        if starts:
            self._closed_below = max(self._closed_below or 0, starts[-1] + self.window_ms)
        return [self._emit(start) for start in starts]

    def _emit(self, window_start: int) -> PipelineHealth:
        state = self._windows.pop(window_start)
        return PipelineHealth(
            window_start_ms=window_start,
            window_end_ms=window_start + self.window_ms,
            channels=len(state.channels),
            readings=state.readings,
            missing=state.missing,
            duplicates=state.duplicates,
            regressions=state.regressions,
            max_lag_ms=max(state.max_lag_ms, 0),
            severity=self.thresholds.grade(
                state.missing, state.duplicates, state.regressions, max(state.max_lag_ms, 0)
            ),
        )

    # -- whole-run totals, for the drift claim --

    @property
    def sources_tracked(self) -> int:
        return len(self._sources)

    @property
    def total_missing(self) -> int:
        return sum(c.missing for c in self.channels.values())

    @property
    def total_duplicates(self) -> int:
        return sum(c.duplicates for c in self.channels.values())

    @property
    def total_regressions(self) -> int:
        return sum(c.regressions for c in self.channels.values())

    @property
    def total_drift(self) -> int:
        """Sum of per-channel drift. This is the number the zero-drift claim is about."""
        return sum(c.drift for c in self.channels.values())

    @property
    def unhealthy_channels(self) -> list[ChannelLedger]:
        return [c for c in self.channels.values() if not c.healthy]

    def report(self) -> str:
        drift = self.total_drift
        verdict = "ZERO DRIFT" if drift == 0 and not self.unhealthy_channels else "DRIFT DETECTED"
        late = (
            f" | late (window already closed) {self.late_readings:,}" if self.late_readings else ""
        )
        return (
            f"{verdict}: {self.total_readings:,} readings across {len(self.channels)} channels | "
            f"drift {drift} | missing {self.total_missing} | "
            f"duplicates {self.total_duplicates} | reordered {self.total_regressions}{late}"
        )
