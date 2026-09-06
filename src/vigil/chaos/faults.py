"""Fault injection: the failure modes, and how each is applied and undone.

Every fault here is a real interruption of a real process, not a simulated one. A mocked
broker outage proves the mock behaves; killing the container proves the pipeline does.

The suite exists to answer one question per fault: **after this, does the system return to
a consistent state within a bounded time?** Consistency is not judged by the system's own
opinion of itself -- it is the reconciliation harness's identity invariant over the sequence
numbers, computed by a process that did not participate in the failure.

Faults are also the pipeline half of the adversarial design in ADR-015. A pipeline
disturbance is a context signal exactly as a deploy marker is, so the same trap applies:
real anomalies are injected inside fault windows as well as outside, and the ones inside
must still be detected. Otherwise "suppress everything during a fault" would score as well
as attributing an artifact to its cause.
"""

from __future__ import annotations

import logging
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger("vigil.chaos")


class FaultError(RuntimeError):
    """A fault could not be applied or, worse, could not be undone."""


def docker(*args: str, timeout: int = 60) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise FaultError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def container_running(name: str) -> bool:
    try:
        return docker("inspect", "-f", "{{.State.Running}}", name) == "true"
    except FaultError:
        return False


def wait_until(predicate, timeout_s: float, poll_s: float = 1.0) -> float:
    """Block until predicate() is true. Returns how long it took, or raises."""
    started = time.perf_counter()
    deadline = started + timeout_s
    while time.perf_counter() < deadline:
        if predicate():
            return time.perf_counter() - started
        time.sleep(poll_s)
    raise TimeoutError(f"condition not met within {timeout_s}s")


@dataclass
class Fault(ABC):
    """One injectable failure mode."""

    name: str = field(init=False, default="fault")
    description: str = field(init=False, default="")

    @abstractmethod
    def inject(self) -> None:
        """Break something. Must raise rather than silently no-op."""

    @abstractmethod
    def heal(self) -> None:
        """Undo it. Must be safe to call even if inject() partly failed."""

    @abstractmethod
    def healthy(self) -> bool:
        """True once the broken thing is serving again."""

    def __enter__(self) -> Fault:
        log.warning("injecting fault: %s", self.name)
        self.inject()
        return self

    def __exit__(self, *_exc) -> None:
        log.warning("healing fault: %s", self.name)
        self.heal()


@dataclass
class BrokerKill(Fault):
    """Hard-kill the Kafka container.

    SIGKILL rather than a graceful stop, so the broker gets no chance to flush or hand off.
    A graceful stop tests shutdown; this tests crash recovery, which is the interesting one.
    """

    container: str = "vigil-kafka"
    bootstrap: str = "localhost:19092"

    def __post_init__(self) -> None:
        self.name = "broker-kill"
        self.description = "SIGKILL the Kafka broker, then restart it"

    def inject(self) -> None:
        if not container_running(self.container):
            raise FaultError(f"{self.container} is not running; nothing to kill")
        docker("kill", "--signal=KILL", self.container)

    def heal(self) -> None:
        if not container_running(self.container):
            docker("start", self.container)

    def healthy(self) -> bool:
        if not container_running(self.container):
            return False
        try:
            from confluent_kafka.admin import AdminClient

            AdminClient({"bootstrap.servers": self.bootstrap}).list_topics(timeout=5)
            return True
        except Exception:  # noqa: BLE001 - any failure means not yet serving
            return False


@dataclass
class BrokerPause(Fault):
    """Freeze the broker's processes without killing them.

    Distinct from a kill in a way that matters: the TCP connections stay open, so clients
    see silence rather than a reset. That is the shape of a GC pause, a hung disk, or a
    saturated host -- and it is the case where a client's own timeouts decide the outcome,
    not the broker's.
    """

    container: str = "vigil-kafka"
    bootstrap: str = "localhost:19092"
    hold_s: float = 20.0

    def __post_init__(self) -> None:
        self.name = "broker-pause"
        self.description = f"SIGSTOP the broker for {self.hold_s:g}s, then resume it"

    def inject(self) -> None:
        if not container_running(self.container):
            raise FaultError(f"{self.container} is not running; nothing to pause")
        docker("pause", self.container)

    def heal(self) -> None:
        try:
            state = docker("inspect", "-f", "{{.State.Paused}}", self.container)
        except FaultError:
            return
        if state == "true":
            docker("unpause", self.container)

    def healthy(self) -> bool:
        try:
            if docker("inspect", "-f", "{{.State.Paused}}", self.container) == "true":
                return False
            from confluent_kafka.admin import AdminClient

            AdminClient({"bootstrap.servers": self.bootstrap}).list_topics(timeout=5)
            return True
        except Exception:  # noqa: BLE001
            return False


