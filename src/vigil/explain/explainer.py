"""The VLM explainer: a plain-language account of a flagged window, or an honest absence.

ADR-004 puts this on **flagged windows only**. That is not a cost optimisation bolted on
afterwards, it is the reason the design can afford a vision model at all: the hot path scores
every window in 0.15 ms, a few of those become episodes, and only those few are ever
rendered and sent. A per-window explainer would be a per-window API call.

Three properties this must have, in order of how badly their absence would hurt:

1. **It cannot delay or fail detection.** Every failure mode -- no key, unreachable endpoint,
   timeout, malformed response, rate limit, a model that declines -- returns an `Explanation`
   that says it is unavailable and why. Nothing here raises into the detection path.
2. **It says when it did not run.** An episode with no explanation and an episode whose
   explanation failed are different states, and an operator reading a blank field should be
   able to tell which one they are looking at.
3. **It never invents context it was not given.** The prompt carries the episode's own
   numbers and the flagged span, and asks for an account of the picture. It does not carry
   runbooks: grounding remediation in documents is the agent's job (ADR-029), and a model
   asked to both explain and prescribe will prescribe.

Two backends, one contract (ADR-041). `ClaudeExplainer` calls Anthropic's Messages API with
the chart as an image block; `VlmExplainer` calls any OpenAI-compatible endpoint and is what
B-1 originally specified. `window_explainer_from_env` picks the first that is configured.
Both are optional: with neither key set the explainer is *unavailable*, which is a documented
degradation and not a stub pretending to work -- there is no fallback text, no canned
explanation, and no path that produces prose without a model having produced it.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field

from vigil.episodes import Episode
from vigil.explain.rendering import WindowPlot, render_window
from vigil.settings import ClaudeVisionSettings, VlmSettings

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


class ExplainerRefused(Exception):
    """The backend answered, and the answer was not an explanation.

    Separate from an ordinary exception because the two want different records: a refusal or
    an HTTP status is a fact about the request that is safe and useful to write down verbatim,
    while an arbitrary exception gets its type name and is logged at warning.
    """


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


def describe_episode(episode: Episode, plot: WindowPlot) -> str:
    """The numbers the plot cannot carry precisely, and nothing more.

    Deliberately excludes the ground-truth labels the synthetic source attaches. An explainer
    told what was injected is not explaining, it is reciting. Shared by both backends so the
    two cannot drift into answering subtly different questions.
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


@dataclass
class WindowExplainer:
    """What both backends owe the caller, and the counters that say how it went.

    Counters are kept because the useful operational question about a rare path is how rare
    it actually was, and how often it failed when it ran. Subclasses supply `_request`; this
    class owns the promise that nothing escapes into the detection path.
    """

    requested: int = field(default=0, init=False)
    explained: int = field(default=0, init=False)
    failed: int = field(default=0, init=False)
    skipped_unconfigured: int = field(default=0, init=False)
    total_latency_ms: float = field(default=0.0, init=False)

    @property
    def available(self) -> bool:
        raise NotImplementedError

    @property
    def model_name(self) -> str:
        raise NotImplementedError

    def _unconfigured_reason(self) -> str:
        raise NotImplementedError

    def _empty_reason(self) -> str:
        raise NotImplementedError

    def _request(self, episode: Episode, plot: WindowPlot) -> tuple[str, dict]:
        """Do the call. Return (text, usage); raise `ExplainerRefused` for a stated refusal."""
        raise NotImplementedError

    def explain(self, episode: Episode, plot: WindowPlot) -> Explanation:
        """Explain one flagged window. Never raises."""
        self.requested += 1
        if not self.available:
            self.skipped_unconfigured += 1
            return Explanation(reason=self._unconfigured_reason(), image_kb=plot.kilobytes)

        started = time.perf_counter()
        try:
            text, usage = self._request(episode, plot)
        except ExplainerRefused as refusal:
            self.failed += 1
            return Explanation(
                reason=str(refusal),
                latency_ms=(time.perf_counter() - started) * 1000.0,
                model=self.model_name,
                image_kb=plot.kilobytes,
            )
        except Exception as exc:  # noqa: BLE001 - every failure is a handled absence
            self.failed += 1
            log.warning("explanation failed for %s: %s", episode.channel, exc)
            return Explanation(
                reason=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
                model=self.model_name,
                image_kb=plot.kilobytes,
            )
        latency_ms = (time.perf_counter() - started) * 1000.0

        if not text:
            self.failed += 1
            return Explanation(
                reason=self._empty_reason(),
                latency_ms=latency_ms,
                model=self.model_name,
                image_kb=plot.kilobytes,
            )

        self.explained += 1
        self.total_latency_ms += latency_ms
        return Explanation(
            text=text,
            available=True,
            model=self.model_name,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            image_kb=plot.kilobytes,
        )

    def _unavailable_summary(self) -> str:
        raise NotImplementedError

    def summary(self) -> str:
        if not self.available:
            return self._unavailable_summary()
        mean = self.total_latency_ms / self.explained if self.explained else 0.0
        return (
            f"explainer: {self.requested:,} requested | {self.explained:,} explained | "
            f"{self.failed:,} failed | mean {mean:.0f} ms"
        )


