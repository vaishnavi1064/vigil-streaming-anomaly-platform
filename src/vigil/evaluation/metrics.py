"""Threshold-independent metrics for anomaly detection, and the ones deliberately refused.

The refusals matter as much as the implementations.

**Point-adjusted F1 is not here and will not be added.** It marks an entire ground-truth
range as detected if the detector fires even once inside it, which lets a near-random scorer
post a high F1 -- and that specific inflation is the reliability problem TSB-AD was built to
expose. Adopting the benchmark and then scoring with the metric it exists to discredit would
be self-defeating (ADR-013). Our numbers will therefore look worse than papers that
point-adjust, and are not comparable to them.

**Accuracy and ROC-AUC are not reported.** Anomalies are well under 1% of points, so both
are dominated by the negative class: a detector that says "normal" forever scores above 99%.

**VUS-PR is not here either, and that is a change from ADR-013.** A faithful implementation
weights a decayed buffer region on both the true-positive and false-positive sides; the
obvious shortcut -- dilating the label set and averaging the area over tolerances -- turns
out to penalise a *perfect* point detector, which scored 0.48 on a case where it had found
the event exactly. A metric that punishes precision for not padding its answers is worse
than no tolerance metric at all, and shipping it under the name VUS-PR would have been a
claim we could not defend. It was implemented, measured, found wrong, and removed
(ADR-023). What replaces it is below.

What is here:

  * **AUC-PR**, point-wise. The informative curve at these base rates, fully standard.
  * **Precision, recall and F1 at a matched alarm budget** -- the only fair way to compare
    detectors whose scores are on different scales. Fix the number of alarms an operator
    would receive, then ask who spent them better.
  * **Event-level detection rate**, reported *always and only* next to point recall. On its
    own this is point-adjustment by another name; the reason it is here is that the **gap**
    between the two is informative: a high event rate with low point recall means the
    detector is clipping the edges of events rather than missing them.
  * **Detection latency** -- how far into an event the detector first fires. This is what
    the tolerance metric was really wanted for, expressed as a number with a unit instead of
    an integral over an arbitrary range.

Implemented here rather than taken from a library because the point is to know exactly what
is being computed, and because the obvious library's default is point-adjusted scoring.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def _as_arrays(scores, labels) -> tuple[np.ndarray, np.ndarray]:
    s = np.asarray(scores, dtype=np.float64)
    y = np.asarray(labels, dtype=np.int8)
    if s.shape != y.shape:
        raise ValueError(f"scores {s.shape} and labels {y.shape} must be the same length")
    # A detector with no opinion must not be read as "confidently normal", but a NaN cannot
    # be ranked. Treating it as the lowest possible score is the least-wrong option and is
    # stated rather than silently done.
    s = np.nan_to_num(s, nan=-np.inf, posinf=np.finfo(np.float64).max)
    return s, y


def precision_recall_curve(scores, labels) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Point-wise PR curve, computed by sweeping every distinct score as a threshold."""
    s, y = _as_arrays(scores, labels)
    order = np.argsort(-s, kind="mergesort")
    y_sorted = y[order]
    s_sorted = s[order]

    tp = np.cumsum(y_sorted == 1)
    fp = np.cumsum(y_sorted == 0)
    positives = int((y == 1).sum())
    if positives == 0:
        return np.array([1.0]), np.array([0.0]), np.array([np.inf])

    # Only threshold where the score actually changes; otherwise ties are split arbitrarily
    # and the curve gets credit for an ordering it did not produce.
    distinct = np.where(np.diff(s_sorted))[0]
    idx = np.r_[distinct, s_sorted.size - 1]

    precision = tp[idx] / np.maximum(tp[idx] + fp[idx], 1)
    recall = tp[idx] / positives
    return precision, recall, s_sorted[idx]


def auc_pr(scores, labels) -> float:
    """Area under the point-wise PR curve, by the step rule.

    Trapezoidal interpolation between PR points assumes a linear path the detector never
    took and is optimistically biased; the step rule is what average precision uses.
    """
    precision, recall, _ = precision_recall_curve(scores, labels)
    if recall.size == 0:
        return 0.0
    recall_prev = np.r_[0.0, recall[:-1]]
    return float(np.sum((recall - recall_prev) * precision))


def roc_auc(scores, labels) -> float:
    """Point-wise ROC-AUC. An input to the tolerant measure only; never reported alone,
    because at these base rates it is dominated by the negative class."""
    s, y = _as_arrays(scores, labels)
    positives = int((y == 1).sum())
    negatives = int((y == 0).sum())
    if positives == 0 or negatives == 0:
        return 0.0
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(1, s.size + 1)
    # Average ranks within ties, or a detector that emits one constant score would score
    # above 0.5 purely on tie-breaking order.
    _, inverse, counts = np.unique(s, return_inverse=True, return_counts=True)
    tie_sums = np.zeros(counts.size)
    np.add.at(tie_sums, inverse, ranks)
    ranks = (tie_sums / counts)[inverse]
    positive_rank_sum = ranks[y == 1].sum()
    return float((positive_rank_sum - positives * (positives + 1) / 2) / (positives * negatives))


