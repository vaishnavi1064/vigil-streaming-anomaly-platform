"""Context-conditioned detection: the core contribution.

Detection scores a window; conditioning decides what that score *means* given what else was
happening. The mechanism is one join and one policy, and both are here.
"""

from vigil.conditioning.barrier import CorroborationBarrier, HeldEpisode
from vigil.conditioning.persistence import EpisodePersistence, PersistenceIndex
from vigil.conditioning.policy import (
    Attribution,
    ConditioningPolicy,
    ConditioningThresholds,
    FlaggedWindowIndex,
    Verdict,
)
from vigil.conditioning.second_opinion import SecondOpinion, SecondOpinionIndex
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
    "CorroborationBarrier",
    "EpisodePersistence",
    "FlaggedWindowIndex",
    "HeldEpisode",
    "KafkaContextSource",
    "PersistenceIndex",
    "SecondOpinion",
    "SecondOpinionIndex",
    "SignalLookup",
    "SignalWindow",
    "StaticContextSource",
    "Verdict",
]
