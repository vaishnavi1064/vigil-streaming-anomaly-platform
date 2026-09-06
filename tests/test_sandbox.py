"""Tests for the sandbox executor.

Two questions: can anything run without the gate's approval, and can anything escape the
sandbox. Both are answered structurally where possible -- the executor's signature does not
accept a bare action, and the module imports nothing that could reach outside the process --
because a behavioural test only covers the paths someone thought to write.
"""

import inspect
import json

import pytest

from vigil.agent.actions import Action, ActionKind
from vigil.agent.gate import GateDecision, GatePolicy, Reason, SafetyGate, Verdict
from vigil.agent.sandbox import (
    NotApproved,
    SandboxExecutor,
    SandboxState,
    trace_json,
)
from vigil.episodes import Episode, EpisodeStatus

CHANNEL = "pump-03.bearing_temp_c"
EPISODE_ID = 42


def episode(status=EpisodeStatus.REAL):
    return Episode(
        channel=CHANNEL,
        t_start_ms=1_000,
        t_end_ms=31_000,
        raised_by="zscore",
        peak_score=55.0,
        window_count=3,
        threshold=8.0,
        status=status,
    )


def approve(action: Action) -> GateDecision:
    return SafetyGate(policy=GatePolicy()).verdict(action, episode(), EPISODE_ID)


def act(kind, **parameters):
    return Action(kind=kind, parameters=parameters, rationale="because")


# ------------------------- the interlock -------------------------


def test_a_rejected_action_cannot_be_executed():
    # Refuses, not warns. An executor that ran unapproved actions would make the gate
    # advisory and NFR-11's "100%" would rest on every caller remembering to ask.
    executor = SandboxExecutor()
    rejected = GateDecision(
        verdict=Verdict.REJECTED,
        reason=Reason.LIMIT_EXCEEDED,
        detail="too long",
        action=act(ActionKind.SILENCE_CHANNEL, channel=CHANNEL, minutes=9999, reason="x"),
    )
    with pytest.raises(NotApproved):
        executor.execute(rejected, EPISODE_ID)
    assert executor.refused == 1
    assert executor.executed == 0


def test_a_rejected_action_changes_no_state():
    executor = SandboxExecutor()
    rejected = GateDecision(
        verdict=Verdict.REJECTED,
        reason=Reason.PROTECTED_CHANNEL,
        detail="protected",
        action=act(ActionKind.SILENCE_CHANNEL, channel=CHANNEL, minutes=5, reason="x"),
    )
    with pytest.raises(NotApproved):
        executor.execute(rejected, EPISODE_ID)
    assert executor.state.snapshot() == SandboxState().snapshot()


def test_there_is_no_signature_that_takes_a_bare_action():
    # The interlock is structural: you cannot call execute() without a decision, so an
    # approval cannot be forgotten.
    signature = inspect.signature(SandboxExecutor.execute)
    assert "decision" in signature.parameters
    assert "action" not in signature.parameters


# ------------------------- containment -------------------------


def test_the_sandbox_imports_nothing_that_could_reach_outside_the_process():
    from vigil.agent import sandbox

    source = inspect.getsource(sandbox)
    for forbidden in (
        "import subprocess",
        "import socket",
        "import requests",
        "import httpx",
        "urllib.request",
        "confluent_kafka",
        "os.system",
    ):
        assert forbidden not in source, f"sandbox imports {forbidden}"


def test_a_read_with_no_reader_says_so_rather_than_returning_nothing():
    # "There was no reader" and "the reader found nothing" are different facts; a trace that
    # conflated them would be unreadable.
    executor = SandboxExecutor(reader=None)
    result = executor.execute(
        approve(act(ActionKind.DESCRIBE_CHANNEL, channel=CHANNEL)), EPISODE_ID
    )
    assert result.ok
    assert result.output["available"] is False


def test_a_reader_is_used_when_one_is_given():
    class Reader:
        def query(self, name, **kwargs):
            return {"called": name, "kwargs": kwargs}

    executor = SandboxExecutor(reader=Reader())
    result = executor.execute(
        approve(act(ActionKind.DESCRIBE_CHANNEL, channel=CHANNEL)), EPISODE_ID
    )
    assert result.output["available"] is True
    assert result.output["result"]["called"] == "describe_channel"


def test_read_actions_record_no_effects():
    executor = SandboxExecutor()
    result = executor.execute(
        approve(act(ActionKind.DESCRIBE_CHANNEL, channel=CHANNEL)), EPISODE_ID
    )
    assert result.effects == ()
    assert executor.state.snapshot() == SandboxState().snapshot()


def test_recalibration_is_a_request_not_a_recalibration():
    # The agent asks; a human with physical access acts. Anything touching the instrument
    # directly would be outside the sandbox by definition.
    executor = SandboxExecutor()
    result = executor.execute(
        approve(act(ActionKind.REQUEST_RECALIBRATION, channel=CHANNEL, reason="drift")),
        EPISODE_ID,
    )
    assert result.ok
    assert executor.state.recalibration_requests[0]["status"] == "requested"
    assert "requested" in result.effects[0]


