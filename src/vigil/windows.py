"""Event-time sliding windows, keyed by channel.

Deliberately pure: no Kafka, no clock, no threads. Everything it decides is a function of
the event timestamps it is given. That is what makes the out-of-order and lateness
behaviour testable here rather than only observable in a running pipeline, and it is what
lets Phase 2 hand the same semantics to Flink and compare the two directly.

**Watermark model.** The watermark is `max event time seen - allowed_lateness`. A window
closes when the watermark passes its end. An event older than the watermark is late: it is
counted and dropped, never silently folded into a window that has already been emitted and
scored. Reopening a closed window would mean a second, different score for a window the
detector has already decided on, which is exactly the kind of quiet inconsistency the
reconciliation harness exists to catch.

Per channel, not global, because the channels here are genuinely independent devices: one
inverter falling silent must not stall the watermark of every other inverter in the fleet.
The cost is that a channel that stops publishing leaves its last windows unclosed until
`close_all()` -- that is the correct trade, and the ingest gap watch is what notices.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterator
from dataclasses import dataclass, field

from vigil.readings import Reading


@dataclass(frozen=True, slots=True)
class Window:
    """A closed, scored-once slice of one channel's series."""

    channel: str
    start_ms: int
    end_ms: int
    values: tuple[float, ...]
    event_ts_ms: tuple[int, ...]
    # Ground-truth labels, present only on the synthetic source. Carried through so the
    # evaluation harness can score a window; no detector reads it.
    injected: tuple[str | None, ...] = ()

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def contains_injected_anomaly(self) -> bool:
        return any(label is not None for label in self.injected)

    def __repr__(self) -> str:
        return f"Window({self.channel} [{self.start_ms}, {self.end_ms}) n={self.count})"


def window_starts(event_ts_ms: int, size_ms: int, slide_ms: int) -> Iterator[int]:
    """Every sliding-window start that covers this timestamp.

    With size == slide these are tumbling windows and exactly one start is yielded.
    """
    first = ((event_ts_ms - size_ms) // slide_ms + 1) * slide_ms
    start = first
    while start <= event_ts_ms:
        if start + size_ms > event_ts_ms:
            yield start
        start += slide_ms


@dataclass
class _ChannelState:
    watermark_ms: int | None = None
    max_event_ts_ms: int | None = None
    open_windows: dict[int, list[tuple[int, float, str | None]]] = field(default_factory=dict)


@dataclass
class SlidingWindowAssigner:
    """Assigns readings to event-time sliding windows and emits them once closed.

    `size_ms` is the window width, `slide_ms` how far each window advances. The default
    30s/10s gives three overlapping views of every point, so a short excursion is scored
    against three different surrounding contexts rather than depending on where an
    arbitrary boundary happened to fall.
    """

    size_ms: int = 30_000
    slide_ms: int = 10_000
    allowed_lateness_ms: int = 5_000
    min_points: int = 8

    _channels: dict[str, _ChannelState] = field(default_factory=dict, init=False, repr=False)
    late_readings: int = field(default=0, init=False)
    dropped_thin_windows: int = field(default=0, init=False)
    emitted_windows: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.slide_ms <= 0 or self.size_ms <= 0:
            raise ValueError("window size and slide must be positive")
        if self.size_ms % self.slide_ms:
            raise ValueError(
                f"size_ms ({self.size_ms}) must be a whole multiple of slide_ms "
                f"({self.slide_ms}); otherwise window boundaries drift and two runs over "
                f"the same data can produce different windows"
            )

    def add(self, reading: Reading) -> list[Window]:
        """Add a reading; return any windows this reading's watermark closed."""
        state = self._channels.setdefault(reading.channel, _ChannelState())

        if state.max_event_ts_ms is None or reading.event_ts_ms > state.max_event_ts_ms:
            state.max_event_ts_ms = reading.event_ts_ms
            state.watermark_ms = state.max_event_ts_ms - self.allowed_lateness_ms

        if state.watermark_ms is not None and reading.event_ts_ms < state.watermark_ms:
            # Older than the watermark. Its windows may already be closed and scored, so
            # admitting it now would silently change a decision that has been acted on.
            self.late_readings += 1
            return []

        for start in window_starts(reading.event_ts_ms, self.size_ms, self.slide_ms):
            bucket = state.open_windows.setdefault(start, [])
            bucket.append((reading.event_ts_ms, reading.value, reading.injected))

        return self._close_passed(reading.channel, state)

    def _close_passed(self, channel: str, state: _ChannelState) -> list[Window]:
        if state.watermark_ms is None:
            return []
        due = [s for s in state.open_windows if s + self.size_ms <= state.watermark_ms]
        return self._emit(channel, state, sorted(due))

    def _emit(self, channel: str, state: _ChannelState, starts: list[int]) -> list[Window]:
        out: list[Window] = []
        for start in starts:
            points = state.open_windows.pop(start)
            if len(points) < self.min_points:
                # Too few points to say anything about dispersion. Scoring it anyway would
                # manufacture confident nonsense out of two or three samples.
                self.dropped_thin_windows += 1
                continue
            points.sort(key=lambda p: p[0])
            out.append(
                Window(
                    channel=channel,
                    start_ms=start,
                    end_ms=start + self.size_ms,
                    values=tuple(p[1] for p in points),
                    event_ts_ms=tuple(p[0] for p in points),
                    injected=tuple(p[2] for p in points),
                )
            )
        self.emitted_windows += len(out)
        return out

    def close_all(self) -> list[Window]:
        """Flush every remaining open window. For shutdown and bounded test runs."""
        out: list[Window] = []
        for channel, state in self._channels.items():
            out.extend(self._emit(channel, state, sorted(state.open_windows)))
        return out

    def watermark_for(self, channel: str) -> int | None:
        state = self._channels.get(channel)
        return state.watermark_ms if state else None

    @property
    def open_window_count(self) -> int:
        return sum(len(s.open_windows) for s in self._channels.values())

    @property
    def channels(self) -> int:
        return len(self._channels)


class ChannelHistory:
    """A bounded per-channel tail of recent values.

    The z-score baseline needs a reference distribution wider than the window it is
    scoring, or it would be comparing a window against itself. Bounded because unbounded
    per-key state on a stream is the standard way to die slowly: 336 live channels at an
    unbounded tail is a leak, at a fixed 600 samples it is a known ceiling.
    """

    def __init__(self, capacity: int = 600) -> None:
        self.capacity = capacity
        self._tails: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=capacity))

    def extend(self, channel: str, values: tuple[float, ...]) -> None:
        self._tails[channel].extend(values)

    def tail(self, channel: str) -> tuple[float, ...]:
        return tuple(self._tails[channel])

    def __len__(self) -> int:
        return len(self._tails)
