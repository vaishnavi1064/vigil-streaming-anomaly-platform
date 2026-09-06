"""Tests for the detection metrics.

Two jobs: verify the arithmetic against cases with known answers, and pin the properties
that make these metrics honest -- that a constant scorer cannot beat chance, that a detector
cannot buy recall with alarm volume, and that nothing here quietly point-adjusts.
"""

import numpy as np
import pytest

from vigil.evaluation.metrics import (
    auc_pr,
    detection_delays,
    labelled_regions,
    precision_recall_curve,
    roc_auc,
    score_at_budget,
    score_series,
)


def test_a_perfect_ranking_scores_one():
    scores = [0.1, 0.2, 0.9, 0.95]
    labels = [0, 0, 1, 1]
    assert auc_pr(scores, labels) == pytest.approx(1.0)
    assert roc_auc(scores, labels) == pytest.approx(1.0)


def test_an_inverted_ranking_scores_near_zero():
    scores = [0.95, 0.9, 0.2, 0.1]
    labels = [0, 0, 1, 1]
    assert roc_auc(scores, labels) == pytest.approx(0.0)
    assert auc_pr(scores, labels) < 0.6


def test_a_constant_scorer_cannot_beat_chance():
    # Ties must be ranked at their average, or a detector emitting one number forever scores
    # above 0.5 purely on the order the array happened to be in.
    rng = np.random.default_rng(0)
    labels = (rng.random(2000) < 0.05).astype(int)
    assert roc_auc(np.ones(2000), labels) == pytest.approx(0.5, abs=1e-9)


def test_auc_pr_of_a_random_scorer_approximates_the_base_rate():
    rng = np.random.default_rng(1)
    labels = (rng.random(20_000) < 0.02).astype(int)
    scores = rng.random(20_000)
    assert auc_pr(scores, labels) == pytest.approx(0.02, abs=0.01)


def test_a_series_with_no_anomalies_scores_zero_rather_than_dividing_by_zero():
    assert auc_pr([0.1, 0.2, 0.3], [0, 0, 0]) == 0.0
    assert roc_auc([0.1, 0.2, 0.3], [0, 0, 0]) == 0.0


def test_a_missing_score_is_ranked_lowest_rather_than_treated_as_normal():
    # A detector with no opinion must not be read as confidently normal, and a NaN cannot be
    # ranked at all.
    scores = [float("nan"), 0.9, 0.1]
    labels = [1, 1, 0]
    assert 0.0 < auc_pr(scores, labels) < 1.0


def test_mismatched_lengths_are_rejected_rather_than_silently_truncated():
    with pytest.raises(ValueError, match="same length"):
        auc_pr([0.1, 0.2], [1])


def test_the_pr_curve_only_thresholds_where_the_score_changes():
    # Otherwise ties get split arbitrarily and the curve takes credit for an ordering the
    # detector did not produce.
    precision, recall, thresholds = precision_recall_curve([1.0, 1.0, 1.0, 1.0], [1, 0, 1, 0])
    assert thresholds.size == 1
    assert precision[0] == pytest.approx(0.5)
    assert recall[0] == pytest.approx(1.0)


# ------------------------- detection latency -------------------------


def test_detection_delay_is_points_from_the_start_of_an_event_to_the_first_alarm():
    # This is what the abandoned tolerance metric was really wanted for, expressed as a
    # number with a unit rather than an integral over an arbitrary range (ADR-023).
    labels = np.zeros(200, dtype=int)
    labels[100:120] = 1
    scores = np.zeros(200)
    scores[105:120] = 1.0
    assert detection_delays(scores, labels, budget=int(labels.sum())) == [5]


def test_an_event_never_alarmed_reports_no_delay_rather_than_a_large_one():
    # Counting a miss as a very slow detection would let misses average away against fast
    # detections and turn "never found it" into "found it eventually".
    labels = np.zeros(100, dtype=int)
    labels[40:50] = 1
    assert detection_delays(np.zeros(100), labels, budget=1) == [None]


def test_each_event_gets_its_own_delay():
    labels = np.zeros(300, dtype=int)
    labels[50:60] = 1
    labels[200:210] = 1
    scores = np.zeros(300)
    scores[52:60] = 1.0
    scores[200:210] = 1.0
    assert detection_delays(scores, labels, budget=int(labels.sum())) == [2, 0]


def test_missed_events_are_counted_separately_from_the_median_delay():
    labels = np.zeros(400, dtype=int)
    labels[50:60] = 1
    labels[200:210] = 1
    scores = np.zeros(400)
    scores[50:60] = 5.0
    result = score_series("s", "d", scores, labels)
    assert result.events_missed == 1
    assert result.median_detection_delay == 0.0


