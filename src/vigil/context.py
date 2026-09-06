"""Context events: the operational facts a detector normally cannot see.

A context event says "something happened to this system between t_start and t_end that can
explain an excursion in the telemetry". Two kinds exist in v1: `deploy` markers, and
`pipeline` disturbances emitted by the reconciliation harness in Phase 2. Both travel the
same wire and are consumed through the same interface, which is what makes the mechanism
extensible rather than a special case per signal (ADR-003).

An event is a *claim about the world*, not a verdict about any anomaly. Deciding what an
overlapping anomaly means is the conditioning policy's job in Phase 3. Keeping those
separate is what allows a context event to exist without an anomaly, and an anomaly to be
raised during a context event -- both of which have to happen for the evaluation to be
able to tell targeted suppression from blanket muting (ADR-015).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ContextKind(StrEnum):
    DEPLOY = "deploy"
    PIPELINE = "pipeline"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ContextEvent:
    """One operational disturbance, bounded in time and scope."""

    event_id: str
    kind: ContextKind
    t_start_ms: int
    t_end_ms: int
    severity: Severity
    detail: str
    # Channels this event can explain. Empty means fleet-wide. Scope matters: a deploy
    # that touched one service must not be allowed to explain an excursion on an unrelated
    # channel, which is the cheapest way for suppression to become over-suppression.
    scope: tuple[str, ...] = ()

    # Ground truth, present only on synthetically generated events: whether this
    # disturbance actually perturbed the telemetry. Quiet events -- real deploys that
    # changed nothing observable -- are what make blanket suppression detectable.
    # No conditioning policy reads this; the evaluation harness does.
    perturbed_telemetry: bool | None = None

    def covers(self, t_ms: int) -> bool:
        return self.t_start_ms <= t_ms <= self.t_end_ms

    def overlaps(self, start_ms: int, end_ms: int) -> bool:
        return self.t_start_ms <= end_ms and start_ms <= self.t_end_ms

    def applies_to(self, channel: str) -> bool:
        return not self.scope or channel in self.scope

    def to_json(self) -> bytes:
        payload: dict[str, Any] = {
            "event_id": self.event_id,
            "kind": str(self.kind),
            "t_start_ms": self.t_start_ms,
            "t_end_ms": self.t_end_ms,
            "severity": str(self.severity),
            "detail": self.detail,
        }
        if self.scope:
            payload["scope"] = list(self.scope)
        if self.perturbed_telemetry is not None:
            payload["perturbed_telemetry"] = self.perturbed_telemetry
        return json.dumps(payload, separators=(",", ":")).encode()

    @classmethod
    def from_json(cls, raw: bytes | str) -> ContextEvent:
        d = json.loads(raw)
        return cls(
            event_id=d["event_id"],
            kind=ContextKind(d["kind"]),
            t_start_ms=int(d["t_start_ms"]),
            t_end_ms=int(d["t_end_ms"]),
            severity=Severity(d["severity"]),
            detail=d["detail"],
            scope=tuple(d.get("scope", ())),
            perturbed_telemetry=d.get("perturbed_telemetry"),
        )
