"""Continuous proof that ingested equals processed, and the health signal that falls out of it.

The harness has two jobs and they reinforce each other: it proves the correctness claim, and
the per-window signal it emits is what the detector conditions on. That is why it is built
before Flink -- it is load-bearing for the core contribution, not just for the guarantee.
"""

from vigil.reconciliation.ledger import (
    ChannelLedger,
    HealthSeverity,
    HealthThresholds,
    PipelineHealth,
    ReconciliationLedger,
)

__all__ = [
    "ChannelLedger",
    "HealthSeverity",
    "HealthThresholds",
    "PipelineHealth",
    "ReconciliationLedger",
]
