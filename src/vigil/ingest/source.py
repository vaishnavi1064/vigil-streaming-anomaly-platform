"""The ingestion boundary contract, and the two things every source needs.

A source produces readings. It does not know about Kafka, and the publisher does not know
where readings came from -- so the live solar feed and the synthetic generator are
interchangeable, and every downstream stage sees one wire format.

Two concerns live here rather than in each source because both sources need them and
getting them subtly different per source would silently break reconciliation:

  SequenceAssigner  stamps the per-channel identity the correctness layer checks
                    invariants against.
  IngestGapWatch    notices when a channel that was arriving on a cadence stops.
"""

from __future__ import annotations

import statistics
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import TracebackType

from vigil.readings import Reading


class ReadingSource(ABC):
    """A stream of readings with a name and a lifecycle.

    Implementations block in `readings()`. Closing is what stops them.
    """

    name: str

    @abstractmethod
    def readings(self) -> Iterator[Reading]:
        """Yield readings until the source is closed or exhausted."""

    def close(self) -> None:
        """Release whatever the source holds. Must be safe to call twice.

        Not abstract: a source with nothing to release is legitimate, and forcing every
        one of them to write an empty override would be noise.
        """
        return None

    def __enter__(self) -> ReadingSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class SequenceAssigner:
    """Per-channel monotonic sequence numbers, assigned at the ingestion boundary.

    Downstream treats these as identity: a missing number is a gap, a repeated number is a
    duplicate. That makes the boundary the origin of the correctness claim -- anything lost
    upstream of here (the public broker is QoS 0) is by definition invisible to it, which
    is why docs/CORRECTNESS.md scopes the guarantee to the interior.
    """

    def __init__(self) -> None:
        self._next: dict[str, int] = defaultdict(int)

    def next_for(self, channel: str) -> int:
        self._next[channel] += 1
        return self._next[channel]

    @property
    def channels(self) -> int:
        return len(self._next)

    def total_assigned(self) -> int:
        return sum(self._next.values())


@dataclass(frozen=True)
class IngestGap:
    """A channel went quiet for longer than its own established cadence explains."""

    channel: str
    last_seen_s: float
    resumed_at_s: float
    expected_cadence_s: float

    @property
    def duration_s(self) -> float:
        return self.resumed_at_s - self.last_seen_s

    @property
    def estimated_missing(self) -> int:
        if self.expected_cadence_s <= 0:
            return 0
        return max(0, int(round(self.duration_s / self.expected_cadence_s)) - 1)


@dataclass
class IngestGapWatch:
    """Detects silence on a per-channel cadence learned from the channel itself.

    A fixed timeout cannot work here: the solar fleet publishes site rollups every few
    seconds and string telemetry hundreds of times a second, so one threshold would either
    miss real outages on the fast channels or cry wolf on the slow ones. The cadence is
    learned per channel from the median of recent inter-arrival times, which is robust to
    the occasional scheduling hiccup in a way a mean is not.

    Detection only. This feed exposes no history API, so there is nothing to backfill
    from -- see ADR-011.
    """

    gap_factor: float = 6.0
    min_gap_s: float = 1.0
    cadence_window: int = 32
    warmup_samples: int = 8

    _last_seen: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _gaps_seen: dict[str, deque[float]] = field(default_factory=dict, init=False, repr=False)
    gaps: list[IngestGap] = field(default_factory=list, init=False)

    def observe(self, channel: str, arrival_s: float) -> IngestGap | None:
        last = self._last_seen.get(channel)
        self._last_seen[channel] = arrival_s
        if last is None:
            self._gaps_seen[channel] = deque(maxlen=self.cadence_window)
            return None

        interval = arrival_s - last
        history = self._gaps_seen[channel]

        gap: IngestGap | None = None
        if len(history) >= self.warmup_samples:
            cadence = statistics.median(history)
            threshold = max(cadence * self.gap_factor, self.min_gap_s)
            if interval > threshold:
                gap = IngestGap(
                    channel=channel,
                    last_seen_s=last,
                    resumed_at_s=arrival_s,
                    expected_cadence_s=cadence,
                )
                self.gaps.append(gap)

        # A gap is an outage, not evidence about the normal cadence, so it must not be
        # folded into the estimate -- one long stall would otherwise raise the threshold
        # enough to hide every later stall.
        if gap is None:
            history.append(interval)
        return gap

    @property
    def estimated_missing(self) -> int:
        return sum(g.estimated_missing for g in self.gaps)
