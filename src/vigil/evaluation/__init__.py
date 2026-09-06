"""Scoring runs against ground truth.

Results are pairs, never single numbers: false-positive reduction on its own is maximised by
suppressing everything, so it is always reported next to what it cost in recall (ADR-016).
"""

from vigil.evaluation.metrics import (
    SeriesScore,
    auc_pr,
    detection_delays,
    score_at_budget,
    score_series,
)
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
    "SeriesScore",
    "TruthWindow",
    "auc_pr",
    "score_at_budget",
    "score_pass",
    "score_series",
    "detection_delays",
]
