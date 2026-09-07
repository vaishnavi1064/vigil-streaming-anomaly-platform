"""Turn a flagged window into the picture a vision model is asked about.

Why a picture at all, when the underlying data is a list of numbers: this follows the
flagged-window VLM pattern (VLM4TS, cited in `docs/PROJECT_PLAN.md` section 15.2), whose
premise is that a plot puts a time series into the representation these models were actually
trained on. Handing a model 300 floats as text asks it to do signal processing in tokens;
handing it a chart asks it to do what it is good at. The premise is theirs, and this
implementation is not evidence for it -- measuring whether the explanation is any good needs
a judge, which is the same key we do not have (BLOCKERS C-2).

What is drawn is fixed rather than pretty. The flagged window is shaded, the reference
history before it is drawn in full, and the threshold crossing is marked -- because an
explanation that cannot see what the detector reacted to is guessing.
"""

from __future__ import annotations

import io
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowPlot:
    """A rendered window, ready to send. `png` is raw bytes, not base64."""

    png: bytes
    channel: str
    points: int
    window_start_ms: int
    window_end_ms: int

    @property
    def kilobytes(self) -> float:
        return len(self.png) / 1024.0


def render_window(
    *,
    channel: str,
    timestamps_ms: list[int],
    values: list[float],
    window_start_ms: int,
    window_end_ms: int,
    peak_score: float,
    threshold: float,
    width_px: int = 900,
    height_px: int = 380,
) -> WindowPlot:
    """Draw the window against its preceding history.

    Matplotlib is imported here rather than at module import: the explainer is an optional
    extra, and the detection spine must not fail to start because a plotting library for a
    rare path is absent.
    """
    import matplotlib

    matplotlib.use("Agg")  # No display on a server, and no Tk to import.
    import matplotlib.pyplot as plt

    if len(timestamps_ms) != len(values):
        raise ValueError(
            f"{len(timestamps_ms)} timestamps against {len(values)} values; "
            "a plot built from mismatched arrays would be a lie about the data"
        )
    if not values:
        raise ValueError("nothing to plot: the window carried no samples")

    seconds = [(t - timestamps_ms[0]) / 1000.0 for t in timestamps_ms]
    window_from = (window_start_ms - timestamps_ms[0]) / 1000.0
    window_to = (window_end_ms - timestamps_ms[0]) / 1000.0

    dpi = 100
    figure, axes = plt.subplots(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    try:
        axes.plot(seconds, values, linewidth=1.1, color="#3d4b5c")
        axes.axvspan(window_from, window_to, color="#d95f02", alpha=0.16)
        axes.axvline(window_from, color="#d95f02", linewidth=1.0)
        axes.axvline(window_to, color="#d95f02", linewidth=1.0)
        axes.set_title(
            f"{channel}  |  flagged window shaded  |  peak score {peak_score:.1f} "
            f"against threshold {threshold:.1f}",
            fontsize=10,
        )
        axes.set_xlabel("seconds from the start of the plotted history", fontsize=9)
        axes.set_ylabel("value", fontsize=9)
        axes.grid(True, alpha=0.25, linewidth=0.6)
        axes.tick_params(labelsize=8)
        figure.tight_layout()

        buffer = io.BytesIO()
        figure.savefig(buffer, format="png")
    finally:
        plt.close(figure)

    return WindowPlot(
        png=buffer.getvalue(),
        channel=channel,
        points=len(values),
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
