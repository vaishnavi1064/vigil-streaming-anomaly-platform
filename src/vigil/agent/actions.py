"""The typed surface an agent is allowed to act through.

An agent that can emit arbitrary shell is not gateable: to decide whether `sh -c "..."` is
safe you have to understand shell, and at that point the gate is an interpreter with an
attack surface. So the agent does not emit commands. It emits an **Action** -- one of a
closed set of verbs, with typed parameters -- and the gate reasons about that.

This is what makes NFR-11's "100% of actions pass a deterministic gate" implementable rather
than aspirational. The set is deliberately small and every member is reversible or
read-only; anything destructive is absent rather than gated, because a verb that exists is a
verb someone will eventually allow.

Adding a verb is a deliberate act: it needs an entry here, a risk classification, and a
sandbox implementation. That friction is the feature.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ActionKind(StrEnum):
    """Everything the agent may propose. There is nothing else."""

    # Read-only: gather evidence.
    DESCRIBE_CHANNEL = "describe_channel"
    FETCH_RECENT_READINGS = "fetch_recent_readings"
    FETCH_PIPELINE_HEALTH = "fetch_pipeline_health"
    SEARCH_RUNBOOKS = "search_runbooks"

    # Reversible: change operational posture without touching the plant.
    ANNOTATE_EPISODE = "annotate_episode"
    RAISE_TICKET = "raise_ticket"
    SILENCE_CHANNEL = "silence_channel"
    REQUEST_RECALIBRATION = "request_recalibration"

    # Escalation: hand to a human. Always allowed, never automatic.
    ESCALATE_TO_HUMAN = "escalate_to_human"


class RiskClass(StrEnum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    ESCALATION = "escalation"


# The risk of each verb, fixed here rather than judged per call. A gate that re-derived risk
# from an action's parameters could be argued into a different answer by a well-chosen
# parameter; a lookup cannot.
RISK: dict[ActionKind, RiskClass] = {
    ActionKind.DESCRIBE_CHANNEL: RiskClass.READ_ONLY,
    ActionKind.FETCH_RECENT_READINGS: RiskClass.READ_ONLY,
    ActionKind.FETCH_PIPELINE_HEALTH: RiskClass.READ_ONLY,
    ActionKind.SEARCH_RUNBOOKS: RiskClass.READ_ONLY,
    ActionKind.ANNOTATE_EPISODE: RiskClass.REVERSIBLE,
    ActionKind.RAISE_TICKET: RiskClass.REVERSIBLE,
    ActionKind.SILENCE_CHANNEL: RiskClass.REVERSIBLE,
    ActionKind.REQUEST_RECALIBRATION: RiskClass.REVERSIBLE,
    ActionKind.ESCALATE_TO_HUMAN: RiskClass.ESCALATION,
}

# Required parameters per verb. Absence is a rejection, not a default: an action missing the
# field that says *what* it applies to is not a partially-specified action, it is one whose
# blast radius is unknown.
REQUIRED: dict[ActionKind, tuple[str, ...]] = {
    ActionKind.DESCRIBE_CHANNEL: ("channel",),
    ActionKind.FETCH_RECENT_READINGS: ("channel", "minutes"),
    ActionKind.FETCH_PIPELINE_HEALTH: ("window_start_ms",),
    ActionKind.SEARCH_RUNBOOKS: ("query",),
    ActionKind.ANNOTATE_EPISODE: ("episode_id", "note"),
    ActionKind.RAISE_TICKET: ("episode_id", "summary", "severity"),
    ActionKind.SILENCE_CHANNEL: ("channel", "minutes", "reason"),
    ActionKind.REQUEST_RECALIBRATION: ("channel", "reason"),
    ActionKind.ESCALATE_TO_HUMAN: ("episode_id", "reason"),
}


@dataclass(frozen=True)
class Action:
    """One thing the agent proposes to do."""

    kind: ActionKind
    parameters: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    @property
    def risk(self) -> RiskClass:
        return RISK[self.kind]

    @property
    def is_read_only(self) -> bool:
        return self.risk is RiskClass.READ_ONLY

    def missing_parameters(self) -> tuple[str, ...]:
        required = REQUIRED.get(self.kind, ())
        return tuple(k for k in required if k not in self.parameters)

    def to_json(self) -> str:
        return json.dumps(
            {"kind": str(self.kind), "parameters": self.parameters, "rationale": self.rationale},
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> Action:
        """Parse a proposal. Raises on anything not in the closed set.

        An unknown verb is an error, never a pass-through. A gate that forwarded verbs it
        did not recognise would be a gate in name only -- and a model asked for an action
        will happily invent one.
        """
        d = json.loads(raw)
        kind_raw = d.get("kind")
        try:
            kind = ActionKind(kind_raw)
        except ValueError as exc:
            raise UnknownAction(
                f"{kind_raw!r} is not an action this agent may take. "
                f"Allowed: {', '.join(sorted(str(k) for k in ActionKind))}"
            ) from exc
        parameters = d.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise UnknownAction(f"parameters must be an object, got {type(parameters).__name__}")
        return cls(kind=kind, parameters=parameters, rationale=str(d.get("rationale", "")))


class UnknownAction(ValueError):
    """A proposal named a verb outside the closed set, or was malformed."""
