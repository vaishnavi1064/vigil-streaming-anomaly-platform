"""Tests for the agent loop and runbook retrieval.

The properties worth guarding are about *authority*, not about whether the agent picks a
clever action: that retrieval grounds the plan, that the planner cannot propose what no
runbook licenses, that the gate still overrules a planner that tries, and that nothing
executes ungated. A planner is replaceable; those properties are not.
"""

from pathlib import Path

import pytest

from vigil.agent.actions import Action, ActionKind
from vigil.agent.gate import GatePolicy, Reason, SafetyGate
from vigil.agent.loop import (
    Diagnoser,
    RemediationAgent,
    RunbookPlanner,
    Symptom,
)
from vigil.agent.runbooks import Passage, RunbookIndex, load_runbooks, tokenize
from vigil.agent.sandbox import SandboxExecutor
from vigil.episodes import Episode, EpisodeStatus, ScoreSample

RUNBOOKS = Path(__file__).resolve().parents[1] / "runbooks"


def episode(
    channel="pump-03.bearing_temp_c",
    windows=9,
    duration_ms=120_000,
    peak=61.0,
    dispersion=False,
    status=EpisodeStatus.REAL,
):
    detail = (
        {"mean_z": 2.0, "dispersion_z": 40.0}
        if dispersion
        else {"mean_z": 40.0, "dispersion_z": 2.0}
    )
    return Episode(
        channel=channel,
        t_start_ms=0,
        t_end_ms=duration_ms,
        raised_by="zscore",
        peak_score=peak,
        window_count=windows,
        threshold=8.0,
        scores=[ScoreSample("zscore", 0, 30_000, peak, 0.2, detail) for _ in range(windows)],
        status=status,
    )


def build_agent(**policy):
    return RemediationAgent(
        runbooks=load_runbooks(RUNBOOKS),
        gate=SafetyGate(policy=GatePolicy(**policy)),
        executor=SandboxExecutor(),
    )


# ------------------------- retrieval -------------------------


def test_channel_names_survive_tokenization_intact():
    # Splitting bearing_temp_c into three tokens would let a query about temperature match
    # every channel with a _c suffix.
    assert "bearing_temp_c" in tokenize("excursion on pump-03.bearing_temp_c today")


def test_the_runbooks_parse_into_passages_with_licences():
    index = load_runbooks(RUNBOOKS)
    assert len(index) >= 8
    assert all(p.title for p in index.passages)
    assert any(p.licenses for p in index.passages)


def test_every_passage_licenses_only_real_actions():
    # A licence naming a verb that does not exist would silently license nothing.
    valid = {str(k) for k in ActionKind}
    for passage in load_runbooks(RUNBOOKS).passages:
        assert set(passage.licenses) <= valid, f"{passage.title} licenses an unknown action"


def test_retrieval_ranks_the_matching_passage_first():
    index = load_runbooks(RUNBOOKS)
    top = index.search("variance burst stable mean electrical interference", limit=1)[0]
    assert "Variance burst" in top.passage.title


def test_retrieval_returns_nothing_when_nothing_matches():
    # An empty result is a real answer -- the runbooks do not cover this -- and is what
    # should send the agent to a human rather than to invention.
    index = load_runbooks(RUNBOOKS)
    assert index.search("quarterly revenue forecast shareholder dividend") == []


def test_retrieval_reports_which_terms_matched():
    index = load_runbooks(RUNBOOKS)
    hit = index.search("calibration drift", limit=1)[0]
    assert hit.matched_terms
    assert hit.score > 0


def test_an_empty_index_returns_nothing_rather_than_failing():
    assert RunbookIndex(passages=[]).search("anything") == []


def test_a_passage_with_no_licence_line_licenses_nothing():
    index = RunbookIndex(passages=[Passage("r", "t", "some text about pumps")])
    assert index.search("pumps", limit=1)[0].passage.licenses == ()