@dataclass
class NetworkPartition(Fault):
    """Cut the broker off the network without stopping it.

    The broker keeps running and keeps its state; it simply becomes unreachable. This is the
    split-brain shape, and it is different from a kill because on heal the broker is still
    holding everything it had -- there is no restart to paper over an inconsistency.
    """

    container: str = "vigil-kafka"
    network: str = "vigil_default"
    bootstrap: str = "localhost:19092"

    def __post_init__(self) -> None:
        self.name = "network-partition"
        self.description = "disconnect the broker from the compose network, then reattach"
        self._disconnected = False

    def inject(self) -> None:
        if not container_running(self.container):
            raise FaultError(f"{self.container} is not running")
        docker("network", "disconnect", self.network, self.container)
        self._disconnected = True

    def heal(self) -> None:
        if not self._disconnected:
            return
        try:
            docker("network", "connect", "--alias", "kafka", self.network, self.container)
        except FaultError as exc:
            # Already attached is fine; anything else must surface, because a chaos run
            # that leaves the network broken would poison every later measurement.
            if "already exists" not in str(exc):
                raise
        self._disconnected = False

    def healthy(self) -> bool:
        # Check the network attachment directly as well as the client path. A published
        # port can keep answering the TCP handshake from Docker's proxy after the container
        # leaves the network, so trusting the client alone reported "healthy" throughout a
        # partition that had in fact been applied.
        try:
            networks = docker(
                "inspect", "-f", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                self.container,
            )
            if self.network not in networks.split():
                return False
        except FaultError:
            return False
        try:
            from confluent_kafka.admin import AdminClient

            AdminClient({"bootstrap.servers": self.bootstrap}).list_topics(timeout=5)
            return True
        except Exception:  # noqa: BLE001
            return False


def consumer_group_lag(bootstrap: str, group: str, topic: str, timeout: float = 10.0) -> int | None:
    """Total unread records for a consumer group. None if it cannot be determined.

    This is what makes consumer recovery measurable. "The process is running again" says
    nothing; "the group has caught back up to the log end" is the property that matters.
    """
    try:
        from confluent_kafka import Consumer, TopicPartition
        from confluent_kafka.admin import AdminClient

        admin = AdminClient({"bootstrap.servers": bootstrap})
        metadata = admin.list_topics(topic=topic, timeout=timeout)
        partitions = [TopicPartition(topic, p) for p in metadata.topics[topic].partitions]

        probe = Consumer(
            {"bootstrap.servers": bootstrap, "group.id": group, "enable.auto.commit": False}
        )
        try:
            committed = probe.committed(partitions, timeout=timeout)
            total = 0
            for tp in committed:
                _low, high = probe.get_watermark_offsets(tp, timeout=timeout, cached=False)
                # An unset offset means the group never committed for that partition, so
                # everything in it is still outstanding.
                position = tp.offset if tp.offset >= 0 else 0
                total += max(0, high - position)
            return total
        finally:
            probe.close()
    except Exception:  # noqa: BLE001 - unknown lag is reported as unknown, not as zero
        return None


@dataclass
class ConsumerKill(Fault):
    """Kill the detector mid-window, then restart it and wait for it to catch back up.

    The one fault that tests our own recovery rather than Kafka's. It lands while windows
    are open and episodes are half-built, so it exercises the exact claim in
    docs/CORRECTNESS.md: offsets commit only after episodes are durable, and the sink
    upserts on (channel, t_start_ms, raised_by), so a replay re-derives the same episodes
    instead of duplicating them.

    Recovery is **consumer-group lag returning to near zero**, not "a process is running".
    A restarted consumer that never catches up has not recovered, and a check that only
    looked at liveness would happily report that it had.
    """

    process: object | None = None
    restart: object | None = None
    bootstrap: str = "localhost:19092"
    group: str = ""
    topic: str = ""
    caught_up_within: int = 2_000

    def __post_init__(self) -> None:
        self.name = "consumer-kill"
        self.description = "SIGKILL the detector mid-window, restart it, wait for it to catch up"
        self._restarted = None

    def inject(self) -> None:
        if self.process is None:
            raise FaultError("no consumer process supplied to kill")
        if self.process.poll() is not None:
            raise FaultError("the consumer had already exited; nothing to kill")
        self.process.kill()

    def heal(self) -> None:
        # Restart through the caller-supplied factory. The fault does not know how the
        # consumer is launched, and hard-coding that here would couple it to one runner.
        if self.restart is None or self._restarted is not None:
            return
        self._restarted = self.restart()

    def healthy(self) -> bool:
        if self._restarted is None:
            # Before heal: healthy means the original consumer is still alive and keeping
            # up. Once killed, it is not, which is the disruption the suite needs to see.
            if self.process is None or self.process.poll() is not None:
                return False
            lag = self._lag()
            return lag is None or lag <= self.caught_up_within
        if self._restarted.poll() is not None:
            return False
        lag = self._lag()
        return lag is not None and lag <= self.caught_up_within

    def _lag(self) -> int | None:
        if not self.group or not self.topic:
            return None
        return consumer_group_lag(self.bootstrap, self.group, self.topic)


ALL_FAULTS = {
    "broker-kill": BrokerKill,
    "broker-pause": BrokerPause,
    "network-partition": NetworkPartition,
    "consumer-kill": ConsumerKill,
}
