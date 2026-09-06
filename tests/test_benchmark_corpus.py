"""Tests for how the benchmark loads a corpus and accounts for what it drops.

The scoring itself is tested in `test_metrics.py`. What is tested here is the part that
decides which series are in the result at all, because that decision is invisible in an
aggregate and it can quietly change what the number means: truncating long series to keep a
run tractable excludes every series whose labelled anomalies begin past the cut, which biases
the scored corpus toward early-onset anomalies. A benchmark that drops a third of its corpus
without saying so is reporting on a population nobody chose.
"""

import csv

import numpy as np
import pytest

from benchmark import load_series


def write_series(path, points: int, anomaly_at: int | None, features: int = 3):
    rng = np.random.default_rng(7)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([f"f{i}" for i in range(features)] + ["Label"])
        for t in range(points):
            row = list(np.round(rng.normal(size=features), 4))
            label = 1 if anomaly_at is not None and anomaly_at <= t < anomaly_at + 20 else 0
            writer.writerow([*row, label])
    return path


def test_a_series_keeps_its_labels_when_the_cut_falls_after_them(tmp_path):
    path = write_series(tmp_path / "early.csv", points=1000, anomaly_at=100)

    series = load_series(path, max_points=500)

    assert series.points == 500
    assert int(series.labels.sum()) == 20


def test_truncation_can_remove_every_label_a_series_has(tmp_path):
    """The failure mode the skip accounting exists to surface."""
    path = write_series(tmp_path / "late.csv", points=1000, anomaly_at=800)

    series = load_series(path, max_points=500)

    assert series.points == 500
    assert int(series.labels.sum()) == 0


def test_no_cut_keeps_the_whole_series(tmp_path):
    path = write_series(tmp_path / "whole.csv", points=1000, anomaly_at=800)

    series = load_series(path, max_points=0)

    assert series.points == 1000
    assert int(series.labels.sum()) == 20


def test_a_file_without_a_label_column_is_rejected_rather_than_guessed(tmp_path):
    path = tmp_path / "unlabelled.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["f0", "f1"])
        writer.writerow([1.0, 2.0])

    with pytest.raises(ValueError, match="Label"):
        load_series(path)


def test_feature_capping_keeps_the_most_variable_columns(tmp_path):
    """Truncating in file order would hand the detectors an arbitrary handicap."""
    path = tmp_path / "wide.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["flat", "loud", "quiet", "Label"])
        for t in range(50):
            writer.writerow([1.0, 100.0 * (t % 7), 0.01 * (t % 3), 0])

    series = load_series(path, max_features=2)

    assert series.features == 2
    # The constant column carries no signal for any detector, so it is the one dropped.
    assert series.values[:, 0].std() > 0
    assert series.values[:, 1].std() > 0