@dataclass
class ClaudeExplainer(WindowExplainer):
    """Reads the chart with Claude, through Anthropic's Messages API (ADR-041).

    The chart goes up as an image block and the episode's numbers as text beside it, which is
    the VLM4TS screen-then-verify shape: the picture is the representation the model was
    trained on, and the numbers are the part a picture cannot carry precisely.

    `client` exists to be injected. The tests never reach the network -- an explainer that can
    only be verified by spending money on every run is one nobody runs.
    """

    settings: ClaudeVisionSettings = field(default_factory=ClaudeVisionSettings.from_env)
    client: object | None = None

    @property
    def available(self) -> bool:
        return self.settings.configured or self.client is not None

    @property
    def model_name(self) -> str:
        return self.settings.model

    def _unconfigured_reason(self) -> str:
        return "ANTHROPIC_API_KEY is not set; explanation skipped and detection unaffected"

    def _empty_reason(self) -> str:
        return "Claude returned no text content"

    def _anthropic_client(self) -> object:
        """Built once, lazily, from the environment key only.

        Imported here rather than at module import for the same reason matplotlib is: the
        explainer is an optional extra, and the detection spine must not fail to start because
        a library for a rare path is absent. `api_key` is passed explicitly rather than left
        to the SDK's credential chain -- the chain would also accept an OAuth profile or a
        token file, which would make "configured" depend on state nobody documented here.
        """
        if self.client is None:
            import anthropic

            self.client = anthropic.Anthropic(
                api_key=self.settings.api_key,
                timeout=self.settings.timeout_s,
                # One retry, not the SDK default of two: this sits behind a bounded queue on
                # a rare path, and three attempts at a 20 s timeout is a minute of a worker
                # thread spent on one episode nobody is waiting for.
                max_retries=1,
            )
        return self.client

    def _request(self, episode: Episode, plot: WindowPlot) -> tuple[str, dict]:
        message = self._anthropic_client().messages.create(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=SYSTEM_PROMPT,
            # Reading one chart and writing four sentences is not a reasoning problem, and
            # this path has a latency budget (NFR-2). Effort is the knob to raise if the
            # explanations turn out to be thin.
            output_config={"effort": "low"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(plot.png).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": describe_episode(episode, plot)},
                    ],
                }
            ],
        )

        # A declined request is a 200 with no explanation in it, so `stop_reason` has to be
        # read before `content` or the absence is mistaken for an empty answer.
        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise ExplainerRefused(f"Claude declined to answer (category: {category})")

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        ).strip()
        usage = getattr(message, "usage", None)
        return text, {
            "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
        }

    def _unavailable_summary(self) -> str:
        return (
            f"explainer: unavailable (ANTHROPIC_API_KEY unset) | {self.requested:,} flagged "
            f"windows would have been explained"
        )


@dataclass
class VlmExplainer(WindowExplainer):
    """Explains flagged windows against a hosted, OpenAI-compatible endpoint (B-1).

    Kept alongside the Claude backend rather than replaced by it: it is the interface B-1
    was answered with, it is what the contract tests run against a local fake, and it is the
    escape hatch for anyone serving their own VLM.
    """

    settings: VlmSettings = field(default_factory=VlmSettings.from_env)

    @property
    def available(self) -> bool:
        return self.settings.configured

    @property
    def model_name(self) -> str:
        return self.settings.model

    def _unconfigured_reason(self) -> str:
        return (
            "VLM_ENDPOINT, VLM_API_KEY and VLM_MODEL are not all set; "
            "explanation skipped and detection unaffected"
        )

    def _empty_reason(self) -> str:
        return "endpoint returned no content"

    def _request(self, episode: Episode, plot: WindowPlot) -> tuple[str, dict]:
        import httpx

        response = httpx.post(
            self.settings.endpoint,
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            json=self._payload(episode, plot),
            timeout=self.settings.timeout_s,
        )
        if response.status_code != 200:
            # The body can carry an API key echo in some providers' error shapes, so only the
            # status is recorded.
            raise ExplainerRefused(f"endpoint returned HTTP {response.status_code}")
        return self._parse(response.json())

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
        return describe_episode(episode, plot)

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

    def _unavailable_summary(self) -> str:
        return (
            f"explainer: unavailable (no endpoint or key) | {self.requested:,} flagged "
            f"windows would have been explained"
        )


def window_explainer_from_env() -> WindowExplainer:
    """Claude when `ANTHROPIC_API_KEY` is set, the hosted endpoint otherwise.

    Claude first because it is the backend that has a key in practice; the OpenAI-compatible
    path stays reachable by simply not setting one. Either way an unconfigured explainer is
    returned rather than None, so the caller still gets counters telling it how many flagged
    windows went unexplained.
    """
    claude = ClaudeVisionSettings.from_env()
    if claude.configured:
        return ClaudeExplainer(settings=claude)
    return VlmExplainer(settings=VlmSettings.from_env())


def explain_episode(
    explainer: WindowExplainer,
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
    Handles both wire shapes -- the OpenAI-compatible `image_url` part and Anthropic's
    `source.data` -- because a log line that leaks one of them is as bad as one that leaks
    the other.
    """
    copy = json.loads(json.dumps(payload))
    for message in copy.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            url = part.get("image_url", {}).get("url", "")
            if url.startswith("data:image"):
                part["image_url"]["url"] = f"<png, {len(url) * 3 // 4 // 1024} KB>"
            source = part.get("source")
            if isinstance(source, dict) and source.get("type") == "base64":
                encoded = source.get("data", "")
                source["data"] = f"<png, {len(encoded) * 3 // 4 // 1024} KB>"
    return json.dumps(copy)
