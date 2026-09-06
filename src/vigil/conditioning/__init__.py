"""Context-conditioned detection: the core contribution.

Detection scores a window; conditioning decides what that score *means* given what else was
happening. The mechanism is one join and one policy, and both are here.
"""

from vigil.conditioning.policy import (
    Attribution,
    ConditioningPolicy,
    ConditioningThresholds,
    FlaggedWindowIndex,
    Verdict,
)
from vigil.conditioning.signals import (
    CompositeContextSource,
    ContextSignalSource,
    KafkaContextSource,
    SignalLookup,
    SignalWindow,
    StaticContextSource,
)

__all__ = [
    "Attribution",
    "CompositeContextSource",
    "ConditioningPolicy",
    "ConditioningThresholds",
    "ContextSignalSource",
    "FlaggedWindowIndex",
    "KafkaContextSource",
    "SignalLookup",
    "SignalWindow",
    "StaticContextSource",
    "Verdict",
]
