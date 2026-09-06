"""Tests for the fault-injection primitives.

The faults themselves are exercised for real by `chaos.py`, against real containers -- that
is the whole point of them, and mocking Docker there would prove only that the mock
behaves. What is tested here is the surrounding contract: that a fault refuses to
no-op silently, that healing is idempotent and survives a partly-failed inject, and that
`wait_until` times out rather than hanging. Those are the properties that decide whether a
chaos *result* can be trusted, and getting them wrong would produce confident nonsense.
"""

import time

import pytest

from vigil.chaos.faults import (
    ALL_FAULTS,
    BrokerKill,
    BrokerPause,
    ConsumerKill,
    FaultError,
    NetworkPartition,
    wait_until,
)


class FakeProcess:
    def __init__(self, alive: bool = True):
        self._alive = alive
        self.killed = False

    def poll(self):
        return None if self._alive else -9

    def kill(self):
        self.killed = True
        self._alive = False


# --------------------------- wait_until ---------------------------


def test_wait_until_returns_as_soon_as_the_condition_holds():
    started = time.perf_counter()
    elapsed = wait_until(lambda: True, timeout_s=5.0, poll_s=0.01)
    assert elapsed < 1.0
    assert time.perf_counter() - started < 1.0


def test_wait_until_raises_rather_than_hanging_forever():
    # A recovery check that blocks indefinitely would turn a failed chaos run into a hung
    # one, which reports nothing at all.
    with pytest.raises(TimeoutError):
        wait_until(lambda: False, timeout_s=0.3, poll_s=0.05)


def test_wait_until_reports_roughly_how_long_recovery_took():
    calls = {"n": 0}

    def ready():
        calls["n"] += 1
        return calls["n"] >= 3

    elapsed = wait_until(ready, timeout_s=5.0, poll_s=0.05)
    assert elapsed >= 0.05


# --------------------------- the fault contract ---------------------------


def test_every_registered_fault_names_and_describes_itself():
    # The report is only readable if each row says what was actually done.
    for name, cls in ALL_FAULTS.items():
        fault = cls()
        assert fault.name == name
        assert fault.description


def test_killing_a_consumer_that_was_never_supplied_is_an_error_not_a_no_op():
    # A fault that silently does nothing produces a chaos run that reports a pass for a
    # failure that was never injected. That is worse than a failing test.
    with pytest.raises(FaultError, match="no consumer process"):
        ConsumerKill().inject()


def test_killing_an_already_dead_consumer_is_an_error_not_a_no_op():
    fault = ConsumerKill(process=FakeProcess(alive=False))
    with pytest.raises(FaultError, match="already exited"):
        fault.inject()


def test_a_consumer_kill_actually_kills_the_process():
    process = FakeProcess()
    fault = ConsumerKill(process=process)
    fault.inject()
    assert process.killed
    assert fault.healthy()


def test_consumer_kill_does_not_secretly_respawn_on_heal():
    # Restarting is the caller's job. A fault that quietly respawned processes would make
    # the measured recovery time meaningless.
    process = FakeProcess()
    fault = ConsumerKill(process=process)
    fault.inject()
    fault.heal()
    assert process.poll() is not None


def test_healing_a_network_partition_that_was_never_injected_does_nothing():
    # heal() runs in a finally block, so it is called even when inject() failed. It must not
    # then reconnect something it never disconnected.
    fault = NetworkPartition(container="does-not-exist", network="does-not-exist")
    fault.heal()


def test_injecting_against_a_missing_container_fails_loudly():
    for fault in (
        BrokerKill(container="vigil-nonexistent-xyz"),
        BrokerPause(container="vigil-nonexistent-xyz"),
        NetworkPartition(container="vigil-nonexistent-xyz"),
    ):
        with pytest.raises(FaultError):
            fault.inject()


def test_an_unreachable_broker_is_reported_unhealthy_rather_than_raising():
    # healthy() is polled in a loop; raising there would abort the recovery measurement
    # instead of simply meaning "not yet".
    assert BrokerKill(container="vigil-nonexistent-xyz", bootstrap="127.0.0.1:1").healthy() is False
    assert (
        NetworkPartition(container="vigil-nonexistent-xyz", bootstrap="127.0.0.1:1").healthy()
        is False
    )


def test_the_context_manager_heals_even_when_the_body_raises():
    healed = {"n": 0}

    class Recording(ConsumerKill):
        def heal(self):
            healed["n"] += 1

    fault = Recording(process=FakeProcess())
    with pytest.raises(ValueError), fault:
        raise ValueError("boom")
    assert healed["n"] == 1


def test_the_four_fault_modes_cover_distinct_failure_shapes():
    # NFR-7 asks for at least three. These four are deliberately different in kind: a crash,
    # a freeze that keeps sockets open, an unreachable-but-intact broker, and our own
    # consumer dying mid-window.
    assert len(ALL_FAULTS) >= 3
    descriptions = {cls().description for cls in ALL_FAULTS.values()}
    assert len(descriptions) == len(ALL_FAULTS)


def test_a_pause_is_not_the_same_failure_as_a_kill():
    # Freezing keeps TCP connections open, so clients see silence rather than a reset --
    # the shape of a GC pause or a hung disk, where the client's own timeouts decide.
    assert "pause" not in BrokerKill().description.lower()
    assert "resume" in BrokerPause().description.lower()
