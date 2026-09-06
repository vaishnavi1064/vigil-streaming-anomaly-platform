"""The synthetic fleet as a reading source.

This is the harness, not the showcase. The live solar feed proves the pipeline runs on
genuinely unseen data; this one is what makes throughput, chaos and correctness tests
repeatable, because it is seeded, it has ground-truth labels, and it can be driven at an
exact rate or as fast as the machine allows -- none of which a public feed can offer.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from vigil.ingest.source import ReadingSource, SequenceAssigner
from vigil.readings import Reading
from vigil.synthetic import ChannelSimulator, default_fleet


class SyntheticFleetSource(ReadingSource):
    """Readings from a seeded synthetic fleet, at a target rate or unpaced.

    `rate_per_s = 0` means blast: produce as fast as possible, with event time taken from
    the wall clock. Any other rate paces against a virtual clock, so event timestamps are
    evenly spaced and windowed detection sees a realistic timeline.
    """

    def __init__(
        self,
        *,
        channels: int = 8,
        rate_per_s: float = 2000.0,
        seed: int = 1729,
        anomalies_per_hour: float = 12.0,
        duration_s: float = 0.0,
    ) -> None:
        self.name = f"synthetic-fleet:{channels}ch"
        self.specs = default_fleet(channels, seed=seed)
        self._sims = [
            ChannelSimulator(spec, seed=seed + i, anomalies_per_hour=anomalies_per_hour)
            for i, spec in enumerate(self.specs)
        ]
        self.sequences = SequenceAssigner()
        self.rate_per_s = rate_per_s
        self.duration_s = duration_s
        self._stopped = False

    @property
    def blast(self) -> bool:
        return self.rate_per_s == 0

    def readings(self) -> Iterator[Reading]:
        per_event_s = 0.0 if self.blast else 1.0 / self.rate_per_s
        t0_wall = time.time()
        start = time.perf_counter()
        deadline = start + self.duration_s if self.duration_s > 0 else float("inf")

        k = 0
        while not self._stopped and time.perf_counter() < deadline:
            idx = k % len(self._sims)
            if self.blast:
                event_ts_ms = int(time.time() * 1000)
                stream_t_s = time.perf_counter() - start
            else:
                stream_t_s = k * per_event_s
                event_ts_ms = int((t0_wall + stream_t_s) * 1000)

            sample = self._sims[idx].sample(stream_t_s)
            channel = self.specs[idx].name
            yield Reading(
                channel=channel,
                seq=self.sequences.next_for(channel),
                event_ts_ms=event_ts_ms,
                value=sample.value,
                injected=sample.injected.value if sample.injected else None,
            )

            k += 1
            if not self.blast:
                # Pace against the virtual clock rather than sleeping a fixed slice, so
                # scheduler jitter cannot accumulate into rate drift over a long run.
                slack = (start + k * per_event_s) - time.perf_counter()
                if slack > 0.0005:
                    time.sleep(slack)

    def close(self) -> None:
        self._stopped = True