def alarmed_mask(scores, budget: int) -> np.ndarray:
    """The top `budget` points, as a boolean mask.

    Used by both the budgeted score and the delay measurement so they cannot disagree.
    Re-deriving the mask by thresholding with `>=` is wrong when scores tie: a detector
    emitting one constant value would alarm on every point rather than on `budget` of them,
    and would then appear to detect every event instantly.
    """
    s = np.asarray(scores, dtype=np.float64)
    s = np.nan_to_num(s, nan=-np.inf, posinf=np.finfo(np.float64).max)
    budget = max(1, min(budget, s.size))
    mask = np.zeros(s.size, dtype=bool)
    mask[np.argsort(-s, kind="mergesort")[:budget]] = True
    return mask


def detection_delays(scores, labels, budget: int) -> list[int | None]:
    """For each labelled event, how many points elapsed before the detector first alarmed.

    None means the event was never alarmed within the budget. Reported as a distribution
    rather than a mean, because one missed event and one instant detection do not average
    into "detected halfway".
    """
    _, y = _as_arrays(scores, labels)
    mask = alarmed_mask(scores, budget)
    out: list[int | None] = []
    for start, end in labelled_regions(y):
        fired = np.where(mask[start : end + 1])[0]
        out.append(int(fired[0]) if fired.size else None)
    return out


@dataclass(frozen=True)
class BudgetedScore:
    """Precision, recall and F1 at a fixed number of alarms."""

    budget: int
    threshold: float
    precision: float
    recall: float
    f1: float
    detected_regions: int
    total_regions: int

    @property
    def region_recall(self) -> float:
        """Fraction of labelled regions touched at least once.

        Reported *alongside* point recall, never instead of it. On its own this is
        point-adjustment by another name, and it is included only so the gap between the two
        is visible -- a large gap means the detector is clipping the edges of events.
        """
        return self.detected_regions / self.total_regions if self.total_regions else 0.0


def labelled_regions(labels) -> list[tuple[int, int]]:
    """Contiguous runs of label 1, as inclusive index ranges."""
    y = np.asarray(labels, dtype=np.int8)
    if y.size == 0:
        return []
    padded = np.r_[0, y, 0]
    starts = np.where(np.diff(padded) == 1)[0]
    ends = np.where(np.diff(padded) == -1)[0] - 1
    return list(zip(starts.tolist(), ends.tolist(), strict=True))


def score_at_budget(scores, labels, budget: int) -> BudgetedScore:
    """Take the top `budget` points as alarms and score them.

    The matched alarm budget is what makes two detectors on different scales comparable:
    fix what an operator would receive, then ask who spent it better.
    """
    s, y = _as_arrays(scores, labels)
    budget = max(1, min(budget, s.size))
    order = np.argsort(-s, kind="mergesort")
    alarmed = alarmed_mask(s, budget)
    threshold = float(s[order[budget - 1]])

    tp = int(np.sum(alarmed & (y == 1)))
    fp = int(np.sum(alarmed & (y == 0)))
    positives = int((y == 1).sum())

    precision = tp / max(tp + fp, 1)
    recall = tp / positives if positives else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    regions = labelled_regions(y)
    touched = sum(1 for start, end in regions if alarmed[start : end + 1].any())

    return BudgetedScore(
        budget=budget,
        threshold=threshold,
        precision=precision,
        recall=recall,
        f1=f1,
        detected_regions=touched,
        total_regions=len(regions),
    )


@dataclass(frozen=True)
class SeriesScore:
    """Every metric for one detector on one series."""

    series: str
    detector: str
    points: int
    anomaly_points: int
    anomaly_regions: int
    auc_pr: float
    at_budget: BudgetedScore
    # None entries are events never detected at the budget threshold. Kept as a list rather
    # than a mean so a miss cannot average away against a fast detection.
    detection_delays: tuple[int | None, ...] = ()

    @property
    def anomaly_rate(self) -> float:
        return self.anomaly_points / self.points if self.points else 0.0

    @property
    def events_missed(self) -> int:
        return sum(1 for d in self.detection_delays if d is None)

    @property
    def median_detection_delay(self) -> float | None:
        """Median points-to-first-alarm over the events that were detected at all.

        None if nothing was detected. Missed events are excluded rather than counted as an
        infinite delay, and `events_missed` is reported beside it so the exclusion is
        visible rather than flattering.
        """
        seen = sorted(d for d in self.detection_delays if d is not None)
        return float(np.median(seen)) if seen else None


def score_series(
    series: str,
    detector: str,
    scores,
    labels,
    *,
    budget_multiple: float = 1.0,
) -> SeriesScore:
    """Score one detector on one series.

    The alarm budget defaults to the number of anomalous points, so a detector is given
    exactly as many alarms as there are anomalies and cannot buy recall with volume.
    """
    s, y = _as_arrays(scores, labels)
    positives = int((y == 1).sum())
    budget = max(1, int(positives * budget_multiple))
    budgeted = score_at_budget(s, y, budget)
    return SeriesScore(
        series=series,
        detector=detector,
        points=int(s.size),
        anomaly_points=positives,
        anomaly_regions=len(labelled_regions(y)),
        auc_pr=auc_pr(s, y),
        at_budget=budgeted,
        detection_delays=tuple(detection_delays(s, y, budget)),
    )
