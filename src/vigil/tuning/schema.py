"""The training task, defined precisely before anything is generated for it.

What the fine-tuned model is asked to do: given an **episode** and the **runbook passages
retrieved for it**, emit a JSON list of actions from the closed set. That is the whole task.
It is not asked to diagnose in prose, not asked to decide whether to act, and not asked to
judge safety -- the gate does that and would overrule it anyway (ADR-027).

Why this framing rather than a chat format: the agent's failure modes are structural, not
stylistic. A planner that emits prose has to be parsed; a planner that emits an unknown verb
has to be rejected; a planner that proposes an action no runbook licenses is ungrounded.
Making the target a strict JSON list means every one of those is measurable as a rate rather
than as an impression, and the eval in `evaluate_planner.py` reports exactly those rates.

**The honest framing of what fine-tuning buys.** The training targets come largely from the
deterministic planner, so this is in substantial part **distillation of a rule set into a
model**. That is a real technique with a real payoff -- the model should generalise to
phrasings, channel names and symptom descriptions the rules were never written for -- but it
would be dishonest to present it as the model discovering policy. The eval measures whether
it actually generalises, by holding out symptom/channel combinations the rules handle only
by falling through to escalation. If it does not beat the rules there, that is the finding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from vigil.agent.actions import REQUIRED, Action, ActionKind

SYSTEM_PROMPT = """You are the planning step of an operational anomaly-remediation agent.

You are given one detected episode and the runbook passages retrieved for it. Reply with a \
JSON array of actions and nothing else -- no prose, no markdown fence, no explanation \
outside the array.

Each action is an object: {"kind": <verb>, "parameters": {...}, "rationale": <short string>}

Permitted verbs and their required parameters:
  describe_channel        channel
  fetch_recent_readings   channel, minutes
  fetch_pipeline_health   window_start_ms
  search_runbooks         query
  annotate_episode        episode_id, note
  raise_ticket            episode_id, summary, severity   (severity: low | medium | high)
  silence_channel         channel, minutes, reason
  request_recalibration   channel, reason
  escalate_to_human       episode_id, reason

Rules you must follow:
  - Propose ONLY actions that the retrieved runbook passages list under licensed-actions.
  - If no passage is retrieved, or none licenses a useful action, reply with a single \
escalate_to_human.
  - Never propose any action other than escalate_to_human and describe_channel for a \
channel whose name contains safety, interlock, fire, or emergency.
  - Gather evidence before changing state.
  - Every action must concern the episode's own channel and episode id."""


@dataclass
class TrainingExample:
    """One prompt/response pair, with the metadata the eval needs to score it."""

    prompt: str
    response: str
    # Everything below is for evaluation and curation, never shown to the model.
    episode_id: int
    channel: str
    symptom: str
    licensed: tuple[str, ...]
    is_safety_channel: bool
    source: str
    split: str = "train"
    notes: str = ""

    def to_jsonl(self) -> str:
        return json.dumps(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self.prompt},
                    {"role": "assistant", "content": self.response},
                ],
                "meta": {
                    "episode_id": self.episode_id,
                    "channel": self.channel,
                    "symptom": self.symptom,
                    "licensed": list(self.licensed),
                    "is_safety_channel": self.is_safety_channel,
                    "source": self.source,
                    "split": self.split,
                    "notes": self.notes,
                },
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_jsonl(cls, line: str) -> TrainingExample:
        d = json.loads(line)
        messages = d["messages"]
        meta = d["meta"]
        return cls(
            prompt=next(m["content"] for m in messages if m["role"] == "user"),
            response=next(m["content"] for m in messages if m["role"] == "assistant"),
            episode_id=meta["episode_id"],
            channel=meta["channel"],
            symptom=meta["symptom"],
            licensed=tuple(meta["licensed"]),
            is_safety_channel=meta["is_safety_channel"],
            source=meta["source"],
            split=meta.get("split", "train"),
            notes=meta.get("notes", ""),
        )


