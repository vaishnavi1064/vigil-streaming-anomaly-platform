"""Explanation of flagged windows, on the rare path only.

The detection spine does not import this. The explainer is an optional extra with an
optional endpoint, and a platform whose detector cannot start because a plotting library or
an API key is missing has traded the important thing for the decorative one.
"""

from vigil.explain.explainer import (
    SYSTEM_PROMPT,
    ClaudeExplainer,
    ExplainerRefused,
    Explanation,
    VlmExplainer,
    WindowExplainer,
    describe_episode,
    explain_episode,
    redacted_payload_for_logging,
    window_explainer_from_env,
)
from vigil.explain.rendering import WindowPlot, render_window
from vigil.explain.worker import ExplanationRequest, ExplanationWorker

__all__ = [
    "SYSTEM_PROMPT",
    "ClaudeExplainer",
    "Explanation",
    "ExplainerRefused",
    "ExplanationRequest",
    "ExplanationWorker",
    "VlmExplainer",
    "WindowExplainer",
    "WindowPlot",
    "describe_episode",
    "explain_episode",
    "redacted_payload_for_logging",
    "render_window",
    "window_explainer_from_env",
]
