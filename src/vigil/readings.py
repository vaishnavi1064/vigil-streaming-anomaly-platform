"""The reading record and its wire codec.

One place defines the wire format, so the producer, the consumer, the reconciliation
harness and Flink cannot drift apart in their understanding of a message.

`seq` is per channel and strictly increasing. It is the identity the reconciliation
harness checks invariants against: a missing seq is a gap, a repeated seq is a duplicate.
Counting messages alone would not distinguish "we processed 1,000,000 events" from
"we processed one event a million times", which is the whole point of per-stage identity
invariants rather than row counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Reading:
    channel: str
    seq: int
    event_ts_ms: int
    value: float
    # Ground truth from the synthetic source only. No detector reads either field; the
    # evaluation harness does. On a real feed both are always None.
    #
    # `injected` is the shape of the excursion (spike, level_shift, variance_burst).
    # `origin` is what caused it, and it is the field the whole evaluation turns on: a
    # 'fault' must be detected even when a deploy is in flight, while a 'deploy' or
    # 'pipeline' artifact is what conditioning is allowed to attribute away. Collapsing
    # the two into one label would make targeted attribution indistinguishable from
    # blanket muting (ADR-015).
    injected: str | None = None
    origin: str | None = None

    def to_json(self) -> bytes:
        payload: dict[str, Any] = {
            "channel": self.channel,
            "seq": self.seq,
            "ts": self.event_ts_ms,
            "value": self.value,
        }
        if self.injected is not None:
            payload["injected"] = self.injected
        if self.origin is not None:
            payload["origin"] = self.origin
        return json.dumps(payload, separators=(",", ":")).encode()

    @classmethod
    def from_json(cls, raw: bytes | str) -> Reading:
        d = json.loads(raw)
        return cls(
            channel=d["channel"],
            seq=int(d["seq"]),
            event_ts_ms=int(d["ts"]),
            value=float(d["value"]),
            injected=d.get("injected"),
            origin=d.get("origin"),
        )