def render_prompt(
    *,
    episode_id: int,
    channel: str,
    peak_score: float,
    window_count: int,
    duration_s: float,
    status: str,
    diagnosis_summary: str,
    passages: list[tuple[str, str, tuple[str, ...]]],
    context: str | None = None,
) -> str:
    """Build the user turn.

    Deliberately terse and machine-shaped rather than conversational. The model is a
    component in a pipeline, and a prompt that reads like a chat invites a reply that reads
    like one -- which then has to be parsed out of prose.
    """
    lines = [
        "EPISODE",
        f"  id: {episode_id}",
        f"  channel: {channel}",
        f"  status: {status}",
        f"  peak_score: {peak_score:.1f}",
        f"  windows: {window_count}",
        f"  duration_s: {duration_s:.0f}",
        f"  observed: {diagnosis_summary}",
    ]
    if context:
        lines += ["", "OPERATIONAL CONTEXT", f"  {context}"]

    lines += ["", "RETRIEVED RUNBOOK PASSAGES"]
    if not passages:
        lines.append("  (none matched)")
    else:
        for runbook, title, licensed in passages:
            lines.append(f"  - {runbook}: {title}")
            lines.append(f"    licensed-actions: {', '.join(licensed) if licensed else '(none)'}")
    return "\n".join(lines)


def render_response(actions: list[Action]) -> str:
    """The target completion: a compact JSON array, nothing else."""
    return json.dumps(
        [
            {"kind": str(a.kind), "parameters": a.parameters, "rationale": a.rationale}
            for a in actions
        ],
        separators=(",", ":"),
    )


@dataclass
class ParsedPlan:
    """What came back from a model, and what was wrong with it if anything."""

    raw: str
    actions: list[Action] = field(default_factory=list)
    valid_json: bool = False
    is_array: bool = False
    unknown_verbs: tuple[str, ...] = ()
    malformed_entries: int = 0
    missing_parameters: tuple[str, ...] = ()

    @property
    def schema_valid(self) -> bool:
        return (
            self.valid_json
            and self.is_array
            and not self.unknown_verbs
            and not self.malformed_entries
            and not self.missing_parameters
        )


def parse_plan(raw: str) -> ParsedPlan:
    """Parse a model's reply into actions, recording every way it was wrong.

    Records rather than raises, because the eval's job is to report rates -- how often the
    model emits invalid JSON, how often it invents a verb -- and an exception would collapse
    all of those into "it failed".
    """
    parsed = ParsedPlan(raw=raw)
    text = _strip_fence(raw.strip())
    try:
        payload: Any = json.loads(text)
    except ValueError:
        return parsed
    parsed.valid_json = True

    if not isinstance(payload, list):
        return parsed
    parsed.is_array = True

    unknown: list[str] = []
    missing: list[str] = []
    for entry in payload:
        if not isinstance(entry, dict) or "kind" not in entry:
            parsed.malformed_entries += 1
            continue
        try:
            kind = ActionKind(entry["kind"])
        except ValueError:
            unknown.append(str(entry["kind"]))
            continue
        parameters = entry.get("parameters")
        if not isinstance(parameters, dict):
            parsed.malformed_entries += 1
            continue
        action = Action(kind=kind, parameters=parameters, rationale=str(entry.get("rationale", "")))
        for name in REQUIRED[kind]:
            if name not in parameters:
                missing.append(f"{kind}.{name}")
        parsed.actions.append(action)

    parsed.unknown_verbs = tuple(unknown)
    parsed.missing_parameters = tuple(missing)
    return parsed


def _strip_fence(text: str) -> str:
    """Remove a markdown code fence if the model added one.

    Tolerated at parse time and *counted* separately in the eval: it is a formatting failure
    against an explicit instruction, not a planning failure, and conflating the two would
    understate how well the model plans.
    """
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def has_fence(raw: str) -> bool:
    return raw.strip().startswith("```")
