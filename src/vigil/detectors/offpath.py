"""Running a detector off the critical path.

ADR-017 splits the latency budget: the cheap detector gates the stream, the foundation
model runs batched behind it and enriches what the hot path already raised. This is the
mechanism.

Windows are handed to a worker thread through a **bounded** queue. Bounded on purpose: if
the model cannot keep up, the correct behaviour is to drop windows and report exactly how
many, not to grow a queue until the process dies. A dropped window is a measurement the
model did not make, and the summary says so -- it is never counted as a window the model
looked at and found normal.

The hot path never blocks on this. If the model is slow, absent, or fails to load, the
stream is unaffected and detection continues on the baseline alone. That is the degradation
mode `docs/ARCHITECTURE.md` section 7 promises, made real rather than asserted.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

from vigil.detectors.base import DetectorScore
from vigil.windows import Window

log = logging.getLogger(__name__)


class OffPathScorer:
    """Batches windows to a detector on a worker thread.

    `on_scores` is called from the worker thread with each batch's results. Keep it cheap
    and thread-safe -- anything slow there becomes the bottleneck the queue exists to
    absorb.
    """

    def __init__(
        self,
        detector,
        on_scores: Callable[[list[DetectorScore]], None],
        *,
        max_batch: int = 32,
        max_wait_s: float = 1.0,
        queue_size: int = 4096,
    ) -> None:
        self.detector = detector
        self.on_scores = on_scores
        self.max_batch = max_batch
        self.max_wait_s = max_wait_s
        self._queue: queue.Queue[Window] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.submitted = 0
        self.dropped = 0
        self.scored = 0
        self.batches = 0
        self.errors = 0
        self.first_error: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="offpath-scorer", daemon=True)
        self._thread.start()

    def submit(self, window: Window) -> bool:
        """Offer a window. Returns False if it was dropped because the worker is behind."""
        try:
            self._queue.put_nowait(window)
            self.submitted += 1
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _drain_batch(self) -> list[Window]:
        """Collect up to max_batch windows, waiting at most max_wait_s for the first."""
        batch: list[Window] = []
        try:
            batch.append(self._queue.get(timeout=self.max_wait_s))
        except queue.Empty:
            return batch
        deadline = time.perf_counter() + self.max_wait_s
        while len(batch) < self.max_batch and time.perf_counter() < deadline:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            batch = self._drain_batch()
            if not batch:
                continue
            try:
                results = self.detector.score_batch(batch)
            except Exception as exc:  # noqa: BLE001 - the hot path must survive this
                self.errors += 1
                if self.first_error is None:
                    self.first_error = f"{type(exc).__name__}: {exc}"
                log.warning("off-path scoring failed for %d windows: %s", len(batch), exc)
                continue
            self.batches += 1
            scores = [s for s in results if s is not None]
            self.scored += len(scores)
            if scores:
                try:
                    self.on_scores(scores)
                except Exception as exc:  # noqa: BLE001 - same reasoning
                    self.errors += 1
                    if self.first_error is None:
                        self.first_error = f"{type(exc).__name__}: {exc}"
                    log.warning("off-path result handler failed: %s", exc)

    def stop(self, timeout_s: float = 30.0) -> None:
        """Signal the worker and wait for the backlog to clear."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            if self._thread.is_alive():
                log.warning(
                    "off-path worker still busy after %.0fs; %d windows unscored",
                    timeout_s,
                    self._queue.qsize(),
                )
            self._thread = None

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    def summary(self) -> str:
        parts = [
            f"submitted {self.submitted:,}",
            f"scored {self.scored:,}",
            f"batches {self.batches:,}",
        ]
        if self.dropped:
            parts.append(f"DROPPED {self.dropped:,} (model could not keep up)")
        if self.errors:
            parts.append(f"errors {self.errors:,} (first: {self.first_error})")
        return " | ".join(parts)