# ------------------------- diagnosis -------------------------


def test_a_sustained_excursion_is_diagnosed_as_a_level_shift():
    assert Diagnoser().diagnose(episode(windows=9)).symptom is Symptom.LEVEL_SHIFT


def test_a_single_window_excursion_is_diagnosed_as_a_spike():
    assert (
        Diagnoser().diagnose(episode(windows=1, duration_ms=30_000)).symptom
        is Symptom.ISOLATED_SPIKE
    )


def test_a_dispersion_driven_excursion_is_diagnosed_as_a_variance_burst():
    # Read from the detector's own working rather than re-derived, since the agent cannot
    # see the raw values.
    assert Diagnoser().diagnose(episode(dispersion=True)).symptom is Symptom.VARIANCE_BURST


def test_a_safety_channel_is_recognised_before_anything_else():
    diagnosis = Diagnoser().diagnose(episode(channel="unit.safety_interlock_state", windows=9))
    assert diagnosis.symptom is Symptom.SAFETY_CHANNEL


def test_the_diagnosis_carries_the_evidence_behind_it():
    diagnosis = Diagnoser().diagnose(episode())
    assert diagnosis.evidence["windows"] == 9
    assert diagnosis.evidence["peak_score"] == 61.0
    assert diagnosis.channel in diagnosis.summary


# ------------------------- planning is grounded -------------------------


def test_the_plan_only_contains_actions_a_retrieved_passage_licenses():
    # The property that makes a wrong answer traceable to a document rather than to a
    # model's mood.
    index = load_runbooks(RUNBOOKS)
    diagnoser = Diagnoser()
    planner = RunbookPlanner()
    for ep in (episode(), episode(dispersion=True), episode(windows=1, duration_ms=30_000)):
        diagnosis = diagnoser.diagnose(ep)
        retrieved = index.search(diagnosis.query, limit=3)
        licensed = {n for hit in retrieved for n in hit.passage.licenses}
        for action in planner.plan(ep, 1, diagnosis, retrieved):
            assert str(action.kind) in licensed, f"{action.kind} was not licensed"


def test_an_unmatched_episode_escalates_rather_than_improvising():
    planner = RunbookPlanner()
    diagnosis = Diagnoser().diagnose(episode())
    actions = planner.plan(episode(), 7, diagnosis, [])
    assert len(actions) == 1
    assert actions[0].kind is ActionKind.ESCALATE_TO_HUMAN
    assert "no runbook passage" in actions[0].parameters["reason"]


def test_every_proposed_action_cites_the_passage_it_came_from():
    index = load_runbooks(RUNBOOKS)
    diagnosis = Diagnoser().diagnose(episode())
    actions = RunbookPlanner().plan(episode(), 1, diagnosis, index.search(diagnosis.query))
    assert actions
    assert all("[" in a.rationale and "]" in a.rationale for a in actions)


def test_a_safety_channel_plan_contains_no_remediation():
    ep = episode(channel="unit.safety_interlock_state")
    index = load_runbooks(RUNBOOKS)
    diagnosis = Diagnoser().diagnose(ep)
    actions = RunbookPlanner().plan(ep, 1, diagnosis, index.search(diagnosis.query))
    kinds = {a.kind for a in actions}
    assert ActionKind.ESCALATE_TO_HUMAN in kinds
    assert ActionKind.SILENCE_CHANNEL not in kinds
    assert ActionKind.REQUEST_RECALIBRATION not in kinds


def test_a_spike_gets_recorded_and_nothing_else():
    # The runbook is explicit that no remediation is warranted for a single spike.
    ep = episode(windows=1, duration_ms=30_000)
    index = load_runbooks(RUNBOOKS)
    diagnosis = Diagnoser().diagnose(ep)
    kinds = {a.kind for a in RunbookPlanner().plan(ep, 1, diagnosis, index.search(diagnosis.query))}
    assert ActionKind.SILENCE_CHANNEL not in kinds
    assert ActionKind.REQUEST_RECALIBRATION not in kinds


