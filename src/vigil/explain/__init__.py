"""Explanation of flagged windows, on the rare path only.

The detection spine does not import this. The explainer is an optional extra with an
optional endpoint, and a platform whose detector cannot start because a plotting library or
an API key is missing has traded the important thing for the decorative one.
"""

from vigil.explain.explainer import (
    SYSTEM_PROMPT,
    Explanation,
    VlmExplainer,
    explain_episode,
    redacted_payload_for_logging,
)
from vigil.explain.rendering import WindowPlot, render_window
from vigil.explain.worker import ExplanationRequest, ExplanationWorker

__all__ = [
    "SYSTEM_PROMPT",
    "Explanation",
    "ExplanationRequest",
    "ExplanationWorker",
    "VlmExplainer",
    "WindowPlot",
    "explain_episode",
    "redacted_payload_for_logging",
    "render_window",
]
