"""Tests for the VLM explainer.

No API key exists in this environment (BLOCKERS C-2), so the success path is exercised
against a local fake endpoint rather than left untested until a key appears. What that
proves is the contract: the request shape, the parse, the counters and the latency. What it
cannot prove is whether a real model's explanation is any good -- that needs a judge, and
that is the same missing key.

The failure paths matter more than the success one here. An explainer that raises, blocks, or
quietly emits plausible-looking text without a model behind it would each be worse than
having no explainer at all, and each has a test.
"""

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vigil.episodes import Episode
from vigil.explain import (
    Explanation,
    VlmExplainer,
    explain_episode,
    redacted_payload_for_logging,
    render_window,
)
from vigil.settings import VlmSettings


def episode(channel="pump-01.flow_m3_h", peak=41.2):
    return Episode(
        channel=channel,
        t_start_ms=60_000,
        t_end_ms=90_000,
        raised_by="zscore",
        peak_score=peak,
        window_count=3,
        threshold=8.0,
    )


def series(points=120, step_ms=1000):
    timestamps = [i * step_ms for i in range(points)]
    values = [
        10 + math.sin(i / 6) + (7 if 60_000 <= t < 90_000 else 0) for i, t in enumerate(timestamps)
    ]
    return timestamps, values


def plot_for(ep=None):
    timestamps, values = series()
    ep = ep or episode()
    return render_window(
        channel=ep.channel,
        timestamps_ms=timestamps,
        values=values,
        window_start_ms=ep.t_start_ms,
        window_end_ms=ep.t_end_ms,
        peak_score=ep.peak_score,
        threshold=ep.threshold,
    )


# ------------------------- rendering -------------------------


def test_a_window_renders_to_a_png():
    plot = plot_for()

    assert plot.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert plot.points == 120
    assert 1 < plot.kilobytes < 500


def test_mismatched_arrays_are_refused_rather_than_plotted():
    """A plot drawn from misaligned arrays is a confident picture of nothing."""
    with pytest.raises(ValueError, match="lie about the data"):
        render_window(
            channel="a.b",
            timestamps_ms=[0, 1000, 2000],
            values=[1.0, 2.0],
            window_start_ms=0,
            window_end_ms=1000,
            peak_score=10.0,
            threshold=8.0,
        )


def test_an_empty_window_is_refused():
    with pytest.raises(ValueError, match="no samples"):
        render_window(
            channel="a.b",
            timestamps_ms=[],
            values=[],
            window_start_ms=0,
            window_end_ms=1000,
            peak_score=10.0,
            threshold=8.0,
        )


# ------------------------- the unavailable path -------------------------


def test_with_no_key_the_explainer_reports_unavailable_and_explains_nothing():
    explainer = VlmExplainer(settings=VlmSettings(endpoint="", api_key="", model=""))

    result = explainer.explain(episode(), plot_for())

    assert result.available is False
    assert result.text == ""
    assert "not all set" in result.reason
    assert explainer.skipped_unconfigured == 1
    assert "unavailable" in explainer.summary()


def test_a_partially_configured_endpoint_counts_as_unconfigured():
    """Two of three is not a working endpoint, and guessing the third is not an option."""
    explainer = VlmExplainer(settings=VlmSettings(endpoint="http://x", api_key="k", model=""))

    assert explainer.available is False
    assert explainer.explain(episode(), plot_for()).available is False


def test_an_unreachable_endpoint_degrades_instead_of_raising():
    explainer = VlmExplainer(
        settings=VlmSettings(
            endpoint="http://127.0.0.1:1/v1/chat/completions",
            api_key="k",
            model="m",
            timeout_s=2.0,
        )
    )

    result = explainer.explain(episode(), plot_for())

    assert result.available is False
    assert result.reason
    assert explainer.failed == 1


def test_a_render_failure_becomes_an_absent_explanation_not_an_exception():
    explainer = VlmExplainer(settings=VlmSettings(endpoint="", api_key="", model=""))

    result = explain_episode(explainer, episode(), timestamps_ms=[0, 1], values=[1.0])

    assert result.available is False
    assert "render failed" in result.reason
    assert explainer.failed == 1


def test_an_explanation_that_did_not_happen_is_distinguishable_from_one_that_was_blank():
    """An operator reading an empty field has to be able to tell which case it is."""
    skipped = Explanation(reason="no key")
    blank = Explanation(reason="endpoint returned no content")

    assert skipped.reason != blank.reason
    assert not skipped.available and not blank.available


# ------------------------- the success path, against a fake endpoint -------------------------


