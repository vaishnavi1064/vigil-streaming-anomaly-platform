"""Explaining flagged episodes without letting the explainer touch the stream.

Same shape as `vigil.detectors.offpath` and for the same reason: a bounded queue and a worker
thread, so a slow or unreachable endpoint costs latency in the explanation and nothing in
detection. Bounded rather than unbounded because the correct behaviour when explanations
cannot keep up is to drop them and say how many, not to grow a queue until the process dies.

The asymmetry with the foundation model is deliberate. That runs on every window and batches
for throughput; this runs on episodes, which are rare by construction, so it takes them one
at a time and spends its effort on not blocking anything.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

from vigil.episodes import Episode
from vigil.explain.explainer import Explanation, WindowExplainer, explain_episode

log = logging.getLogger(__name__)


@dataclass
class ExplanationRequest:
    episode_id: int
    episode: Episode
    timestamps_ms: list[int]
    values: list[float]


class ExplanationWorker:
    """Renders and explains episodes on a worker thread.

    `on_explained` is called from that thread with (episode_id, Explanation). Keep it cheap
    and thread-safe; it is where the result reaches the store.
    """

    def __init__(
        self,
        explainer: WindowExplainer,
        on_explained: Callable[[int, Explanation], None],
        *,
        max_pending: int = 32,
    ) -> None:
        self.explainer = explainer
        self.on_explained = on_explained
        self.queue: queue.Queue[ExplanationRequest | None] = queue.Queue(maxsize=max_pending)
        self.dropped = 0
        self.submitted = 0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="explain", daemon=True)
        self._thread.start()

    def submit(self, request: ExplanationRequest) -> bool:
        """Queue an episode. Returns False if it was dropped, and never blocks."""
        try:
            self.queue.put_nowait(request)
        except queue.Full:
            self.dropped += 1
            return False
        self.submitted += 1
        return True

    def _run(self) -> None:
        while True:
            request = self.queue.get()
            if request is None:
                return
            try:
                result = explain_episode(
                    self.explainer, request.episode, request.timestamps_ms, request.values
                )
                self.on_explained(request.episode_id, result)
            except Exception:  # noqa: BLE001 - a failed explanation must not kill the worker
                log.exception("explanation worker failed for episode %s", request.episode_id)

    def drain(self, timeout_s: float = 30.0) -> None:
        """Finish what is queued, then stop. Called at shutdown, never on the hot path."""
        if self._thread is None:
            return
        self.queue.put(None)
        self._thread.join(timeout=timeout_s)
        self._thread = None

    def summary(self) -> str:
        dropped = f" | {self.dropped:,} dropped (queue full)" if self.dropped else ""
        return f"{self.explainer.summary()} | {self.submitted:,} queued{dropped}"
