"""The remediation agent: a closed action set, a deterministic gate, and a sandbox.

The agent proposes; the gate decides; the sandbox executes. Nothing runs that has not passed
the gate, and the gate consults no model.
"""

from vigil.agent.actions import Action, ActionKind, RiskClass, UnknownAction
from vigil.agent.gate import GateDecision, GatePolicy, Reason, SafetyGate, Verdict

__all__ = [
    "Action",
    "ActionKind",
    "GateDecision",
    "GatePolicy",
    "Reason",
    "RiskClass",
    "SafetyGate",
    "UnknownAction",
    "Verdict",
]