# ------------------------- the gate still has the last word -------------------------


def test_nothing_executes_without_an_approval():
    # NFR-11, checkable per run.
    agent = build_agent()
    run = agent.handle(episode(), 42)
    assert run.steps
    assert run.every_action_was_gated


def test_the_gate_overrules_a_planner_that_proposes_something_forbidden():
    # A planner that could be trusted to self-limit would not need a gate.
    class RecklessPlanner:
        def plan(self, episode, episode_id, diagnosis, retrieved):
            return [
                Action(
                    kind=ActionKind.SILENCE_CHANNEL,
                    parameters={"channel": episode.channel, "minutes": 100_000, "reason": "x"},
                    rationale="trust me",
                )
            ]

    agent = build_agent()
    agent.planner = RecklessPlanner()
    run = agent.handle(episode(), 42)
    assert run.rejected
    assert run.rejected[0].decision.reason is Reason.LIMIT_EXCEEDED
    assert agent.executor.executed == 0


def test_the_gate_overrules_a_planner_aiming_at_a_protected_channel():
    class RecklessPlanner:
        def plan(self, episode, episode_id, diagnosis, retrieved):
            return [
                Action(
                    kind=ActionKind.SILENCE_CHANNEL,
                    parameters={"channel": episode.channel, "minutes": 5, "reason": "noisy"},
                )
            ]

    agent = build_agent()
    agent.planner = RecklessPlanner()
    run = agent.handle(episode(channel="unit.fire_suppression_pressure"), 42)
    assert run.rejected[0].decision.reason is Reason.PROTECTED_CHANNEL
    assert agent.executor.state.silenced == {}


def test_an_attributed_episode_gets_investigated_but_not_remediated():
    # The interlock with the core contribution: attributing an artifact to its cause and
    # then acting on it anyway would undo the attribution.
    agent = build_agent()
    run = agent.handle(episode(status=EpisodeStatus.ATTRIBUTED), 42)
    executed = {s.action.kind for s in run.executed}
    assert executed <= {ActionKind.DESCRIBE_CHANNEL, ActionKind.FETCH_RECENT_READINGS}
    assert any(s.decision.reason is Reason.EPISODE_NOT_ACTIONABLE for s in run.rejected)


# ------------------------- the whole loop -------------------------


def test_a_level_shift_run_produces_a_recalibration_request_and_a_ticket():
    agent = build_agent()
    run = agent.handle(episode(), 42)
    executed = {s.action.kind for s in run.executed}
    assert ActionKind.REQUEST_RECALIBRATION in executed
    assert ActionKind.RAISE_TICKET in executed
    assert agent.executor.state.recalibration_requests
    assert agent.executor.state.tickets


def test_a_variance_burst_run_silences_the_channel_and_gives_the_silence_an_owner():
    agent = build_agent()
    run = agent.handle(episode(dispersion=True), 42)
    executed = {s.action.kind for s in run.executed}
    assert ActionKind.SILENCE_CHANNEL in executed
    assert ActionKind.RAISE_TICKET in executed, "a silence needs an owner"


def test_every_step_carries_a_trace():
    agent = build_agent()
    run = agent.handle(episode(), 42)
    assert all(s.trace for s in run.steps)
    assert all("gate" in s.trace for s in run.steps)


def test_the_run_cites_the_passages_it_used():
    run = build_agent().handle(episode(), 42)
    assert run.citations()
    assert all(":" in c for c in run.citations())


def test_evidence_is_gathered_before_state_is_changed():
    # An action taken before looking is one whose justification is the diagnosis alone.
    run = build_agent().handle(episode(), 42)
    kinds = [s.action.kind for s in run.steps]
    first_write = next(
        (
            i
            for i, k in enumerate(kinds)
            if k in {ActionKind.REQUEST_RECALIBRATION, ActionKind.RAISE_TICKET}
        ),
        None,
    )
    assert first_write is not None
    assert ActionKind.DESCRIBE_CHANNEL in kinds[:first_write]


