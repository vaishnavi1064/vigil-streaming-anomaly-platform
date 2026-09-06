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
    # A dead consumer is precisely the disruption the suite needs to observe, so healthy()
    # must report False here. Reporting True would let the scenario claim a pass without
    # anything having broken.
    assert fault.healthy() is False


def test_consumer_kill_without_a_restart_factory_does_not_respawn():
    # The fault does not know how the consumer is launched; hard-coding that would couple it
    # to one runner.
    process = FakeProcess()
    fault = ConsumerKill(process=process)
    fault.inject()
    fault.heal()
    assert process.poll() is not None


def test_healing_restarts_the_consumer_through_the_supplied_factory():
    process = FakeProcess()
    restarted = FakeProcess()
    fault = ConsumerKill(process=process, restart=lambda: restarted)
    fault.inject()
    fault.heal()
    assert fault._restarted is restarted


def test_healing_twice_does_not_start_a_second_consumer():
    # heal() runs both explicitly and again in the cleanup finally block.
    starts = {"n": 0}

    def factory():
        starts["n"] += 1
        return FakeProcess()

    fault = ConsumerKill(process=FakeProcess(), restart=factory)
    fault.inject()
    fault.heal()
    fault.heal()
    assert starts["n"] == 1


def test_a_restarted_consumer_that_dies_again_is_not_healthy():
    dead = FakeProcess(alive=False)
    fault = ConsumerKill(process=FakeProcess(), restart=lambda: dead)
    fault.inject()
    fault.heal()
    assert fault.healthy() is False


def test_recovery_means_caught_up_not_merely_running():
    # A restarted consumer that never catches up has not recovered, and a liveness-only
    # check would happily report that it had. With no group or topic configured the lag is
    # unknown, and unknown must not be treated as zero.
    fault = ConsumerKill(process=FakeProcess(), restart=FakeProcess)
    fault.inject()
    fault.heal()
    assert fault.group == ""
    assert fault.healthy() is False


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


def test_the_flink_fault_is_registered_and_describes_itself():
    from vigil.chaos.faults import FlinkTaskManagerKill

    fault = FlinkTaskManagerKill()
    assert ALL_FAULTS["flink-taskmanager-kill"] is FlinkTaskManagerKill
    assert "TaskManager" in fault.description


def test_the_flink_fault_refuses_to_inject_when_the_cluster_is_not_up():
    from vigil.chaos.faults import FlinkTaskManagerKill

    fault = FlinkTaskManagerKill(container="vigil-flink-nonexistent-xyz")
    with pytest.raises(FaultError, match="flink profile"):
        fault.inject()


def test_an_unreachable_jobmanager_is_reported_unhealthy_rather_than_raising():
    from vigil.chaos.faults import FlinkTaskManagerKill

    fault = FlinkTaskManagerKill(
        container="vigil-flink-nonexistent-xyz", jobmanager_url="http://127.0.0.1:1"
    )
    assert fault.healthy() is False
