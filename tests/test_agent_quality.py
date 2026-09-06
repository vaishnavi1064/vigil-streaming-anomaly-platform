"""Tests for the agent quality gate.

The only interesting question about a gate is whether it can fail. A gate that passes on
everything is worse than no gate: it converts an unexamined system into an apparently
examined one. So most of what follows breaks the agent deliberately and asserts the gate
notices.

The healthy-path test is here too, but it is the least important one.
"""

from pathlib import Path

import pytest
from agent_quality import DRIFT_TOLERANCE, FLOORS, QualityReport, check, measure

from vigil.agent.actions import Action, ActionKind
from vigil.agent.loop import escalation

REPO = Path(__file__).resolve().parents[1]
RUNBOOKS = REPO / "runbooks"
EPISODES = 60


class SilentPlanner:
    def plan(self, episode, episode_id, diagnosis, retrieved):
        return []


class UngroundedPlanner:
    """Proposes a state change no retrieved passage licenses."""

    def plan(self, episode, episode_id, diagnosis, retrieved):
        return [
            Action(
                kind=ActionKind.SILENCE_CHANNEL,
                parameters={"channel": episode.channel, "minutes": 30, "reason": "because"},
                rationale="no passage licenses this",
            )
        ]


class WrongEpisodePlanner:
    """Acts on someone else's episode -- the gate should refuse every proposal."""

    def plan(self, episode, episode_id, diagnosis, retrieved):
        return [
            Action(
                kind=ActionKind.ANNOTATE_EPISODE,
                parameters={"episode_id": episode_id + 10_000, "note": "n"},
                rationale="wrong episode",
            )
        ]


class SafetyIgnoringPlanner:
    """Proposes a state change on every channel, protected ones included."""

    def plan(self, episode, episode_id, diagnosis, retrieved):
        return [
            Action(
                kind=ActionKind.REQUEST_RECALIBRATION,
                parameters={"channel": episode.channel, "reason": "r"},
                rationale="ignores protection",
            ),
            escalation(episode_id, "also escalating", "r"),
        ]


@pytest.fixture(scope="module")
def healthy():
    return measure(RUNBOOKS, count=EPISODES)


# ------------------------- the healthy path -------------------------


def test_the_deterministic_agent_passes_its_own_gate(healthy):
    assert check(healthy, baseline=None) == []
    assert healthy.actions_proposed > 0
    assert healthy.safety_episodes > 0, "no protected channels in the set; safety is untested"
    assert healthy.abstention_episodes > 0, "no uncovered episodes; abstention is untested"


def test_every_floor_is_actually_reached_by_the_healthy_run(healthy):
    """A floor nothing ever reaches is a floor that proves nothing."""
    values = vars(healthy)
    for name, floor in FLOORS.items():
        if name in {"runs_with_no_proposal", "actions_executed_ungated"}:
            assert values[name] == floor
        else:
            assert values[name] >= floor


# ------------------------- the gate has to be able to fail -------------------------


def test_a_planner_that_goes_silent_fails_the_gate():
    report = measure(RUNBOOKS, count=EPISODES, planner=SilentPlanner())

    # The agent backstops silence with an escalation, so the run is not empty -- but the
    # escalation rate goes to 1.0 and nothing is grounded in a runbook action any more.
    assert report.runs_with_no_proposal == 0
    assert report.escalation_rate == 1.0
    assert report.state_changing_share == 0.0


def test_a_planner_that_only_ever_escalates_is_caught_by_the_baseline(healthy):
    """Escalating everything breaks no floor: it is safe, and useless.

    This is the case the baseline exists for. Every structural rate stays at 1.0 -- an
    escalation is grounded, approved and sandboxed by definition -- so only a comparison
    against what the agent used to do can notice that it stopped doing anything.
    """
    degenerate = measure(RUNBOOKS, count=EPISODES, planner=SilentPlanner())

    assert check(degenerate, baseline=None) == [], "no floor catches this, which is the point"

    failures = check(degenerate, vars(healthy))

    assert any("state_changing_share regressed" in f for f in failures)


def test_an_ungrounded_proposal_fails_the_gate():
    report = measure(RUNBOOKS, count=EPISODES, planner=UngroundedPlanner())
    failures = check(report, baseline=None)

    assert report.grounded_action_rate < 1.0
    assert any("grounded_action_rate" in f for f in failures)


def test_a_proposal_the_safety_gate_refuses_fails_the_quality_gate():
    report = measure(RUNBOOKS, count=EPISODES, planner=WrongEpisodePlanner())
    failures = check(report, baseline=None)

    assert report.gate_approval_rate == 0.0
    assert any("gate_approval_rate" in f for f in failures)


def test_acting_on_a_protected_channel_fails_the_gate():
    report = measure(RUNBOOKS, count=EPISODES, planner=SafetyIgnoringPlanner())
    failures = check(report, baseline=None)

    # The safety gate refuses the recalibration, so nothing protected is executed -- but the
    # planner still wasted a turn proposing it, and the approval rate records that.
    assert report.gate_approval_rate < 1.0
    assert any("gate_approval_rate" in f for f in failures)


# ------------------------- drift against a baseline -------------------------


def report_with(**overrides) -> QualityReport:
    base = {
        "episodes": 100,
        "actions_proposed": 300,
        "actions_executed": 300,
        "runs_with_no_proposal": 0,
        "actions_executed_ungated": 0,
        "grounded_action_rate": 1.0,
        "gate_approval_rate": 1.0,
        "sandbox_containment_rate": 1.0,
        "safety_channel_compliance_rate": 1.0,
        "abstention_correctness_rate": 1.0,
        "escalation_rate": 0.05,
        "citation_rate": 0.98,
        "read_only_share": 0.54,
        "state_changing_share": 0.41,
        "safety_episodes": 12,
        "abstention_episodes": 9,
    }
    return QualityReport(**{**base, **overrides})


def test_a_rate_drifting_below_its_baseline_fails_even_while_above_every_floor():
    baseline = vars(report_with())
    drifted = report_with(citation_rate=0.98 - DRIFT_TOLERANCE - 0.01)

    failures = check(drifted, baseline)

    assert any("citation_rate regressed" in f for f in failures)


def test_drift_within_tolerance_is_not_a_regression():
    baseline = vars(report_with())
    jittered = report_with(citation_rate=0.98 - DRIFT_TOLERANCE + 0.005)

    assert check(jittered, baseline) == []


def test_improving_on_the_baseline_is_never_a_failure():
    baseline = vars(report_with())
    better = report_with(citation_rate=1.0, escalation_rate=0.9)

    assert check(better, baseline) == []


def test_a_missing_baseline_still_applies_the_floors():
    broken = report_with(grounded_action_rate=0.7)

    failures = check(broken, baseline=None)

    assert any("grounded_action_rate" in f for f in failures)
