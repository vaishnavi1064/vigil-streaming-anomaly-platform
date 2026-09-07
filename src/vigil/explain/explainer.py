"""The VLM explainer: a plain-language account of a flagged window, or an honest absence.

ADR-004 puts this on **flagged windows only**. That is not a cost optimisation bolted on
afterwards, it is the reason the design can afford a vision model at all: the hot path scores
every window in 0.15 ms, a few of those become episodes, and only those few are ever
rendered and sent. A per-window explainer would be a per-window API call.

Three properties this must have, in order of how badly their absence would hurt:

1. **It cannot delay or fail detection.** Every failure mode -- no key, unreachable endpoint,
   timeout, malformed response, rate limit -- returns an `Explanation` that says it is
   unavailable and why. Nothing here raises into the detection path.
2. **It says when it did not run.** An episode with no explanation and an episode whose
   explanation failed are different states, and an operator reading a blank field should be
   able to tell which one they are looking at.
3. **It never invents context it was not given.** The prompt carries the episode's own
   numbers and the conditioning verdict, and asks for an account of the picture. It does not
   carry runbooks: grounding remediation in documents is the agent's job (ADR-029), and a
   model asked to both explain and prescribe will prescribe.

Served on a hosted endpoint per B-1, because 4 GB of VRAM does not hold a useful VLM. With
no key configured the explainer is *unavailable*, which is a documented degradation and not a
stub pretending to work -- there is no fallback text, no canned explanation, and no path that
produces prose without a model having produced it.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field

from vigil.episodes import Episode
from vigil.explain.rendering import WindowPlot, render_window
from vigil.settings import VlmSettings

log = logging.getLogger("vigil.explain")

SYSTEM_PROMPT = """You are helping an on-call engineer triage an anomaly detected in sensor \
telemetry. You are shown a plot of one channel, with the flagged window shaded.

Answer in at most four sentences, in plain language, covering: what the signal does inside \
the shaded window compared with before it; whether the shape looks like a step change, a \
burst of noise, a single spike, or something else; and what class of physical or \
instrumentation cause would produce that shape.

