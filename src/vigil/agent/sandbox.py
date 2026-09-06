"""The sandbox: where approved actions actually happen, and nowhere else.

NFR-11 requires that agent actions run only in a sandbox with no unsandboxed side effects.
That is enforced two ways, because one is not enough:

**By construction.** The executor holds no credentials for anything outside its own record,
opens no sockets, and writes to no filesystem path outside the store it was handed. There is
no code path from an `Action` to a shell, a plant control system, or the readings topic. The
closed action set is what makes this checkable -- with arbitrary commands it would not be.

**By interlock.** `execute()` refuses anything without an approval from the gate. Not "logs
a warning" -- refuses. An executor that could be called directly would make the gate
advisory, and NFR-11's "100%" would depend on every future caller remembering to ask.

What "sandbox" means concretely here: read actions return data from the platform's own
stores, and write actions record an *intent* -- a ticket that was raised, a channel that was
silenced -- in the agent's own tables. Nothing reaches the simulated plant, because the
plant is a public feed we do not own and must not act on. That boundary is real and is the
honest limit of this implementation: `docs/BLOCKERS.md` records that a production deployment
would need a genuinely isolated target environment, and that this one does not have one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from vigil.agent.actions import Action, ActionKind, RiskClass
from vigil.agent.gate import GateDecision


class NotApproved(PermissionError):
    """Something tried to execute an action the gate had not approved."""


@dataclass
class ExecutionResult:
    action: Action
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
    # What the action changed outside itself. Empty for reads. This is what a reviewer reads
    # to answer "what did the agent actually do", so it is recorded per action rather than
    # inferred afterwards from logs.
    effects: tuple[str, ...] = ()


@dataclass
class SandboxState:
    """Everything the sandbox may mutate. Deliberately small and entirely in-process.

    Making the mutable surface an explicit object rather than scattered writes means the
    answer to "what can the agent change" is this class, and it can be read in a minute.
    """

    silenced: dict[str, dict[str, Any]] = field(default_factory=dict)
    tickets: list[dict[str, Any]] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)
    recalibration_requests: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[dict[str, Any]] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "silenced": dict(self.silenced),
            "tickets": list(self.tickets),
            "annotations": list(self.annotations),
            "recalibration_requests": list(self.recalibration_requests),
            "escalations": list(self.escalations),
        }


class SandboxExecutor:
    """Runs approved actions against in-process state and the platform's read-only stores.

    `reader` is an optional object exposing the platform's own queries. It is passed in
    rather than constructed here so the executor can be run with no reader at all -- which is
    what the tests do, and what proves the executor cannot reach anything it was not given.
    """

    def __init__(self, state: SandboxState | None = None, reader=None) -> None:
        self.state = state or SandboxState()
        self.reader = reader
        self.executed = 0
        self.refused = 0
        self.failed = 0

    def execute(self, decision: GateDecision, episode_id: int) -> ExecutionResult:
        """Run an approved action. Raises if it was not approved.

        Taking the gate's decision rather than the action is the interlock: there is no
        signature that accepts a bare action, so an approval cannot be forgotten.
        """
        if not decision.approved:
            self.refused += 1
            raise NotApproved(
                f"{decision.action.kind} was {decision.verdict} ({decision.reason}): "
                f"{decision.detail}"
            )

        action = decision.action
        started = time.perf_counter()
        try:
            output, effects = self._dispatch(action, episode_id)
            self.executed += 1
            return ExecutionResult(
                action=action,
                ok=True,
                output=output,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                effects=effects,
            )
        except Exception as exc:  # noqa: BLE001 - a failed action must not stop the loop
            self.failed += 1
            return ExecutionResult(
                action=action,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

    def _dispatch(self, action: Action, episode_id: int) -> tuple[dict[str, Any], tuple[str, ...]]:
        p = action.parameters
        match action.kind:
            case ActionKind.DESCRIBE_CHANNEL:
                return self._read("describe_channel", channel=p["channel"]), ()
            case ActionKind.FETCH_RECENT_READINGS:
                return (
                    self._read("recent_readings", channel=p["channel"], minutes=int(p["minutes"])),
                    (),
                )
            case ActionKind.FETCH_PIPELINE_HEALTH:
                return (
                    self._read("pipeline_health", window_start_ms=int(p["window_start_ms"])),
                    (),
                )
            case ActionKind.SEARCH_RUNBOOKS:
                return self._read("search_runbooks", query=p["query"]), ()

            case ActionKind.ANNOTATE_EPISODE:
                record = {"episode_id": episode_id, "note": p["note"], "at": time.time()}
                self.state.annotations.append(record)
                return record, (f"annotated episode {episode_id}",)

            case ActionKind.RAISE_TICKET:
                ticket = {
                    "id": f"TICKET-{len(self.state.tickets) + 1:04d}",
                    "episode_id": episode_id,
                    "summary": p["summary"],
                    "severity": str(p["severity"]).lower(),
                    "at": time.time(),
                }
                self.state.tickets.append(ticket)
                return ticket, (f"raised {ticket['id']} at severity {ticket['severity']}",)

            case ActionKind.SILENCE_CHANNEL:
                until = time.time() + int(p["minutes"]) * 60
                record = {
                    "channel": p["channel"],
                    "until": until,
                    "minutes": int(p["minutes"]),
                    "reason": p["reason"],
                    "episode_id": episode_id,
                }
                self.state.silenced[p["channel"]] = record
                return record, (f"silenced {p['channel']} for {p['minutes']} minutes",)

            case ActionKind.REQUEST_RECALIBRATION:
                # A request, not a recalibration. The agent asks; a human with physical
                # access acts. Anything that touched the instrument directly would be
                # outside the sandbox by definition.
                record = {
                    "channel": p["channel"],
                    "reason": p["reason"],
                    "episode_id": episode_id,
                    "status": "requested",
                    "at": time.time(),
                }
                self.state.recalibration_requests.append(record)
                return record, (f"requested recalibration of {p['channel']}",)

            case ActionKind.ESCALATE_TO_HUMAN:
                record = {
                    "episode_id": episode_id,
                    "reason": p["reason"],
                    "at": time.time(),
                }
                self.state.escalations.append(record)
                return record, (f"escalated episode {episode_id} to a human",)

        raise NotImplementedError(f"no sandbox implementation for {action.kind}")

    def _read(self, query: str, **kwargs) -> dict[str, Any]:
        if self.reader is None:
            # Explicit rather than an empty result: "there was no reader" and "the reader
            # found nothing" are different facts, and a trace that conflated them would be
            # unreadable.
            return {"query": query, "arguments": kwargs, "available": False}
        return {
            "query": query,
            "arguments": kwargs,
            "available": True,
            "result": self.reader.query(query, **kwargs),
        }

    def summary(self) -> str:
        return (
            f"sandbox: executed {self.executed:,} | failed {self.failed:,} | "
            f"refused (not approved) {self.refused:,} | "
            f"tickets {len(self.state.tickets)} | silenced {len(self.state.silenced)} | "
            f"escalations {len(self.state.escalations)}"
        )


def action_effects_are_reversible(result: ExecutionResult) -> bool:
    """Every effect this sandbox can produce is undoable by deleting a record."""
    return result.action.risk is not RiskClass.READ_ONLY or not result.effects


def trace_json(decision: GateDecision, result: ExecutionResult | None, episode_id: int) -> str:
    """The auditable record of one step: what was proposed, judged, and done.

    The rationale is included here even though the gate never read it -- a human reviewing
    the trace should see the argument the agent made, precisely because the gate did not.
    """
    return json.dumps(
        {
            "episode_id": episode_id,
            "action": json.loads(decision.action.to_json()),
            "gate": {
                "verdict": str(decision.verdict),
                "reason": str(decision.reason),
                "detail": decision.detail,
            },
            "execution": (
                None
                if result is None
                else {
                    "ok": result.ok,
                    "error": result.error,
                    "duration_ms": round(result.duration_ms, 3),
                    "effects": list(result.effects),
                    "output": result.output,
                }
            ),
        },
        separators=(",", ":"),
        default=str,
    )
