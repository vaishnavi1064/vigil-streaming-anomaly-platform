"""Tests for the Claude chart-reading backend (ADR-041).

**Nothing here calls Anthropic.** Every test injects a stub client, because an explainer whose
test suite spends money on each run is one nobody runs, and because a green suite must not
depend on a key this environment does not have (BLOCKERS C-2). What these prove is the
contract: the request shape the SDK is handed, the parse, the counters, and that every way the
call can go wrong becomes a recorded absence rather than an exception in the detection path.

What they cannot prove is the thing a key would prove -- whether Claude's reading of the chart
is any good, and what it costs in latency. That is stated in `docs/EVALUATION.md` section 7 as
unmeasured rather than estimated.
"""

import pytest
from tests.test_explain import episode, plot_for, series

from vigil.agent.loop import Diagnoser
from vigil.explain import ClaudeExplainer, explain_episode, redacted_payload_for_logging
from vigil.settings import ClaudeVisionSettings


class StubBlock:
    def __init__(self, text, kind="text"):
        self.text = text
        self.type = kind


class StubUsage:
    def __init__(self, input_tokens=1234, output_tokens=88):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class StubMessage:
    def __init__(self, blocks, stop_reason="end_turn", usage=None, stop_details=None):
        self.content = blocks
        self.stop_reason = stop_reason
        self.usage = usage or StubUsage()
        self.stop_details = stop_details