Do not recommend an action. Do not speculate about which specific component failed. If the \
plot does not show a clear excursion, say so plainly -- "the shaded window does not look \
different from its surroundings" is a useful answer and a correct one when it is true."""


@dataclass(frozen=True)
class Explanation:
    """What came back, or why nothing did."""

    text: str = ""
    available: bool = False
    reason: str = ""
    model: str = ""
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    image_kb: float = 0.0

    def line(self) -> str:
        if self.available:
            return f"explained by {self.model} in {self.latency_ms:.0f} ms: {self.text[:80]}"
        return f"no explanation: {self.reason}"


@dataclass
class VlmExplainer:
    """Explains flagged windows against a hosted, OpenAI-compatible endpoint.

    Counters are kept because the useful operational question about a rare path is how rare
    it actually was, and how often it failed when it ran.
    """

    settings: VlmSettings = field(default_factory=VlmSettings.from_env)

    requested: int = field(default=0, init=False)
    explained: int = field(default=0, init=False)
    failed: int = field(default=0, init=False)
    skipped_unconfigured: int = field(default=0, init=False)
    total_latency_ms: float = field(default=0.0, init=False)

    @property
    def available(self) -> bool:
        return self.settings.configured

    def explain(self, episode: Episode, plot: WindowPlot) -> Explanation:
        """Explain one flagged window. Never raises."""
        self.requested += 1
        if not self.settings.configured:
            self.skipped_unconfigured += 1
            return Explanation(
                reason=(
                    "VLM_ENDPOINT, VLM_API_KEY and VLM_MODEL are not all set; "
                    "explanation skipped and detection unaffected"
                ),
                image_kb=plot.kilobytes,
            )

        payload = self._payload(episode, plot)
        started = time.perf_counter()
        try:
            import httpx

            response = httpx.post(
                self.settings.endpoint,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.settings.timeout_s,
            )
            latency_ms = (time.perf_counter() - started) * 1000.0
            if response.status_code != 200:
                self.failed += 1
                # The body can carry an API key echo in some providers' error shapes, so only
                # the status and a short prefix are recorded.
                return Explanation(
                    reason=f"endpoint returned HTTP {response.status_code}",
                    latency_ms=latency_ms,
                    model=self.settings.model,
                    image_kb=plot.kilobytes,
                )
            text, usage = self._parse(response.json())
        except Exception as exc:  # noqa: BLE001 - every failure is a handled absence
            self.failed += 1
            latency_ms = (time.perf_counter() - started) * 1000.0
            log.warning("explanation failed for %s: %s", episode.channel, exc)
            return Explanation(
                reason=f"{type(exc).__name__}: {exc}",
                latency_ms=latency_ms,
                model=self.settings.model,
                image_kb=plot.kilobytes,
            )

        if not text:
            self.failed += 1
            return Explanation(
                reason="endpoint returned no content",
                latency_ms=latency_ms,
                model=self.settings.model,
                image_kb=plot.kilobytes,
            )

        self.explained += 1
        self.total_latency_ms += latency_ms
        return Explanation(
            text=text,
            available=True,
            model=self.settings.model,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            image_kb=plot.kilobytes,
        )

    def _payload(self, episode: Episode, plot: WindowPlot) -> dict:
        encoded = base64.b64encode(plot.png).decode("ascii")
        return {
            "model": self.settings.model,
            "max_tokens": 300,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._describe(episode, plot)},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                    ],
                },
            ],
        }

    def _describe(self, episode: Episode, plot: WindowPlot) -> str:
        """The numbers the plot cannot carry precisely, and nothing more.

        Deliberately excludes the ground-truth labels the synthetic source attaches. An
        explainer told what was injected is not explaining, it is reciting.
        """
        lines = [
            f"channel: {episode.channel}",
            f"flagged window: {plot.window_start_ms} to {plot.window_end_ms} (epoch ms)",
            f"episode span: {episode.duration_ms / 1000:.0f} s over {episode.window_count} windows",
            f"peak detector score: {episode.peak_score:.1f} against a threshold of "
            f"{episode.threshold:.1f}",
            f"raised by: {episode.raised_by}",
            f"samples plotted: {plot.points}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _parse(body: dict) -> tuple[str, dict]:
        choices = body.get("choices") or []
        if not choices:
            return "", {}
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # Some providers return content parts rather than a string.
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return (content or "").strip(), body.get("usage") or {}

    def summary(self) -> str:
        if not self.settings.configured:
            return (
                f"explainer: unavailable (no endpoint or key) | {self.requested:,} flagged "
                f"windows would have been explained"
            )
        mean = self.total_latency_ms / self.explained if self.explained else 0.0
        return (
            f"explainer: {self.requested:,} requested | {self.explained:,} explained | "
            f"{self.failed:,} failed | mean {mean:.0f} ms"
        )


def explain_episode(
    explainer: VlmExplainer,
    episode: Episode,
    timestamps_ms: list[int],
    values: list[float],
) -> Explanation:
    """Render then explain, with rendering failures handled the same way as endpoint ones.

    A convenience because callers should not have to remember that a plot can fail to draw --
    an empty or mismatched window is a real possibility on live data, and it must degrade to
    an absent explanation rather than into the detection path.
    """
    try:
        plot = render_window(
            channel=episode.channel,
            timestamps_ms=timestamps_ms,
            values=values,
            window_start_ms=episode.t_start_ms,
            window_end_ms=episode.t_end_ms,
            peak_score=episode.peak_score,
            threshold=episode.threshold,
        )
    except Exception as exc:  # noqa: BLE001 - a plot that will not draw is an absence
        explainer.requested += 1
        explainer.failed += 1
        log.warning("could not render %s: %s", episode.channel, exc)
        return Explanation(reason=f"render failed: {type(exc).__name__}: {exc}")
    return explainer.explain(episode, plot)


def redacted_payload_for_logging(payload: dict) -> str:
    """A payload safe to log: the image is replaced by its size.

    Base64 of a 40 KB PNG in a log line is unreadable and gets truncated somewhere unhelpful,
    and the image is the one part of the request that carries no diagnostic value as text.
    """
    copy = json.loads(json.dumps(payload))
    for message in copy.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:image"):
                    part["image_url"]["url"] = f"<png, {len(url) * 3 // 4 // 1024} KB>"
    return json.dumps(copy)
