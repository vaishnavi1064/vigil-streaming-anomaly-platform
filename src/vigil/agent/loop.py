"""The agent loop: Diagnoser -> Planner -> Safety Gate -> Executor.

The shape is the standard AIOps loop and is not claimed as novel (PROJECT_PLAN section
15.4). What is worth attention is where the authority sits.

**The diagnoser characterises; it does not decide.** It turns an episode into a symptom --
a level shift, a variance burst, an isolated spike -- from the evidence attached to it. That
characterisation becomes a retrieval query, not an instruction.

**The planner may only propose what a runbook licenses.** Every passage names the actions it
permits, and the planner intersects its proposals with that set. An action no retrieved
passage licenses is not proposed, so a wrong answer is traceable to a document rather than
to a model's mood. When retrieval finds nothing, the plan is to escalate -- "the runbooks do
not cover this" is a real answer and it belongs with a human.

**The gate has the last word and cannot be argued with.** It re-checks everything the
planner believed, because a planner that could be trusted to self-limit would not need a
gate. Retrieval widens what is *considered*; it never widens what is *permitted*.

The default planner is deterministic and rule-based -- no model. That is a deliberate
starting point, not a placeholder: it makes the loop testable end to end and gives the
model-backed planner (Phase 5) a baseline to be measured against. A `Planner` protocol is
the seam where an LLM plugs in, and the gate is unchanged either way, which is the whole
point of putting the authority in the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from vigil.agent.actions import Action, ActionKind
from vigil.agent.gate import GateDecision, SafetyGate
from vigil.agent.runbooks import Retrieved, RunbookIndex
from vigil.agent.sandbox import ExecutionResult, NotApproved, SandboxExecutor, trace_json
from vigil.episodes import Episode


class Symptom(StrEnum):
    """What the episode looks like, from its own evidence."""

    LEVEL_SHIFT = "level_shift"
    VARIANCE_BURST = "variance_burst"
    ISOLATED_SPIKE = "isolated_spike"
    SILENT_CHANNEL = "silent_channel"
    SAFETY_CHANNEL = "safety_channel"
    UNKNOWN = "unknown"


SAFETY_MARKERS = ("safety", "interlock", "fire", "emergency")


@dataclass(frozen=True)
class Diagnosis:
    symptom: Symptom
    channel: str
    summary: str
    query: str
    evidence: dict[str, object] = field(default_factory=dict)


class Diagnoser:
    """Characterises an episode from the evidence already attached to it.

    Reads only what the platform recorded -- the detector's per-window scores, the episode's
    shape, the channel's name. It does not fetch anything, because a diagnoser that could
    reach out would need gating too, and the gate exists precisely so that only one component
    needs to be trusted.
    """

    def diagnose(self, episode: Episode) -> Diagnosis:
        channel = episode.channel
        lowered = channel.lower()

        if any(marker in lowered for marker in SAFETY_MARKERS):
            return Diagnosis(
                symptom=Symptom.SAFETY_CHANNEL,
                channel=channel,
                summary=f"excursion on safety-related channel {channel}",
                query=f"excursion on a safety instrument {channel} escalate",
                evidence={"peak_score": episode.peak_score},
            )

        windows = episode.window_count
        duration_s = episode.duration_ms / 1000.0
        dispersion = _dominant_dispersion(episode)

        if windows <= 1 and duration_s <= 60:
            symptom = Symptom.ISOLATED_SPIKE
            query = f"isolated spike single sample {channel} transmission artifact"
            summary = f"single-window excursion on {channel}, peak {episode.peak_score:.1f}"
        elif dispersion:
            symptom = Symptom.VARIANCE_BURST
            query = f"variance burst stable mean {channel} electrical interference"
            summary = (
                f"dispersion-driven excursion on {channel} over {windows} windows "
                f"({duration_s:.0f}s)"
            )
        else:
            symptom = Symptom.LEVEL_SHIFT
            query = f"sustained level shift single channel {channel} calibration drift"
            summary = (
                f"sustained level shift on {channel} over {windows} windows "
                f"({duration_s:.0f}s), peak {episode.peak_score:.1f}"
            )

        return Diagnosis(
            symptom=symptom,
            channel=channel,
            summary=summary,
            query=query,
            evidence={
                "peak_score": episode.peak_score,
                "windows": windows,
                "duration_s": round(duration_s, 1),
                "dispersion_dominant": dispersion,
            },
        )


def _dominant_dispersion(episode: Episode) -> bool:
    """Was the excursion driven by dispersion rather than by the mean moving?

    The z-score detector records both terms per window, so this reads the detector's own
    working rather than re-deriving it from the raw values -- which the agent cannot see.
    """
    dispersion_wins = 0
    total = 0
    for sample in episode.scores:
        detail = getattr(sample, "detail", None)
        if not isinstance(detail, dict):
            continue
        if "dispersion_z" in detail and "mean_z" in detail:
            total += 1
            if detail["dispersion_z"] > detail["mean_z"]:
                dispersion_wins += 1
    return total > 0 and dispersion_wins * 2 > total


class Planner(Protocol):
    """The seam where a model-backed planner plugs in.

    Whatever implements this, the gate is unchanged -- which is the point of putting the
    authority in the gate rather than in the planner.
    """

    def plan(
        self, episode: Episode, episode_id: int, diagnosis: Diagnosis, retrieved: list[Retrieved]
    ) -> list[Action]: ...


class RunbookPlanner:
    """Deterministic planner. Proposes only what the retrieved passages license.

    Rule-based on purpose: it makes the loop testable end to end, and it gives the
    model-backed planner a baseline to be measured against rather than merely replaced by.
    """

    def plan(
        self, episode: Episode, episode_id: int, diagnosis: Diagnosis, retrieved: list[Retrieved]
    ) -> list[Action]:
        if not retrieved:
            # "The runbooks do not cover this" is a real answer, and it belongs with a human
            # rather than with an agent improvising.
            return [
                Action(
                    kind=ActionKind.ESCALATE_TO_HUMAN,
                    parameters={
                        "episode_id": episode_id,
                        "reason": f"no runbook passage matches {diagnosis.summary}",
                    },
                    rationale="retrieval found nothing; escalating rather than improvising",
                )
            ]

        licensed = {name for hit in retrieved for name in hit.passage.licenses}
        citation = f"{retrieved[0].passage.runbook}: {retrieved[0].passage.title}"
        proposed: list[Action] = []

        def offer(kind: ActionKind, rationale: str, **parameters) -> None:
            if str(kind) in licensed:
                proposed.append(
                    Action(kind=kind, parameters=parameters, rationale=f"{rationale} [{citation}]")
                )

        # Always gather evidence first when the runbook allows it: an action taken before
        # looking is one whose justification is the diagnosis alone.
        offer(
            ActionKind.DESCRIBE_CHANNEL,
            "confirm the channel's normal behaviour before acting",
            channel=episode.channel,
        )
        offer(
            ActionKind.FETCH_RECENT_READINGS,
            "compare the excursion against the preceding hour",
            channel=episode.channel,
            minutes=60,
        )

        match diagnosis.symptom:
            case Symptom.SAFETY_CHANNEL:
                proposed = [a for a in proposed if a.kind is ActionKind.DESCRIBE_CHANNEL]
                offer(
                    ActionKind.ESCALATE_TO_HUMAN,
                    "safety-related channel; no automated remediation is permitted",
                    episode_id=episode_id,
                    reason=diagnosis.summary,
                )
            case Symptom.ISOLATED_SPIKE:
                # The runbook is explicit that no remediation is warranted for one spike.
                offer(
                    ActionKind.ANNOTATE_EPISODE,
                    "single-sample spike; record the pattern, take no action",
                    episode_id=episode_id,
                    note=diagnosis.summary,
                )
            case Symptom.VARIANCE_BURST:
                offer(
                    ActionKind.SILENCE_CHANNEL,
                    "channel carries no usable signal during the burst",
                    channel=episode.channel,
                    minutes=30,
                    reason=diagnosis.summary,
                )
                offer(
                    ActionKind.RAISE_TICKET,
                    "a silence needs an owner",
                    episode_id=episode_id,
                    summary=diagnosis.summary,
                    severity="medium",
                )
            case Symptom.LEVEL_SHIFT:
                offer(
                    ActionKind.REQUEST_RECALIBRATION,
                    "sustained shift with calm siblings suggests instrument drift",
                    channel=episode.channel,
                    reason=diagnosis.summary,
                )
                offer(
                    ActionKind.RAISE_TICKET,
                    "recalibration needs a work order",
                    episode_id=episode_id,
                    summary=diagnosis.summary,
                    severity="medium",
                )
            case _:
                offer(
                    ActionKind.ESCALATE_TO_HUMAN,
                    "symptom not recognised",
                    episode_id=episode_id,
                    reason=diagnosis.summary,
                )

        return proposed


@dataclass
class AgentStep:
    action: Action
    decision: GateDecision
    result: ExecutionResult | None
    trace: str


@dataclass
class AgentRun:
    episode_id: int
    diagnosis: Diagnosis
    retrieved: list[Retrieved]
    steps: list[AgentStep] = field(default_factory=list)

    @property
    def executed(self) -> list[AgentStep]:
        return [s for s in self.steps if s.result is not None and s.result.ok]

    @property
    def rejected(self) -> list[AgentStep]:
        return [s for s in self.steps if not s.decision.approved]

    @property
    def every_action_was_gated(self) -> bool:
        """NFR-11, checkable per run: nothing executed without an approval."""
        return all(s.decision.approved for s in self.steps if s.result is not None)

    def citations(self) -> list[str]:
        return [f"{h.passage.runbook}: {h.passage.title}" for h in self.retrieved]


@dataclass
class RemediationAgent:
    """Diagnose, retrieve, plan, gate, execute -- in that order, every time."""

    runbooks: RunbookIndex
    gate: SafetyGate
    executor: SandboxExecutor
    planner: Planner = field(default_factory=RunbookPlanner)
    diagnoser: Diagnoser = field(default_factory=Diagnoser)
    retrieval_limit: int = 3

    runs: int = field(default=0, init=False)
    escalations: int = field(default=0, init=False)

    def handle(self, episode: Episode, episode_id: int) -> AgentRun:
        self.runs += 1
        diagnosis = self.diagnoser.diagnose(episode)
        retrieved = self.runbooks.search(diagnosis.query, limit=self.retrieval_limit)
        run = AgentRun(episode_id=episode_id, diagnosis=diagnosis, retrieved=retrieved)

        for action in self.planner.plan(episode, episode_id, diagnosis, retrieved):
            decision = self.gate.verdict(action, episode, episode_id)
            result: ExecutionResult | None = None
            if decision.approved:
                try:
                    result = self.executor.execute(decision, episode_id)
                except NotApproved:
                    # Unreachable by construction; kept so a future refactor that broke the
                    # interlock fails loudly here rather than executing something ungated.
                    result = None
                else:
                    if result.ok:
                        self.gate.record_taken(episode_id, action)
            if action.kind is ActionKind.ESCALATE_TO_HUMAN and decision.approved:
                self.escalations += 1
            run.steps.append(
                AgentStep(
                    action=action,
                    decision=decision,
                    result=result,
                    trace=trace_json(decision, result, episode_id),
                )
            )
        return run

    def summary(self) -> str:
        return (
            f"agent: {self.runs:,} episodes handled | {self.escalations:,} escalated | "
            f"{self.gate.summary()} | {self.executor.summary()}"
        )
