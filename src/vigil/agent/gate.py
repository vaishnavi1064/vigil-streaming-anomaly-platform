"""The safety gate: a deterministic, non-LLM verdict on every proposed action.

NFR-11 says 100% of agent actions pass this before running. That number is only meaningful
if the gate is something a model cannot talk its way past, so the gate has three properties
by construction:

**It is deterministic.** Same action, same episode, same verdict, always. No model is
consulted. `verdict()` is a pure function of its inputs, which is what lets it be tested
exhaustively rather than sampled.

**It is closed.** It approves from an allow-list, never denies from a deny-list. An action
it does not recognise is rejected, because a gate that forwards the unfamiliar is a gate
only for things someone already thought of -- and a model asked for an action will happily
invent one.

**It cannot be argued with.** The gate reads the action and the episode. It does not read
the agent's rationale, its confidence, or its diagnosis. A rationale is a channel through
which a model could persuade, and a component whose job is to be unpersuadable should not
have one. The rationale is logged for a human; it is not an input to the decision.

The gate can only ever *reduce* what happens. It approves, rejects, or downgrades a
proposal to something weaker -- it never upgrades one, never substitutes a different verb,
and never invents parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from vigil.agent.actions import Action, ActionKind, RiskClass
from vigil.episodes import Episode, EpisodeStatus


class Verdict(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class Reason(StrEnum):
    """Why the gate decided as it did. A closed set, so verdicts are aggregatable."""

    READ_ONLY = "read_only"
    WITHIN_LIMITS = "within_limits"
    ESCALATION_ALWAYS_ALLOWED = "escalation_always_allowed"

    MISSING_PARAMETERS = "missing_parameters"
    NOT_FOR_THIS_EPISODE = "not_for_this_episode"
    CHANNEL_MISMATCH = "channel_mismatch"
    LIMIT_EXCEEDED = "limit_exceeded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    EPISODE_NOT_ACTIONABLE = "episode_not_actionable"
    PROTECTED_CHANNEL = "protected_channel"
    NOT_IN_SANDBOX = "not_in_sandbox"


@dataclass(frozen=True)
class GateDecision:
    verdict: Verdict
    reason: Reason
    detail: str
    action: Action

    @property
    def approved(self) -> bool:
        return self.verdict is Verdict.APPROVED


@dataclass
class GatePolicy:
    """The limits. Values, not code, so they can be reviewed without reading logic."""

    # Nothing runs outside the sandbox. This is not configurable to False anywhere in the
    # codebase; it exists so the check is explicit at the point of decision rather than
    # implied by which executor happened to be wired in.
    require_sandbox: bool = True

    max_silence_minutes: int = 60
    max_readings_lookback_minutes: int = 240
    max_actions_per_episode: int = 6
    max_reversible_actions_per_episode: int = 2

    allowed_ticket_severities: frozenset[str] = frozenset({"low", "medium", "high"})

    # Channels the agent may never act on, however sound its reasoning. Silencing a safety
    # instrument is exactly the action a plausible-sounding chain of inference arrives at.
    protected_channel_substrings: tuple[str, ...] = (
        "safety",
        "interlock",
        "fire",
        "emergency",
    )


@dataclass
class SafetyGate:
    """Judges proposals. Keeps per-episode counters so budgets are enforceable."""

    policy: GatePolicy = field(default_factory=GatePolicy)
    sandbox: bool = True

    approved: int = field(default=0, init=False)
    rejected: int = field(default=0, init=False)
    reasons: dict[str, int] = field(default_factory=dict, init=False)
    _per_episode: dict[int, list[Action]] = field(default_factory=dict, init=False, repr=False)

    def verdict(self, action: Action, episode: Episode, episode_id: int) -> GateDecision:
        """The whole decision. Pure with respect to everything except the episode budget."""
        taken = self._per_episode.get(episode_id, [])

        # -- structural checks, before anything about the episode --
        missing = action.missing_parameters()
        if missing:
            return self._decide(
                action,
                Verdict.REJECTED,
                Reason.MISSING_PARAMETERS,
                f"{action.kind} needs {', '.join(missing)}",
            )

        if self.policy.require_sandbox and not self.sandbox:
            return self._decide(
                action,
                Verdict.REJECTED,
                Reason.NOT_IN_SANDBOX,
                "execution is not sandboxed; nothing is approved outside the sandbox",
            )

        # -- escalation is always available, and never counts against a budget --
        if action.kind is ActionKind.ESCALATE_TO_HUMAN:
            if action.parameters.get("episode_id") != episode_id:
                return self._decide(
                    action,
                    Verdict.REJECTED,
                    Reason.NOT_FOR_THIS_EPISODE,
                    f"escalation names episode {action.parameters.get('episode_id')}, "
                    f"not {episode_id}",
                )
            return self._decide(
                action,
                Verdict.APPROVED,
                Reason.ESCALATION_ALWAYS_ALLOWED,
                "handing to a human is always permitted",
            )

        if len(taken) >= self.policy.max_actions_per_episode:
            return self._decide(
                action,
                Verdict.REJECTED,
                Reason.BUDGET_EXHAUSTED,
                f"episode {episode_id} has already had "
                f"{len(taken)}/{self.policy.max_actions_per_episode} actions; "
                f"escalate instead of continuing",
            )

        # -- read-only actions still have to be about this episode --
        if action.is_read_only:
            return self._check_read_only(action, episode, episode_id)

        return self._check_reversible(action, episode, episode_id, taken)

    def _check_read_only(self, action: Action, episode: Episode, episode_id: int) -> GateDecision:
        channel = action.parameters.get("channel")
        if channel is not None and channel != episode.channel:
            # Reading another channel is not dangerous, but it is out of scope, and an agent
            # wandering the fleet is one whose traces stop being reviewable.
            return self._decide(
                action,
                Verdict.REJECTED,
                Reason.CHANNEL_MISMATCH,
                f"episode {episode_id} is about {episode.channel}, not {channel}",
            )
        if action.kind is ActionKind.FETCH_RECENT_READINGS:
            minutes = _as_int(action.parameters.get("minutes"))
            if minutes is None or minutes <= 0:
                return self._decide(
                    action, Verdict.REJECTED, Reason.MISSING_PARAMETERS, "minutes must be positive"
                )
            if minutes > self.policy.max_readings_lookback_minutes:
                return self._decide(
                    action,
                    Verdict.REJECTED,
                    Reason.LIMIT_EXCEEDED,
                    f"{minutes} minutes exceeds the "
                    f"{self.policy.max_readings_lookback_minutes} minute lookback limit",
                )
        return self._decide(
            action, Verdict.APPROVED, Reason.READ_ONLY, "read-only, scoped to this episode"
        )

    def _check_reversible(
        self, action: Action, episode: Episode, episode_id: int, taken: list[Action]
    ) -> GateDecision:
        # An episode conditioning already explained is not something to act on. Acting on it
        # would undo the core contribution: attributing an artifact to its cause and then
        # silencing the channel anyway is worse than not attributing at all.
        if episode.status is not EpisodeStatus.REAL:
            return self._decide(
                action,
                Verdict.REJECTED,
                Reason.EPISODE_NOT_ACTIONABLE,
                f"episode {episode_id} is {episode.status}"
                + (f" (attributed to {episode.attributed_to})" if episode.attributed_to else "")
                + "; no remediation is warranted",
            )

        reversible_taken = sum(1 for a in taken if a.risk is RiskClass.REVERSIBLE)
        if reversible_taken >= self.policy.max_reversible_actions_per_episode:
            return self._decide(
                action,
                Verdict.REJECTED,
                Reason.BUDGET_EXHAUSTED,
                f"episode {episode_id} has already had {reversible_taken} state-changing "
                f"actions; the limit is {self.policy.max_reversible_actions_per_episode}",
            )

        channel = action.parameters.get("channel")
        if channel is not None:
            if channel != episode.channel:
                return self._decide(
                    action,
                    Verdict.REJECTED,
                    Reason.CHANNEL_MISMATCH,
                    f"episode {episode_id} is about {episode.channel}, not {channel}",
                )
            lowered = channel.lower()
            for marker in self.policy.protected_channel_substrings:
                if marker in lowered:
                    return self._decide(
                        action,
                        Verdict.REJECTED,
                        Reason.PROTECTED_CHANNEL,
                        f"{channel} is protected ({marker!r}); no automated action is "
                        f"permitted on it regardless of the reasoning",
                    )

        episode_param = action.parameters.get("episode_id")
        if episode_param is not None and episode_param != episode_id:
            return self._decide(
                action,
                Verdict.REJECTED,
                Reason.NOT_FOR_THIS_EPISODE,
                f"action names episode {episode_param}, not {episode_id}",
            )

        if action.kind is ActionKind.SILENCE_CHANNEL:
            minutes = _as_int(action.parameters.get("minutes"))
            if minutes is None or minutes <= 0:
                return self._decide(
                    action, Verdict.REJECTED, Reason.MISSING_PARAMETERS, "minutes must be positive"
                )
            if minutes > self.policy.max_silence_minutes:
                return self._decide(
                    action,
                    Verdict.REJECTED,
                    Reason.LIMIT_EXCEEDED,
                    f"silencing for {minutes} minutes exceeds the "
                    f"{self.policy.max_silence_minutes} minute limit",
                )

        if action.kind is ActionKind.RAISE_TICKET:
            severity = str(action.parameters.get("severity", "")).lower()
            if severity not in self.policy.allowed_ticket_severities:
                return self._decide(
                    action,
                    Verdict.REJECTED,
                    Reason.LIMIT_EXCEEDED,
                    f"severity {severity!r} is not one of "
                    f"{sorted(self.policy.allowed_ticket_severities)}",
                )

        return self._decide(
            action, Verdict.APPROVED, Reason.WITHIN_LIMITS, "reversible and within every limit"
        )

    def record_taken(self, episode_id: int, action: Action) -> None:
        """Called by the executor after an approved action actually ran.

        Separate from `verdict` on purpose: a proposal that was approved but never executed
        must not consume budget, or a failing executor would starve an episode of the
        actions it still needs.
        """
        self._per_episode.setdefault(episode_id, []).append(action)

    def _decide(
        self, action: Action, verdict: Verdict, reason: Reason, detail: str
    ) -> GateDecision:
        if verdict is Verdict.APPROVED:
            self.approved += 1
        else:
            self.rejected += 1
        self.reasons[str(reason)] = self.reasons.get(str(reason), 0) + 1
        return GateDecision(verdict=verdict, reason=reason, detail=detail, action=action)

    def summary(self) -> str:
        total = self.approved + self.rejected
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(self.reasons.items()))
        return (
            f"gate: {total:,} judged | {self.approved:,} approved | {self.rejected:,} rejected"
            + (f" | {breakdown}" if breakdown else "")
        )


def _as_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
