"""The remediation agent: a closed action set, a deterministic gate, and a sandbox.

The agent proposes; the gate decides; the sandbox executes. Nothing runs that has not passed
the gate, and the gate consults no model.
"""

from vigil.agent.actions import Action, ActionKind, RiskClass, UnknownAction
from vigil.agent.gate import GateDecision, GatePolicy, Reason, SafetyGate, Verdict
from vigil.agent.loop import (
    AgentRun,
    AgentStep,
    Diagnoser,
    Diagnosis,
    Planner,
    RemediationAgent,
    RunbookPlanner,
    Symptom,
)
from vigil.agent.runbooks import Passage, Retrieved, RunbookIndex, load_runbooks
from vigil.agent.sandbox import (
    ExecutionResult,
    NotApproved,
    SandboxExecutor,
    SandboxState,
    trace_json,
)

__all__ = [
    "Action",
    "ActionKind",
    "AgentRun",
    "AgentStep",
    "Diagnoser",
    "Diagnosis",
    "ExecutionResult",
    "GateDecision",
    "GatePolicy",
    "NotApproved",
    "Passage",
    "Planner",
    "Reason",
    "RemediationAgent",
    "Retrieved",
    "RiskClass",
    "RunbookIndex",
    "RunbookPlanner",
    "SafetyGate",
    "SandboxExecutor",
    "SandboxState",
    "Symptom",
    "UnknownAction",
    "Verdict",
    "load_runbooks",
    "trace_json",
]
