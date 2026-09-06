"""Fault injection and recovery verification.

Every fault interrupts a real process. A mocked broker outage proves the mock behaves;
killing the container proves the pipeline does.
"""

from vigil.chaos.faults import (
    ALL_FAULTS,
    BrokerKill,
    BrokerPause,
    ConsumerKill,
    Fault,
    FaultError,
    NetworkPartition,
    consumer_group_lag,
    wait_until,
)

__all__ = [
    "ALL_FAULTS",
    "BrokerKill",
    "BrokerPause",
    "ConsumerKill",
    "Fault",
    "FaultError",
    "NetworkPartition",
    "consumer_group_lag",
    "wait_until",
]
