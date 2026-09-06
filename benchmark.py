"""Benchmark the detectors against each other on TSB-AD-M, and report where each loses.

The live solar feed is unlabelled by construction, so precision and recall cannot be
computed on it. This is where they can: 200 labelled multivariate series, fetched by
`scripts/fetch_tsb_ad.py`.

**The same detector code that runs on the stream runs here.** `vigil.detectors.zscore` and
`vigil.detectors.foundation` are driven over each series through the same `WindowDetector`
interface, with the same window geometry. A benchmark that scored a reimplementation would
measure the reimplementation.

**Scored at window resolution, and that is a real caveat.** These detectors emit one score
per window, not per point. Spreading a window score across its points -- the first thing
tried here -- produces plateaus of identical values, so a point-level alarm budget picks
arbitrary points from inside a plateau and the measured F1 collapses to zero for reasons
that have nothing to do with detection quality. So each window is scored once, against a
window label that is 1 if the window contains any labelled point.

This is **not** point-adjustment: point-adjustment inflates the detector's credit by
crediting a whole event to one hit, whereas here detector and labels are at the same
resolution and a detector firing on a clean window is penalised exactly as it should be. But
it is a **coarser task** than point-level TSB-AD scoring, so these numbers are not comparable
to point-level TSB-AD leaderboards. They were not comparable anyway -- see the
point-adjustment refusal in ADR-013 -- and what they are for is comparing our two detectors
against each other under identical conditions.

**Multivariate series, univariate detectors.** TSB-AD-M's series have many feature columns.
The detectors score one channel at a time, so each column is scored independently and the
window score is the **maximum across columns** -- an anomaly in any feature is an anomaly in
the series. That is a real modelling choice with a real cost: it cannot detect an anomaly
that exists only in the *correlation* between features, where each column alone looks
normal. Such anomalies are in this corpus, and this is stated in `docs/EVALUATION.md`
rather than left for a reader to discover.

**Reporting obligation.** Where a detector loses is reported with the same prominence as
where it wins, per-series and not only in aggregate. A mean over 200 series hides exactly
the regime boundary that is worth knowing.

    python benchmark.py --limit 20
    python benchmark.py --report-json docs/results/benchmark.json
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from vigil.detectors.base import WindowDetector
from vigil.detectors.zscore import RollingZScoreDetector
from vigil.evaluation.metrics import SeriesScore, score_series
from vigil.readings import Reading
from vigil.windows import SlidingWindowAssigner

DATASETS = Path(__file__).resolve().parent / "Datasets" / "TSB-AD-M"


@dataclass
class Series:
    name: str
    values: np.ndarray  # (points, features)
    labels: np.ndarray  # (points,)

    @property
    def points(self) -> int:
        return int(self.values.shape[0])

    @property
    def features(self) -> int:
        return int(self.values.shape[1])


def load_series(path: Path, max_points: int = 0, max_features: int = 0) -> Series:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)
    if header[-1] != "Label":
        raise ValueError(f"{path.name}: expected a trailing Label column, found {header[-1]!r}")

    data = np.array(rows, dtype=np.float64)
    if max_points and data.shape[0] > max_points:
        data = data[:max_points]
    values, labels = data[:, :-1], data[:, -1].astype(np.int8)
    if max_features and values.shape[1] > max_features:
        # Keep the most variable columns: a constant column carries no signal for any
        # detector here, and truncating in file order would be an arbitrary handicap.
        keep = np.argsort(-values.std(axis=0))[:max_features]
        values = values[:, np.sort(keep)]
    return Series(name=path.name, values=values, labels=labels)


def score_column(
    detector: WindowDetector,
    column: np.ndarray,
    *,
    window_points: int,
    slide_points: int,
    min_points: int,
) -> dict[int, float]:
    """Run a windowed detector over one column; returns window_start -> score.

    Window resolution, not point resolution. Spreading each score across its window's points
    creates plateaus of identical values, and a point-level alarm budget then selects
    arbitrary points from inside one -- which drove measured F1 to zero for reasons unrelated
    to detection.
    """
    assigner = SlidingWindowAssigner(
        size_ms=window_points,
        slide_ms=slide_points,
        allowed_lateness_ms=0,
        min_points=min_points,
    )
    closed = []
    for i, value in enumerate(column):
        closed.extend(
            assigner.add(Reading(channel="s", seq=i + 1, event_ts_ms=i, value=float(value)))
        )
    closed.extend(assigner.close_all())

    out: dict[int, float] = {}
    for window in sorted(closed, key=lambda w: w.start_ms):
        score = detector.score(window)
        detector.observe(window)
        if score is not None:
            out[window.start_ms] = score.score
    return out


def window_labels(labels: np.ndarray, starts: list[int], window_points: int) -> np.ndarray:
    """A window is anomalous if it contains any labelled point."""
    n = labels.size
    out = np.zeros(len(starts), dtype=np.int8)
    for i, start in enumerate(starts):
        lo, hi = max(0, start), min(n, start + window_points)
        if hi > lo and labels[lo:hi].any():
            out[i] = 1
    return out


def score_with(
    make_detector,
    series: Series,
    *,
    window_points: int,
    slide_points: int,
    min_points: int,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Score every column, fold to one score per window by taking the maximum.

    Returns (window scores, window labels, seconds).
    """
    started = time.perf_counter()
    per_window: dict[int, float] = {}
    for feature in range(series.features):
        column = series.values[:, feature]
        if column.std() == 0:
            continue  # a constant column has no scale; every detector abstains on it
        scores = score_column(
            make_detector(),
            column,
            window_points=window_points,
            slide_points=slide_points,
            min_points=min_points,
        )
        for start, value in scores.items():
            if value > per_window.get(start, -np.inf):
                per_window[start] = value

    starts = sorted(per_window)
    scores = np.array([per_window[s] for s in starts], dtype=np.float64)
    labels = window_labels(series.labels, starts, window_points)
    return scores, labels, time.perf_counter() - started


