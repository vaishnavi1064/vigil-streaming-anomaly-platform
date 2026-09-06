"""The pluggable context-signal interface (FR-9, ADR-003).

A context signal is any operational fact that could explain an excursion. v1 has two kinds
-- pipeline disturbances from the reconciliation harness, and deploy markers -- and they
reach the policy through one interface, on one topic, in one schema. That is the whole
extensibility claim: adding a maintenance-window or feature-flag signal in v2 means writing
one class, not threading a new path through detection.

**Availability is part of the contract.** A source must be able to say "I do not know"
distinctly from "there was nothing". Conditioning fails open on the former (ADR-007) and
conditions normally on the latter, and a source that conflated them would turn a signal
outage into silent suppression -- the exact failure mode the fail-open rule exists to
prevent.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from vigil.context import ContextEvent, ContextKind

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalWindow:
    """The span a lookup is about.

    The channel is part of the request because a future source may genuinely need it -- a
    per-asset maintenance API, say. The two sources here filter on **time only** and leave
    scope enforcement to the policy, deliberately: filtering scope here would make an
    out-of-scope deploy indistinguishable from no deploy at all, and the policy could then
    only tell an operator "no context", when the useful answer is "there was a deploy but it
    did not touch this channel".
    """

    channel: str
    t_start_ms: int
    t_end_ms: int


@dataclass(frozen=True)
class SignalLookup:
    """What a source found, and whether it was in a position to look.

    `available=False` is not an empty result. It means the source could not answer, and the
    policy must then fall back to unconditioned detection rather than concluding there was
    no context.
    """

    events: tuple[ContextEvent, ...]
    available: bool
    source: str
    reason: str = ""

    @classmethod
    def unavailable(cls, source: str, reason: str) -> SignalLookup:
        return cls(events=(), available=False, source=source, reason=reason)


class ContextSignalSource(ABC):
    """One kind of operational context. Implement this to add a new one."""

    name: str
    kinds: tuple[ContextKind, ...]

    @abstractmethod
    def signals_for(self, window: SignalWindow) -> SignalLookup:
        """Every event that overlaps this window and applies to this channel."""

    def close(self) -> None:
        return None


class StaticContextSource(ContextSignalSource):
    """A fixed set of events. For tests, replays, and offline evaluation."""

    def __init__(
        self,
        events: list[ContextEvent],
        *,
        name: str = "static",
        available: bool = True,
    ) -> None:
        self.name = name
        self.kinds = tuple({e.kind for e in events}) or (ContextKind.DEPLOY, ContextKind.PIPELINE)
        self._events = sorted(events, key=lambda e: e.t_start_ms)
        self._available = available

    def signals_for(self, window: SignalWindow) -> SignalLookup:
        if not self._available:
            return SignalLookup.unavailable(self.name, "source marked unavailable")
        hits = tuple(
            e for e in self._events if e.overlaps(window.t_start_ms, window.t_end_ms)
        )
        return SignalLookup(events=hits, available=True, source=self.name)


class KafkaContextSource(ContextSignalSource):
    """Context events tailed from the context topic into a bounded in-memory window.

    Held in memory rather than queried per lookup because conditioning happens on the
    episode path, and a broker round-trip per episode would put a network call in a decision
    that has to be cheap. The buffer is bounded by age, so its size is a function of the
    retention horizon rather than of uptime.

    If the tail thread has not managed to connect, or has fallen over, lookups report
    **unavailable** rather than empty. That distinction is the whole of ADR-007.
    """

    def __init__(
        self,
        bootstrap: str,
        topic: str,
        *,
        group: str = "vigil-conditioning",
        retain_ms: int = 3_600_000,
        name: str = "kafka-context",
    ) -> None:
        self.name = name
        self.kinds = (ContextKind.DEPLOY, ContextKind.PIPELINE)
        self.bootstrap = bootstrap
        self.topic = topic
        self.group = group
        self.retain_ms = retain_ms
        self._events: list[ContextEvent] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._failure: str | None = None
        self.events_seen = 0
        self.malformed = 0

    def start(self, wait_s: float = 10.0) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._tail, name="context-tail", daemon=True)
        self._thread.start()
        self._connected.wait(timeout=wait_s)

    def _tail(self) -> None:
        try:
            from confluent_kafka import Consumer, KafkaError

            consumer = Consumer(
                {
                    "bootstrap.servers": self.bootstrap,
                    "group.id": self.group,
                    # Context is small and every consumer needs all of it, so start from the
                    # earliest retained event rather than from now: an episode arriving one
                    # second after startup still needs the deploy that began a minute ago.
                    "auto.offset.reset": "earliest",
                    "enable.auto.commit": False,
                    "isolation.level": "read_committed",
                }
            )
            consumer.subscribe([self.topic])
            self._connected.set()
        except Exception as exc:  # noqa: BLE001 - unavailability is a handled state
            self._failure = f"could not subscribe: {exc}"
            log.error("context source unavailable: %s", self._failure)
            self._connected.set()
            return

        try:
            while not self._stop.is_set():
                message = consumer.poll(0.5)
                if message is None:
                    continue
                if message.error():
                    if message.error().code() != KafkaError._PARTITION_EOF:
                        self._failure = str(message.error())
                    continue
                self._failure = None
                try:
                    event = ContextEvent.from_json(message.value())
                except (ValueError, KeyError, TypeError):
                    self.malformed += 1
                    continue
                with self._lock:
                    self._events.append(event)
                    self.events_seen += 1
                    self._evict_locked()
        finally:
            consumer.close()

    def _evict_locked(self) -> None:
        if not self._events:
            return
        newest = max(e.t_end_ms for e in self._events)
        cutoff = newest - self.retain_ms
        self._events = [e for e in self._events if e.t_end_ms >= cutoff]

    @property
    def available(self) -> bool:
        return self._connected.is_set() and self._failure is None

    def signals_for(self, window: SignalWindow) -> SignalLookup:
        if not self._connected.is_set():
            return SignalLookup.unavailable(self.name, "context tail has not connected")
        if self._failure:
            return SignalLookup.unavailable(self.name, self._failure)
        with self._lock:
            hits = tuple(
                e for e in self._events if e.overlaps(window.t_start_ms, window.t_end_ms)
            )
        return SignalLookup(events=hits, available=True, source=self.name)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


@dataclass
class CompositeContextSource(ContextSignalSource):
    """Several sources behind one interface.

    Availability is deliberately **pessimistic**: if any constituent source cannot answer,
    the composite reports unavailable, so conditioning fails open. Treating a partial answer
    as complete would mean suppressing an episode on the strength of the sources that
    happened to be up, while the one that would have exonerated it was down.
    """

    sources: list[ContextSignalSource] = field(default_factory=list)
    name: str = "composite"

    def __post_init__(self) -> None:
        self.kinds = tuple({k for s in self.sources for k in s.kinds})

    def signals_for(self, window: SignalWindow) -> SignalLookup:
        events: list[ContextEvent] = []
        for source in self.sources:
            lookup = source.signals_for(window)
            if not lookup.available:
                return SignalLookup.unavailable(
                    self.name, f"{lookup.source} unavailable: {lookup.reason}"
                )
            events.extend(lookup.events)
        return SignalLookup(
            events=tuple(sorted(events, key=lambda e: e.t_start_ms)),
            available=True,
            source=self.name,
        )

    def close(self) -> None:
        for source in self.sources:
            source.close()


class FlakyClock:
    """Monotonic milliseconds. Injected so tests need not sleep."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)
