"""Detectors: things that turn a window into a score.

Every detector implements `WindowDetector`, so the hot path, the off-critical-path model and
the benchmark harness all drive them identically and their latencies are directly comparable.
"""

from vigil.detectors.base import DetectorScore, WindowDetector
from vigil.detectors.zscore import RollingZScoreDetector, Welford

__all__ = ["DetectorScore", "RollingZScoreDetector", "Welford", "WindowDetector"]