def test_a_series_with_nothing_detected_has_no_median_delay():
    labels = np.zeros(100, dtype=int)
    labels[40:50] = 1
    result = score_series("s", "d", np.zeros(100), labels)
    assert result.median_detection_delay is None


# ------------------------- alarm budget -------------------------


def test_a_matched_budget_stops_a_detector_buying_recall_with_volume():
    # The whole point: fix what an operator receives, then ask who spent it better. A
    # detector that alarms on everything gets no credit for catching everything.
    labels = np.zeros(1000, dtype=int)
    labels[500:510] = 1
    alarm_everything = np.ones(1000)
    scored = score_at_budget(alarm_everything, labels, budget=10)
    assert scored.recall < 1.0
    assert scored.precision < 0.5


def test_the_budget_is_capped_at_the_series_length():
    labels = np.array([0, 1, 0])
    assert score_at_budget([0.1, 0.9, 0.2], labels, budget=10_000).budget == 3


def test_a_budget_below_one_is_raised_to_one():
    labels = np.array([0, 1, 0])
    assert score_at_budget([0.1, 0.9, 0.2], labels, budget=0).budget == 1


def test_a_perfect_detector_at_a_matched_budget_scores_one():
    labels = np.zeros(500, dtype=int)
    labels[100:110] = 1
    scored = score_at_budget(labels.astype(float), labels, budget=10)
    assert scored.precision == pytest.approx(1.0)
    assert scored.recall == pytest.approx(1.0)
    assert scored.f1 == pytest.approx(1.0)


# ------------------------- regions, and the point-adjustment boundary -------------------------


def test_contiguous_labelled_runs_are_found():
    assert labelled_regions([0, 1, 1, 0, 0, 1, 0]) == [(1, 2), (5, 5)]


def test_a_run_touching_the_end_is_still_a_region():
    assert labelled_regions([0, 0, 1, 1]) == [(2, 3)]


def test_an_all_normal_series_has_no_regions():
    assert labelled_regions([0, 0, 0]) == []


def test_region_recall_is_reported_next_to_point_recall_not_instead_of_it():
    # Region recall alone IS point-adjustment by another name. It is exposed only so the gap
    # between the two is visible: a large gap means the detector is clipping event edges.
    labels = np.zeros(400, dtype=int)
    labels[100:150] = 1
    scores = np.zeros(400)
    scores[100:102] = 1.0  # fires twice inside a 50-point event

    scored = score_at_budget(scores, labels, budget=2)
    assert scored.region_recall == 1.0, "the single region was touched"
    assert scored.recall == pytest.approx(2 / 50), "but only 2 of 50 points were caught"
    assert scored.recall < scored.region_recall


def test_a_near_random_detector_cannot_post_a_high_f1_here():
    # The specific inflation TSB-AD exists to expose: under point-adjustment, firing once
    # inside each event would score near 1.0. It must not here.
    rng = np.random.default_rng(7)
    labels = np.zeros(5000, dtype=int)
    for start in range(200, 5000, 500):
        labels[start : start + 30] = 1
    scores = rng.random(5000)
    scored = score_at_budget(scores, labels, budget=int(labels.sum()))
    assert scored.f1 < 0.2


# ------------------------- the full per-series result -------------------------


def test_scoring_a_series_reports_every_metric_and_the_shape_of_the_data():
    rng = np.random.default_rng(11)
    labels = np.zeros(2000, dtype=int)
    labels[500:520] = 1
    labels[1200:1230] = 1
    scores = rng.random(2000)
    scores[500:520] += 2.0
    scores[1200:1230] += 2.0

    result = score_series("demo.csv", "zscore", scores, labels)
    assert result.series == "demo.csv"
    assert result.detector == "zscore"
    assert result.points == 2000
    assert result.anomaly_points == 50
    assert result.anomaly_regions == 2
    assert result.anomaly_rate == pytest.approx(0.025)
    assert result.auc_pr > 0.8
    assert len(result.detection_delays) == 2
    assert result.at_budget.budget == 50


def test_the_default_budget_equals_the_number_of_anomalous_points():
    labels = np.zeros(300, dtype=int)
    labels[10:25] = 1
    result = score_series("s", "d", np.random.default_rng(0).random(300), labels)
    assert result.at_budget.budget == 15


def test_scoring_is_deterministic_for_the_same_input():
    rng = np.random.default_rng(5)
    labels = (rng.random(1000) < 0.05).astype(int)
    scores = rng.random(1000)
    first = score_series("s", "d", scores, labels)
    second = score_series("s", "d", scores, labels)
    assert first.auc_pr == second.auc_pr
    assert first.at_budget.f1 == second.at_budget.f1