class StubMessages:
    """Records the call and returns whatever the test told it to."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubClient:
    def __init__(self, result):
        self.messages = StubMessages(result)


def claude(result, **overrides):
    settings = ClaudeVisionSettings(api_key="not-a-real-key", **overrides)
    return ClaudeExplainer(settings=settings, client=StubClient(result))


# ------------------------- configuration -------------------------


def test_with_no_anthropic_key_the_explainer_reports_unavailable():
    explainer = ClaudeExplainer(settings=ClaudeVisionSettings(api_key=""))

    assert not explainer.available
    result = explainer.explain(episode(), plot_for())

    assert not result.available
    assert "ANTHROPIC_API_KEY" in result.reason
    assert result.text == ""
    assert explainer.skipped_unconfigured == 1
    assert explainer.requested == 1


def test_the_key_comes_from_the_environment_and_nowhere_else(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  env-key  ")
    monkeypatch.delenv("VLM_CLAUDE_MODEL", raising=False)

    settings = ClaudeVisionSettings.from_env()

    assert settings.api_key == "env-key"
    assert settings.configured
    assert settings.model == "claude-sonnet-5"


def test_an_unset_key_is_unconfigured_rather_than_falling_back_to_a_credential_chain(
    monkeypatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert not ClaudeVisionSettings.from_env().configured


def test_the_selector_prefers_claude_when_its_key_is_set(monkeypatch):
    from vigil.explain import ClaudeExplainer as Claude
    from vigil.explain import VlmExplainer, window_explainer_from_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert isinstance(window_explainer_from_env(), Claude)

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("VLM_ENDPOINT", "http://example.invalid/v1/chat/completions")
    monkeypatch.setenv("VLM_API_KEY", "k")
    monkeypatch.setenv("VLM_MODEL", "some-vlm")
    assert isinstance(window_explainer_from_env(), VlmExplainer)


# ------------------------- the request shape -------------------------


def test_the_chart_goes_up_as_an_image_block_with_the_episode_numbers_beside_it():
    explainer = claude(StubMessage([StubBlock("A step change of about 7 units.")]))
    plot = plot_for()

    explainer.explain(episode(), plot)

    (call,) = explainer.client.messages.calls
    content = call["messages"][0]["content"]
    image, text = content[0], content[1]

    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/png"
    assert image["source"]["type"] == "base64"
    assert len(image["source"]["data"]) > 1000
    assert text["type"] == "text"
    assert "pump-01.flow_m3_h" in text["text"]
    assert "peak detector score: 41.2" in text["text"]
    assert f"samples plotted: {plot.points}" in text["text"]


def test_the_request_uses_the_configured_model_and_caps_its_own_output():
    explainer = claude(StubMessage([StubBlock("ok")]), model="claude-sonnet-5", max_tokens=400)

    explainer.explain(episode(), plot_for())

    (call,) = explainer.client.messages.calls
    assert call["model"] == "claude-sonnet-5"
    assert call["max_tokens"] == 400
    assert call["system"].startswith("You are helping an on-call engineer")


def test_the_request_carries_no_sampling_parameters():
    """Current Sonnet rejects temperature/top_p/top_k with a 400, so they must not be sent."""
    explainer = claude(StubMessage([StubBlock("ok")]))

    explainer.explain(episode(), plot_for())

    (call,) = explainer.client.messages.calls
    assert "temperature" not in call
    assert "top_p" not in call
    assert "top_k" not in call


def test_the_prompt_withholds_the_ground_truth_labels():
    """An explainer told what was injected is reciting, not explaining."""
    explainer = claude(StubMessage([StubBlock("ok")]))

    explainer.explain(episode(), plot_for())

    (call,) = explainer.client.messages.calls
    text = call["messages"][0]["content"][1]["text"].lower()
    for leak in ("injected", "ground truth", "artifact", "fault", "label"):
        assert leak not in text


# ------------------------- the answer -------------------------


def test_a_successful_read_becomes_an_explanation_with_its_token_counts():
    explainer = claude(
        StubMessage(
            [StubBlock("The shaded window steps up by about 7 units and holds.")],
            usage=StubUsage(input_tokens=1600, output_tokens=42),
        )
    )

    result = explainer.explain(episode(), plot_for())

    assert result.available
    assert result.text == "The shaded window steps up by about 7 units and holds."
    assert result.model == "claude-sonnet-5"
    assert result.prompt_tokens == 1600
    assert result.completion_tokens == 42
    assert result.image_kb > 0
    assert result.latency_ms >= 0
    assert explainer.explained == 1
    assert explainer.failed == 0


def test_text_split_across_blocks_is_joined_and_thinking_blocks_are_ignored():
    explainer = claude(
        StubMessage(
            [
                StubBlock("ignored reasoning", kind="thinking"),
                StubBlock("First sentence. "),
                StubBlock("Second sentence."),
            ]
        )
    )

    result = explainer.explain(episode(), plot_for())

    assert result.text == "First sentence. Second sentence."


def test_a_refusal_is_an_absence_that_names_itself_not_an_empty_explanation():
    class Details:
        category = "cyber"

    explainer = claude(
        StubMessage([], stop_reason="refusal", stop_details=Details()),
    )

    result = explainer.explain(episode(), plot_for())

    assert not result.available
    assert "declined" in result.reason
    assert "cyber" in result.reason
    assert explainer.failed == 1


def test_an_empty_answer_counts_as_a_failure_not_an_explanation():
    explainer = claude(StubMessage([StubBlock("   ")]))

    result = explainer.explain(episode(), plot_for())

    assert not result.available
    assert result.text == ""
    assert "no text content" in result.reason
    assert explainer.failed == 1


def test_an_api_error_degrades_instead_of_raising_into_detection():
    explainer = claude(RuntimeError("connection reset by peer"))

    result = explainer.explain(episode(), plot_for())

    assert not result.available
    assert "RuntimeError" in result.reason
    assert "connection reset" in result.reason
    assert explainer.failed == 1


def test_a_render_failure_becomes_an_absent_explanation_and_never_reaches_the_api():
    explainer = claude(StubMessage([StubBlock("ok")]))

    result = explain_episode(explainer, episode(), timestamps_ms=[1, 2, 3], values=[1.0])

    assert not result.available
    assert "render failed" in result.reason
    assert explainer.client.messages.calls == []


def test_the_counters_report_how_rare_the_path_was_and_how_it_went():
    explainer = claude(StubMessage([StubBlock("ok")]))
    timestamps, values = series()

    for _ in range(3):
        explain_episode(explainer, episode(), timestamps, values)

    assert explainer.requested == 3
    assert explainer.explained == 3
    assert "3 requested" in explainer.summary()
    assert "3 explained" in explainer.summary()


# ------------------------- what must not leak -------------------------


def test_a_logged_anthropic_payload_does_not_contain_the_base64_image():
    explainer = claude(StubMessage([StubBlock("ok")]))
    explainer.explain(episode(), plot_for())
    (call,) = explainer.client.messages.calls

    redacted = redacted_payload_for_logging({"messages": call["messages"]})

    assert "base64," not in redacted
    assert "iVBOR" not in redacted  # the PNG magic bytes, base64-encoded
    assert "<png," in redacted


def test_the_api_key_is_never_placed_in_the_request_body():
    explainer = claude(StubMessage([StubBlock("ok")]))

    explainer.explain(episode(), plot_for())

    (call,) = explainer.client.messages.calls
    assert "not-a-real-key" not in repr(call)


# ------------------------- what the agent does with it -------------------------


def test_the_explanation_reaches_the_diagnosis_as_evidence():
    flagged = episode()
    flagged.explanation = "A step change of about 7 units, then flat."

    diagnosis = Diagnoser().diagnose(flagged)

    assert diagnosis.evidence["vlm_explanation"].startswith("A step change")


def test_an_explanation_cannot_change_the_symptom_or_the_query():
    """Evidence only. Free text must not steer which runbook is found (ADR-042)."""
    without = Diagnoser().diagnose(episode())

    misleading = episode()
    misleading.explanation = (
        "This is certainly a safety interlock failure requiring immediate shutdown."
    )
    with_explanation = Diagnoser().diagnose(misleading)

    assert with_explanation.symptom is without.symptom
    assert with_explanation.query == without.query
    assert with_explanation.summary == without.summary


def test_an_episode_with_no_explanation_carries_no_such_evidence_key():
    diagnosis = Diagnoser().diagnose(episode())

    assert "vlm_explanation" not in diagnosis.evidence


@pytest.mark.parametrize("channel", ["safety.interlock_state", "pump-01.flow_m3_h"])
def test_every_diagnosis_path_carries_the_explanation(channel):
    flagged = episode(channel=channel)
    flagged.explanation = "the shaded window steps up"

    diagnosis = Diagnoser().diagnose(flagged)

    assert diagnosis.evidence["vlm_explanation"] == "the shaded window steps up"