def test_a_budget_exhausted_episode_still_escalates():
    agent = build_agent(max_actions_per_episode=1)
    agent.handle(episode(), 42)
    run = agent.handle(episode(channel="pump-99.unknown_metric"), 42)
    # Whatever else was refused, handing to a human remains available.
    assert any(
        s.action.kind is ActionKind.ESCALATE_TO_HUMAN and s.decision.approved for s in run.steps
    ) or all(not s.decision.approved for s in run.steps)


def test_handling_many_episodes_keeps_the_summary_honest():
    agent = build_agent()
    for i in range(5):
        agent.handle(episode(), i)
    summary = agent.summary()
    assert "5 episodes handled" in summary
    assert "gate:" in summary
    assert "sandbox:" in summary


@pytest.mark.parametrize(
    "channel",
    ["unit.safety_interlock_state", "site.fire_alarm_loop", "line.emergency_stop"],
)
def test_a_safety_channel_is_never_acted_on_end_to_end(channel):
    agent = build_agent()
    agent.handle(episode(channel=channel), 42)
    assert agent.executor.state.silenced == {}
    assert agent.executor.state.recalibration_requests == []
    assert agent.executor.state.escalations


# ------------------------- abstention must be visible -------------------------
# An episode that produces no proposal at all leaves no decision and no trace, which reads
# exactly like an episode nobody looked at. Both ways that can happen are covered here.


def coverage_gap_index():
    """A runbook set that matches the query but licenses nothing useful for the symptom.

    Real deployments look like this while runbooks lag the fleet: something is retrieved,
    and none of it authorises the action the symptom calls for.
    """
    index = RunbookIndex()
    index.add(
        Passage(
            runbook="thin.md",
            title="Post-deploy settling after a variance burst",
            text="A channel whose variance rises after a deploy is usually settling; the "
            "burst subsides without intervention and only needs recording.",
            licenses=("annotate_episode",),
        )
    )
    return index


def test_a_runbook_coverage_gap_escalates_rather_than_planning_nothing():
    index = coverage_gap_index()
    subject = episode(dispersion=True)
    diagnosis = Diagnoser().diagnose(subject)
    retrieved = index.search(diagnosis.query, limit=3)
    assert retrieved, "this test is only meaningful when retrieval matched something"

    actions = RunbookPlanner().plan(subject, 7, diagnosis, retrieved)

    assert [a.kind for a in actions] == [ActionKind.ESCALATE_TO_HUMAN]
    assert "license" in actions[0].parameters["reason"]


def test_the_agent_backstops_a_planner_that_proposes_nothing():
    """The planner seam accepts any implementation, including one that returns nothing."""

    class SilentPlanner:
        def plan(self, episode, episode_id, diagnosis, retrieved):
            return []

    agent = RemediationAgent(
        runbooks=load_runbooks(RUNBOOKS),
        gate=SafetyGate(policy=GatePolicy()),
        executor=SandboxExecutor(),
        planner=SilentPlanner(),
    )
    run = agent.handle(episode(), 42)

    assert [s.action.kind for s in run.steps] == [ActionKind.ESCALATE_TO_HUMAN]
    assert run.every_action_was_gated
    assert agent.escalations == 1


def test_the_backstop_escalation_is_gated_like_any_other_action():
    class SilentPlanner:
        def plan(self, episode, episode_id, diagnosis, retrieved):
            return []

    agent = RemediationAgent(
        runbooks=load_runbooks(RUNBOOKS),
        gate=SafetyGate(policy=GatePolicy()),
        executor=SandboxExecutor(),
        planner=SilentPlanner(),
    )
    run = agent.handle(episode(), 42)

    assert all(s.decision is not None for s in run.steps)
    assert run.steps[0].trace
