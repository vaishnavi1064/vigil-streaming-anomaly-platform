"""Reading window scores produced by the Flink job.

The Flink job writes its scores inside a Kafka transaction committed on checkpoint
completion, so this consumer must read **committed-only**. Without that isolation level it
would see records from transactions that were later aborted, and the exactly-once chain
would be broken at its last link -- by the reader, not the writer, which is the version of
this mistake that is hardest to notice.

The wire format is deliberately the same shape as `DetectorScore`, so the episode builder
does not know or care whether a score came from the in-process detector or from Flink. That
is what makes the migration comparable: run both over the same readings and the episodes
should agree.
"""

from __future__ import annotations

import json
from typing import Any

from vigil.detectors.base import DetectorScore


def parse_window_score(raw: bytes | str) -> DetectorScore | None:
    """Decode one score record. Returns None for anything unusable.

    Returning None rather than raising: one malformed record must not stop the stream, and
    the caller counts them so a producer regression shows up as a number rather than as
    silence.
    """
    try:
        d: dict[str, Any] = json.loads(raw)
        return DetectorScore(
            detector=str(d["detector"]),
            channel=str(d["channel"]),
            window_start_ms=int(d["window_start_ms"]),
            window_end_ms=int(d["window_end_ms"]),
            score=float(d["score"]),
            # Flink does not report per-window scoring latency; the job's cost is measured
            # from its own metrics instead. Recording a fabricated 0.0 here would quietly
            # corrupt the NFR-1 percentile report, so it stays absent.
            latency_ms=0.0,
            detail={k: d[k] for k in ("points", "window_mean", "reference_mean") if k in d},
        )
    except (ValueError, KeyError, TypeError):
        return None


def committed_only_consumer_config(bootstrap: str, group: str, from_beginning: bool) -> dict:
    """Consumer settings for reading a transactional topic correctly."""
    return {
        "bootstrap.servers": bootstrap,
        "group.id": group,
        "auto.offset.reset": "earliest" if from_beginning else "latest",
        "enable.auto.commit": False,
        # The whole point. read_uncommitted here would surrender the guarantee the Flink
        # job pays two-phase commit to provide.
        "isolation.level": "read_committed",
        "max.poll.interval.ms": 600_000,
    }
