"""Zero-shot foundation-model detector: Chronos-Bolt forecast residuals.

The spine detector from ADR-001, used strictly zero-shot -- no fine-tuning, no labels. It
is the *other* model in this project; the agent's tool-calling model (Phase 5) is a
different thing entirely and is the one that gets fine-tuned.

**How a forecaster becomes a detector.** The channel's recent history is fed in as context,
the model forecasts the span the window covers, and the window's actual values are compared
against that forecast. The score is the largest normalised residual across the span, where
the scale comes from the model's *own* predicted uncertainty (the 10th-to-90th percentile
spread) rather than from a global constant. That matters: a channel the model considers
volatile gets a correspondingly wider tolerance, so quiet channels are not drowned out by
noisy ones.

**The honest caveat.** Forecast-error and reconstruction-error framings of time-series
anomaly detection are exactly what "When Foundation Models are One-Liners" (2025) argues is
flawed, and Zhou & Yu (ICLR 2025) report limited gains from LLM-style approaches to this
task. That critique is the reason this detector is benchmarked against the z-score baseline
on TSB-AD-M and its losses reported (ADR-013), not the reason to skip building it.

**Why the tiny model.** Measured on this laptop's CPU at batch 32, per window:
chronos-bolt-tiny 0.64 ms, mini 1.15 ms, small 2.95 ms, base 9.32 ms. Per *batch* the base
model takes 298 ms, so the last window in a batch would exceed the 250 ms hot-path budget
on its own -- which is why this detector runs off the critical path regardless of size
(ADR-017), and why tiny is the default.

Leakage is avoided deliberately: the context is drawn strictly from buckets *before* the
window's start. Windows overlap by design, so a naive "everything I have seen so far"
context would include the window being scored and the model would be asked to forecast data
it had already been shown.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass

from vigil.detectors.base import DetectorScore, WindowDetector
from vigil.windows import Window

log = logging.getLogger(__name__)

DEFAULT_MODEL = "amazon/chronos-bolt-tiny"
QUANTILE_LEVELS = [0.1, 0.5, 0.9]
# The 10th-to-90th percentile span of a normal distribution is 2.563 sigma; dividing by it
# converts the model's predicted spread into a sigma-like scale, so this detector's scores
# are on roughly the same footing as the z-score baseline's.
_P10_P90_TO_SIGMA = 2.563


class FoundationModelUnavailable(RuntimeError):
    """torch/chronos are not installed, or the weights could not be fetched."""


@dataclass
class _ChannelHistory:
    """Per-channel bucketed history, bounded, keeping each bucket's index.

    The index is kept rather than only the value because windows overlap: the history
    always runs past the start of the next window to be scored, so the context has to be
    *sliced* at that window's start rather than simply taken as "everything so far".
    Without the index there would be nothing to slice on.
    """

    buckets: deque[tuple[int, float]]
    last_index: int | None = None


class ChronosResidualDetector(WindowDetector):
    """Scores a window by how far it departs from what Chronos forecast for that span.

    Readings are bucketed to a fixed cadence before the model sees them. A window at
    600 events/s across 8 channels holds thousands of points; bucketing gives the model an
    evenly spaced series of a length it was trained on, and makes the context length mean a
    fixed span of wall time rather than a variable one that depends on the ingest rate.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL,
        *,
        bucket_ms: int = 1_000,
        context_buckets: int = 128,
        min_context_buckets: int = 48,
        device: str = "cpu",
        floor_sigma: float = 1e-6,
    ) -> None:
        self.name = model_id.split("/")[-1]
        self.model_id = model_id
        self.bucket_ms = bucket_ms
        self.context_buckets = context_buckets
        self.min_context_buckets = min_context_buckets
        self.device = device
        self.floor_sigma = floor_sigma
        self._history: dict[str, _ChannelHistory] = {}
        self.windows_scored = 0
        self.windows_skipped_cold = 0
        self.batches_run = 0
        self._pipeline = None
        self._torch = None

    # -- model loading ---------------------------------------------------------

    def load(self) -> None:
        """Load the weights. Separate from __init__ so a caller can fail fast and degrade.

        Raises FoundationModelUnavailable rather than propagating an import error, so the
        detector's absence is a handled degradation and not a crash: the hot path must keep
        running when the model path cannot.
        """
        if self._pipeline is not None:
            return
        try:
            import torch
            from chronos import BaseChronosPipeline
        except ImportError as exc:
            raise FoundationModelUnavailable(
                f"chronos/torch not installed ({exc}). Install with "
                f"`pip install -e '.[foundation]'`, or run with --no-foundation-model."
            ) from exc
        try:
            started = time.perf_counter()
            self._pipeline = BaseChronosPipeline.from_pretrained(
                self.model_id, device_map=self.device, torch_dtype=torch.float32
            )
            self._torch = torch
            params = sum(p.numel() for p in self._pipeline.model.parameters())
            log.info(
                "loaded %s (%.1fM params) on %s in %.1fs",
                self.model_id,
                params / 1e6,
                self.device,
                time.perf_counter() - started,
            )
        except Exception as exc:  # noqa: BLE001 - any failure here is the same degradation
            raise FoundationModelUnavailable(
                f"could not load {self.model_id}: {exc}"
            ) from exc

    @property
    def loaded(self) -> bool:
        return self._pipeline is not None

    # -- history ---------------------------------------------------------------

    def _bucketise(self, window: Window) -> list[tuple[int, float]]:
        """Mean value per fixed-width bucket across the window."""
        sums: dict[int, float] = {}
        counts: dict[int, int] = {}
        for ts, value in zip(window.event_ts_ms, window.values, strict=True):
            idx = ts // self.bucket_ms
            sums[idx] = sums.get(idx, 0.0) + value
            counts[idx] = counts.get(idx, 0) + 1
        return [(idx, sums[idx] / counts[idx]) for idx in sorted(sums)]

    def _absorb(self, channel: str, buckets: list[tuple[int, float]]) -> None:
        hist = self._history.get(channel)
        if hist is None:
            # Headroom beyond the context length, because the tail of the deque is the part
            # that gets sliced away when scoring an overlapping window. Without it, a full
            # context would never be available: every slice would cut into the buckets the
            # window itself supplied.
            hist = _ChannelHistory(buckets=deque(maxlen=self.context_buckets * 2))
            self._history[channel] = hist
        for idx, value in buckets:
            # Windows overlap, so the same bucket arrives more than once. Appending only
            # strictly-newer buckets keeps the series free of duplicates.
            if hist.last_index is None or idx > hist.last_index:
                hist.buckets.append((idx, value))
                hist.last_index = idx

    def observe(self, window: Window) -> None:
        """Fold a window into the history.

        Scoring already does this, so the driver must not call it as well -- doing both
        would advance the history past the window and starve the next one of context. It
        exists for the offline benchmark, which scores series one window at a time.
        """
        self._absorb(window.channel, self._bucketise(window))

    # -- scoring ---------------------------------------------------------------

    def _context_for(self, window: Window) -> list[float] | None:
        """The most recent buckets strictly before this window starts.

        Slicing at the window's start is what prevents leakage. Windows overlap by design,
        so the history legitimately extends past this window's start -- those later buckets
        are ones the window itself supplied, and letting the model see them would be asking
        it to forecast data it had already been shown.
        """
        hist = self._history.get(window.channel)
        if hist is None:
            return None
        cutoff = window.start_ms // self.bucket_ms
        context = [value for idx, value in hist.buckets if idx < cutoff]
        if len(context) < self.min_context_buckets:
            return None
        return context[-self.context_buckets :]

    def score(self, window: Window) -> DetectorScore | None:
        return self.score_batch([window])[0]

    def score_batch(self, windows: list[Window]) -> list[DetectorScore | None]:
        """Score several windows in one forward pass.

        Batching is where this detector becomes affordable: measured on this CPU, a single
        window costs 5.5 ms and a batch of 32 costs 20.5 ms -- 0.64 ms each.
        """
        if not self.loaded:
            self.load()

        results: list[DetectorScore | None] = [None] * len(windows)
        contexts: list[list[float]] = []
        actuals: list[list[float]] = []
        slots: list[int] = []

        for i, window in enumerate(windows):
            context = self._context_for(window)
            buckets = self._bucketise(window)
            # Fold the window in immediately after taking its context, so a later window in
            # the same batch is scored against a history that includes this one. Doing it
            # here rather than in the caller also keeps every mutation of the history on
            # this one thread: the hot path submits windows and never touches this state.
            self._absorb(window.channel, buckets)
            if context is None or not buckets:
                self.windows_skipped_cold += 1
                continue
            contexts.append(context)
            actuals.append([v for _, v in buckets])
            slots.append(i)

        if not contexts:
            return results

        horizon = max(len(a) for a in actuals)
        started = time.perf_counter()
        quantiles, _mean = self._pipeline.predict_quantiles(
            inputs=[self._torch.tensor(c, dtype=self._torch.float32) for c in contexts],
            prediction_length=horizon,
            quantile_levels=QUANTILE_LEVELS,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.batches_run += 1
        # Charge each window its share of the batch. Attributing the whole batch to every
        # window would triple-count the cost; attributing none of it would hide it.
        per_window_ms = elapsed_ms / len(contexts)

        q = quantiles.detach().cpu().numpy()
        for row, (slot, actual) in enumerate(zip(slots, actuals, strict=True)):
            window = windows[slot]
            n = min(len(actual), horizon)
            peak = 0.0
            total = 0.0
            for k in range(n):
                low, median, high = float(q[row, k, 0]), float(q[row, k, 1]), float(q[row, k, 2])
                sigma = max((high - low) / _P10_P90_TO_SIGMA, self.floor_sigma)
                residual = abs(actual[k] - median) / sigma
                peak = max(peak, residual)
                total += residual
            if not math.isfinite(peak):
                self.windows_skipped_cold += 1
                continue
            self.windows_scored += 1
            results[slot] = DetectorScore(
                detector=self.name,
                channel=window.channel,
                window_start_ms=window.start_ms,
                window_end_ms=window.end_ms,
                score=peak,
                latency_ms=per_window_ms,
                detail={
                    "peak_residual_sigma": peak,
                    "mean_residual_sigma": total / max(n, 1),
                    "horizon_buckets": n,
                    "context_buckets": len(contexts[row]),
                    "batch_size": len(contexts),
                    "batch_ms": elapsed_ms,
                },
            )
        return results

    @property
    def channels_tracked(self) -> int:
        return len(self._history)