class FakeVlm(BaseHTTPRequestHandler):
    reply = "The shaded window steps about seven units above the preceding baseline."
    status = 200
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's interface
        length = int(self.headers.get("Content-Length", 0))
        FakeVlm.seen.append(json.loads(self.rfile.read(length)))
        body = json.dumps(
            {
                "choices": [{"message": {"content": FakeVlm.reply}}],
                "usage": {"prompt_tokens": 812, "completion_tokens": 44},
            }
        ).encode()
        self.send_response(FakeVlm.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def fake_endpoint():
    FakeVlm.seen = []
    FakeVlm.status = 200
    server = HTTPServer(("127.0.0.1", 0), FakeVlm)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    server.shutdown()
    server.server_close()


def configured(endpoint):
    return VlmExplainer(
        settings=VlmSettings(endpoint=endpoint, api_key="test-key", model="fake-vlm", timeout_s=5)
    )


def test_a_configured_endpoint_produces_an_explanation(fake_endpoint):
    explainer = configured(fake_endpoint)

    result = explainer.explain(episode(), plot_for())

    assert result.available is True
    assert result.text == FakeVlm.reply
    assert result.model == "fake-vlm"
    assert result.latency_ms > 0
    assert result.prompt_tokens == 812
    assert explainer.explained == 1
    assert "explained" in explainer.summary()


def test_the_request_carries_the_image_and_the_episode_numbers(fake_endpoint):
    explainer = configured(fake_endpoint)
    explainer.explain(episode(peak=99.5), plot_for())

    sent = FakeVlm.seen[-1]
    parts = sent["messages"][1]["content"]
    text = next(p["text"] for p in parts if p["type"] == "text")
    image = next(p for p in parts if p["type"] == "image_url")

    assert image["image_url"]["url"].startswith("data:image/png;base64,")
    assert "peak detector score: 99.5" in text
    assert "pump-01.flow_m3_h" in text


def test_the_prompt_withholds_the_ground_truth_labels(fake_endpoint):
    """An explainer told what was injected is reciting, not explaining."""
    explainer = configured(fake_endpoint)
    subject = episode()
    subject.injected_origins = ("fault",)

    explainer.explain(subject, plot_for())

    sent = json.dumps(FakeVlm.seen[-1]["messages"][1]["content"][0])
    assert "injected" not in sent
    assert "fault" not in sent


def test_a_non_200_response_is_an_absence_not_a_crash(fake_endpoint):
    FakeVlm.status = 503
    explainer = configured(fake_endpoint)

    result = explainer.explain(episode(), plot_for())

    assert result.available is False
    assert "503" in result.reason
    assert explainer.failed == 1


def test_an_empty_completion_counts_as_a_failure_not_an_explanation(fake_endpoint):
    FakeVlm.reply = ""
    try:
        explainer = configured(fake_endpoint)
        result = explainer.explain(episode(), plot_for())

        assert result.available is False
        assert "no content" in result.reason
    finally:
        FakeVlm.reply = "The shaded window steps about seven units above the preceding baseline."


def test_content_returned_as_parts_is_parsed(fake_endpoint):
    """Some providers return a list of content parts rather than a string."""
    explainer = configured(fake_endpoint)

    text, usage = explainer._parse(
        {
            "choices": [{"message": {"content": [{"type": "text", "text": "a step change."}]}}],
            "usage": {"prompt_tokens": 5},
        }
    )

    assert text == "a step change."
    assert usage["prompt_tokens"] == 5


# ------------------------- logging -------------------------


def test_a_logged_payload_does_not_contain_the_base64_image(fake_endpoint):
    explainer = configured(fake_endpoint)
    payload = explainer._payload(episode(), plot_for())

    redacted = redacted_payload_for_logging(payload)

    assert "base64" not in redacted
    assert "KB>" in redacted
    assert len(redacted) < 2_000


# ------------------------- the worker, and what it protects -------------------------


def test_the_worker_never_blocks_and_counts_what_it_drops():
    """A full queue drops and says so. Growing it until the process dies is not an option."""
    from vigil.explain import ExplanationRequest, ExplanationWorker

    explainer = VlmExplainer(settings=VlmSettings(endpoint="", api_key="", model=""))
    worker = ExplanationWorker(explainer, lambda _id, _r: None, max_pending=2)
    # Deliberately not started: nothing drains the queue, so the third submit must be dropped
    # rather than block the caller, which on the real path is the detection loop.
    timestamps, values = series()
    requests = [
        ExplanationRequest(episode_id=i, episode=episode(), timestamps_ms=timestamps, values=values)
        for i in range(3)
    ]

    accepted = [worker.submit(r) for r in requests]

    assert accepted == [True, True, False]
    assert worker.dropped == 1
    assert "dropped" in worker.summary()


def test_the_worker_delivers_an_explanation_to_its_callback(fake_endpoint):
    from vigil.explain import ExplanationRequest, ExplanationWorker

    delivered = []
    worker = ExplanationWorker(configured(fake_endpoint), lambda i, r: delivered.append((i, r)))
    worker.start()
    timestamps, values = series()

    worker.submit(
        ExplanationRequest(episode_id=7, episode=episode(), timestamps_ms=timestamps, values=values)
    )
    worker.drain(timeout_s=20)

    assert len(delivered) == 1
    episode_id, result = delivered[0]
    assert episode_id == 7
    assert result.available is True
    assert result.text == FakeVlm.reply


def test_a_failing_explanation_does_not_kill_the_worker(fake_endpoint):
    """One bad episode must not take the explainer down for every episode after it."""
    from vigil.explain import ExplanationRequest, ExplanationWorker

    delivered = []
    worker = ExplanationWorker(configured(fake_endpoint), lambda i, r: delivered.append((i, r)))
    worker.start()
    timestamps, values = series()

    # The first cannot be rendered at all; the second is fine.
    worker.submit(ExplanationRequest(episode_id=1, episode=episode(), timestamps_ms=[], values=[]))
    worker.submit(
        ExplanationRequest(episode_id=2, episode=episode(), timestamps_ms=timestamps, values=values)
    )
    worker.drain(timeout_s=20)

    assert [i for i, _ in delivered] == [1, 2]
    assert delivered[0][1].available is False
    assert delivered[1][1].available is True
