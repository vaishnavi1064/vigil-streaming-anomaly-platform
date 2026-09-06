"""Scoring runs against ground truth.

Results are pairs, never single numbers: false-positive reduction on its own is maximised by
suppressing everything, so it is always reported next to what it cost in recall (ADR-016).
"""

from vigil.evaluation.paired import (
    GroundTruth,
    ObservedEpisode,
    PairedComparison,
    PairedResult,
    TruthEpisode,
    TruthWindow,
    score_pass,
)

__all__ = [
    "GroundTruth",
    "ObservedEpisode",
    "PairedComparison",
    "PairedResult",
    "TruthEpisode",
    "TruthWindow",
    "score_pass",
]