@dataclass
class Comparison:
    series: str
    points: int
    features: int
    anomaly_rate: float
    results: dict[str, SeriesScore]
    seconds: dict[str, float]

    def winner(self, metric: str = "auc_pr") -> str:
        return max(self.results, key=lambda d: getattr(self.results[d], metric))

    def margin(self, metric: str = "auc_pr") -> float:
        values = sorted(getattr(r, metric) for r in self.results.values())
        return values[-1] - values[0] if len(values) > 1 else 0.0


def build_detectors(args) -> dict:
    detectors = {"zscore": lambda: RollingZScoreDetector(warmup_samples=args.warmup)}
    if not args.no_foundation_model:
        from vigil.detectors.foundation import ChronosResidualDetector, FoundationModelUnavailable

        probe = ChronosResidualDetector(
            args.foundation_model, bucket_ms=1, context_buckets=args.context, min_context_buckets=32
        )
        try:
            probe.load()
        except FoundationModelUnavailable as exc:
            print(
                f"foundation model unavailable, benchmarking the baseline alone: {exc}",
                file=sys.stderr,
            )
        else:
            # One loaded pipeline, shared. Reloading per series would dominate the runtime
            # and measure model loading rather than detection.
            def make_chronos():
                fresh = ChronosResidualDetector(
                    args.foundation_model,
                    bucket_ms=1,
                    context_buckets=args.context,
                    min_context_buckets=32,
                )
                fresh._pipeline = probe._pipeline
                fresh._torch = probe._torch
                return fresh

            detectors[probe.name] = make_chronos
    return detectors


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not DATASETS.exists():
        print(f"{DATASETS} not found; run scripts/fetch_tsb_ad.py first", file=sys.stderr)
        return 1

    paths = sorted(DATASETS.glob("*.csv"))
    if args.match:
        paths = [p for p in paths if args.match.lower() in p.name.lower()]
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print("no series matched", file=sys.stderr)
        return 1

    detectors = build_detectors(args)
    print(
        f"benchmarking {list(detectors)} over {len(paths)} series "
        f"(windows {args.window} points, slide {args.slide})",
        flush=True,
    )

    comparisons: list[Comparison] = []
    # Why a series was dropped is part of the result, not noise before it. Truncating long
    # series to keep the run tractable silently excludes every series whose labelled
    # anomalies begin past the cut, which biases the corpus toward early-onset anomalies --
    # so the count and the reason are reported with the aggregate rather than only scrolling
    # past in the log.
    skipped: dict[str, list[str]] = {"unreadable": [], "no_labels_in_window": [], "unscorable": []}
    for i, path in enumerate(paths, start=1):
        try:
            series = load_series(path, max_points=args.max_points, max_features=args.max_features)
        except (ValueError, IndexError) as exc:
            print(f"  [{i}/{len(paths)}] {path.name}: unreadable ({exc}), skipped", flush=True)
            skipped["unreadable"].append(path.name)
            continue
        if int(series.labels.sum()) == 0:
            print(f"  [{i}/{len(paths)}] {path.name}: no labelled anomalies, skipped", flush=True)
            skipped["no_labels_in_window"].append(path.name)
            continue

        results, seconds, window_counts = {}, {}, {}
        for name, make in detectors.items():
            scores, wlabels, elapsed = score_with(
                make,
                series,
                window_points=args.window,
                slide_points=args.slide,
                min_points=args.min_points,
            )
            if scores.size == 0 or int(wlabels.sum()) == 0:
                continue
            results[name] = score_series(path.name, name, scores, wlabels)
            seconds[name] = elapsed
            window_counts[name] = int(scores.size)

        if len(results) < len(detectors):
            print(
                f"  [{i}/{len(paths)}] {path.name}: a detector produced no scorable windows, "
                f"skipped so the comparison stays like-for-like",
                flush=True,
            )
            skipped["unscorable"].append(path.name)
            continue

        comparison = Comparison(
            series=path.name,
            points=series.points,
            features=series.features,
            anomaly_rate=float(series.labels.mean()),
            results=results,
            seconds=seconds,
        )
        comparisons.append(comparison)
        summary = "  ".join(
            f"{name} AUC-PR {r.auc_pr:.3f} F1 {r.at_budget.f1:.3f}" for name, r in results.items()
        )
        print(
            f"  [{i}/{len(paths)}] {path.name[:46]:<46} "
            f"{series.points:>7,}pt {series.features:>3}f "
            f"{comparison.anomaly_rate:>6.2%}  {summary}",
            flush=True,
        )

    if not comparisons:
        print("nothing scored", file=sys.stderr)
        return 1

    report(comparisons, list(detectors), skipped, len(paths), args.max_points)

    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(
            json.dumps(
                {
                    "config": {
                        "series_attempted": len(paths),
                        "series_scored": len(comparisons),
                        "window_points": args.window,
                        "slide_points": args.slide,
                        "max_points": args.max_points,
                        "max_features": args.max_features,
                    },
                    "skipped": skipped,
                    "series": [
                        {
                            "series": c.series,
                            "points": c.points,
                            "features": c.features,
                            "anomaly_rate": c.anomaly_rate,
                            "seconds": c.seconds,
                            "results": {k: asdict(v) for k, v in c.results.items()},
                        }
                        for c in comparisons
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nreport written to {args.report_json}", flush=True)
    return 0


def report(
    comparisons: list[Comparison],
    names: list[str],
    skipped: dict[str, list[str]] | None = None,
    attempted: int = 0,
    max_points: int = 0,
) -> None:
    print(f"\n{'=' * 92}\nAGGREGATE over {len(comparisons)} series\n{'=' * 92}", flush=True)
    if skipped and attempted:
        dropped = sum(len(v) for v in skipped.values())
        detail = ", ".join(f"{k}={len(v)}" for k, v in skipped.items() if v)
        print(
            f"scored {len(comparisons)} of {attempted} series; {dropped} dropped"
            + (f" ({detail})" if detail else ""),
            flush=True,
        )
        if skipped["no_labels_in_window"] and max_points:
            print(
                f"  the {len(skipped['no_labels_in_window'])} with no labels in window are an "
                f"artefact of --max-points {max_points:,}: their labelled anomalies begin past "
                f"the cut, so the scored corpus is biased toward early-onset anomalies.",
                flush=True,
            )
    print(f"{'detector':<22} {'AUC-PR':>16} {'F1 @ budget':>16} {'precision':>12} {'recall':>10}")
    for name in names:
        rows = [c.results[name] for c in comparisons if name in c.results]
        if not rows:
            continue
        print(
            f"{name:<22} "
            f"{statistics.median(r.auc_pr for r in rows):>8.3f} med "
            f"{statistics.fmean(r.auc_pr for r in rows):>6.3f} "
            f"{statistics.fmean(r.at_budget.f1 for r in rows):>15.3f} "
            f"{statistics.fmean(r.at_budget.precision for r in rows):>12.3f} "
            f"{statistics.fmean(r.at_budget.recall for r in rows):>10.3f}",
            flush=True,
        )
    print(
        "\nmedian is reported beside the mean because a mean over 200 series hides the "
        "regime boundary\nthat is the interesting part.",
        flush=True,
    )

    if len(names) < 2:
        return

    a, b = names[0], names[1]
    wins_a = [c for c in comparisons if c.results[a].auc_pr > c.results[b].auc_pr]
    wins_b = [c for c in comparisons if c.results[b].auc_pr > c.results[a].auc_pr]
    print(f"\n{'=' * 92}\nHEAD TO HEAD: {a} vs {b} (AUC-PR)\n{'=' * 92}", flush=True)
    print(
        f"{a} wins {len(wins_a)} | {b} wins {len(wins_b)} | ties "
        f"{len(comparisons) - len(wins_a) - len(wins_b)}",
        flush=True,
    )

    for label, winners, loser in ((a, wins_a, b), (b, wins_b, a)):
        if not winners:
            continue
        worst = sorted(winners, key=lambda c: -c.margin())[:5]
        print(f"\nWhere {loser} loses worst to {label}:", flush=True)
        for c in worst:
            print(
                f"  {c.series[:56]:<56} {c.anomaly_rate:>6.2%} anomalous | "
                f"{label} {c.results[label].auc_pr:.3f} vs {loser} {c.results[loser].auc_pr:.3f} "
                f"(margin {c.margin():.3f})",
                flush=True,
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--limit", type=int, default=0, help="score only the first N series")
    p.add_argument("--match", default=None, help="only series whose filename contains this")
    p.add_argument("--window", type=int, default=100, help="window width in points")
    p.add_argument("--slide", type=int, default=50, help="window slide in points")
    p.add_argument("--min-points", type=int, default=20)
    p.add_argument("--warmup", type=int, default=200, help="z-score warmup samples")
    p.add_argument("--context", type=int, default=256, help="foundation-model context points")
    p.add_argument("--max-points", type=int, default=20_000, help="truncate long series; 0 for all")
    p.add_argument(
        "--max-features", type=int, default=8, help="keep the N most variable; 0 for all"
    )
    p.add_argument("--foundation-model", default="amazon/chronos-bolt-tiny")
    p.add_argument("--no-foundation-model", action="store_true")
    p.add_argument("--report-json", type=Path, default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