# ------------------------- effects are recorded -------------------------


def test_raising_a_ticket_records_it_with_an_id():
    executor = SandboxExecutor()
    result = executor.execute(
        approve(
            act(
                ActionKind.RAISE_TICKET,
                episode_id=EPISODE_ID,
                summary="bearing hot",
                severity="high",
            )
        ),
        EPISODE_ID,
    )
    assert result.ok
    assert executor.state.tickets[0]["id"].startswith("TICKET-")
    assert executor.state.tickets[0]["severity"] == "high"
    assert result.effects


def test_silencing_records_the_channel_and_when_it_expires():
    executor = SandboxExecutor()
    result = executor.execute(
        approve(act(ActionKind.SILENCE_CHANNEL, channel=CHANNEL, minutes=15, reason="flapping")),
        EPISODE_ID,
    )
    assert result.ok
    silenced = executor.state.silenced[CHANNEL]
    assert silenced["minutes"] == 15
    assert silenced["until"] > 0
    assert silenced["reason"] == "flapping"


def test_escalation_is_recorded_so_a_human_can_find_it():
    executor = SandboxExecutor()
    executor.execute(
        approve(act(ActionKind.ESCALATE_TO_HUMAN, episode_id=EPISODE_ID, reason="beyond me")),
        EPISODE_ID,
    )
    assert executor.state.escalations[0]["episode_id"] == EPISODE_ID


def test_every_effect_is_undoable_by_deleting_a_record():
    # The whole mutable surface is SandboxState, so reverting is deleting rows.
    executor = SandboxExecutor()
    for action in (
        act(ActionKind.SILENCE_CHANNEL, channel=CHANNEL, minutes=5, reason="x"),
        act(ActionKind.ANNOTATE_EPISODE, episode_id=EPISODE_ID, note="n"),
    ):
        executor.execute(approve(action), EPISODE_ID)
    assert executor.state.snapshot() != SandboxState().snapshot()
    executor.state = SandboxState()
    assert executor.state.snapshot() == SandboxState().snapshot()


# ------------------------- failure handling -------------------------


def test_a_failing_action_is_reported_rather_than_raised():
    # One bad action must not stop the loop, and a silent failure would be worse than either.
    class ExplodingReader:
        def query(self, name, **kwargs):
            raise RuntimeError("store is down")

    executor = SandboxExecutor(reader=ExplodingReader())
    result = executor.execute(
        approve(act(ActionKind.DESCRIBE_CHANNEL, channel=CHANNEL)), EPISODE_ID
    )
    assert not result.ok
    assert "store is down" in result.error
    assert executor.failed == 1


def test_a_failed_action_leaves_the_counters_honest():
    class ExplodingReader:
        def query(self, name, **kwargs):
            raise RuntimeError("boom")

    executor = SandboxExecutor(reader=ExplodingReader())
    executor.execute(approve(act(ActionKind.DESCRIBE_CHANNEL, channel=CHANNEL)), EPISODE_ID)
    assert executor.executed == 0
    assert executor.failed == 1
    assert "failed 1" in executor.summary()


# ------------------------- the trace -------------------------


def test_the_trace_records_what_was_proposed_judged_and_done():
    executor = SandboxExecutor()
    decision = approve(
        act(ActionKind.RAISE_TICKET, episode_id=EPISODE_ID, summary="s", severity="low")
    )
    result = executor.execute(decision, EPISODE_ID)
    trace = json.loads(trace_json(decision, result, EPISODE_ID))
    assert trace["episode_id"] == EPISODE_ID
    assert trace["action"]["kind"] == "raise_ticket"
    assert trace["gate"]["verdict"] == "approved"
    assert trace["execution"]["ok"] is True
    assert trace["execution"]["effects"]


def test_the_trace_keeps_the_rationale_the_gate_refused_to_read():
    # A human reviewing the trace should see the argument the agent made, precisely because
    # the gate did not.
    action = Action(
        kind=ActionKind.ESCALATE_TO_HUMAN,
        parameters={"episode_id": EPISODE_ID, "reason": "r"},
        rationale="I believe this is urgent",
    )
    trace = json.loads(trace_json(approve(action), None, EPISODE_ID))
    assert trace["action"]["rationale"] == "I believe this is urgent"


def test_a_rejected_proposal_still_produces_a_trace():
    # An action that was refused is exactly the thing an auditor wants to see.
    decision = SafetyGate(policy=GatePolicy()).verdict(
        act(ActionKind.SILENCE_CHANNEL, channel=CHANNEL, minutes=99_999, reason="x"),
        episode(),
        EPISODE_ID,
    )
    trace = json.loads(trace_json(decision, None, EPISODE_ID))
    assert trace["gate"]["verdict"] == "rejected"
    assert trace["gate"]["reason"] == "limit_exceeded"
    assert trace["execution"] is None
