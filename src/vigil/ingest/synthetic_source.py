"""The synthetic fleet as a reading source.

This is the harness, not the showcase. The live solar feed proves the pipeline runs on
genuinely unseen data; this one is what makes throughput, chaos and correctness tests
repeatable, because it is seeded, it has ground-truth labels, and it can be driven at an
exact rate or as fast as the machine allows -- none of which a public feed can offer.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from vigil.context import ContextEvent
from vigil.ingest.source import ReadingSource, SequenceAssigner
from vigil.readings import Reading
from vigil.scenario import ScenarioPlan, build_scenario
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
        scenario: bool = False,
        deploys_per_hour: float = 30.0,
        faults_per_hour: float = 40.0,
        quiet_deploy_fraction: float | None = None,
        fault_in_window_fraction: float | None = None,
    ) -> None:
        self.name = f"synthetic-fleet:{channels}ch"
        self.specs = default_fleet(channels, seed=seed)
        self.plan: ScenarioPlan | None = None

        if scenario:
            if duration_s <= 0:
                raise ValueError(
                    "a scenario run needs --duration: the plan is scheduled up front so "
                    "the ground truth is fixed before a single reading is produced"
                )
            kwargs = {}
            if quiet_deploy_fraction is not None:
                kwargs["quiet_deploy_fraction"] = quiet_deploy_fraction
            if fault_in_window_fraction is not None:
                kwargs["fault_in_window_fraction"] = fault_in_window_fraction
            self.plan = build_scenario(
                tuple(spec.name for spec in self.specs),
                duration_s,
                seed=seed,
                deploys_per_hour=deploys_per_hour,
                faults_per_hour=faults_per_hour,
                **kwargs,
            )
            # Poisson anomalies are switched off under a scenario: the plan is the ground
            # truth the evaluation scores against, and unplanned excursions would show up
            # as unexplained detections that nothing can account for.
            self._sims = [
                ChannelSimulator(
                    spec,
                    seed=seed + i,
                    anomalies_per_hour=0.0,
                    scheduled=self.plan.episodes_for(spec.name),
                )
                for i, spec in enumerate(self.specs)
            ]
        else:
            self._sims = [
                ChannelSimulator(spec, seed=seed + i, anomalies_per_hour=anomalies_per_hour)
                for i, spec in enumerate(self.specs)
            ]

        self.sequences = SequenceAssigner()
        self.rate_per_s = rate_per_s
        self.duration_s = duration_s
        self._stopped = False
        self._pending_context: list[ContextEvent] = (
            self.plan.context_events() if self.plan else []
        )
        self._context_at = 0
        self._t0_wall_ms = 0

    @property
    def t0_wall_ms(self) -> int:
        """Wall-clock anchor for the run, set when readings() starts; 0 before that."""
        return self._t0_wall_ms

    def due_context_events(self, stream_t_s: float) -> list[ContextEvent]:
        """Context events whose start time the stream has now reached.

        Markers are published as the run passes them rather than dumped up front, because
        a conditioning policy that could see the whole future would be solving a different
        and much easier problem than the one this system faces.
        """
        out: list[ContextEvent] = []
        while self._context_at < len(self._pending_context):
            event = self._pending_context[self._context_at]
            if event.t_start_ms > stream_t_s * 1000:
                break
            self._context_at += 1
            out.append(
                ContextEvent(
                    event_id=event.event_id,
                    kind=event.kind,
                    t_start_ms=self._t0_wall_ms + event.t_start_ms,
                    t_end_ms=self._t0_wall_ms + event.t_end_ms,
                    severity=event.severity,
                    detail=event.detail,
                    scope=event.scope,
                    perturbed_telemetry=event.perturbed_telemetry,
                )
            )
        return out

    @property
    def blast(self) -> bool:
        return self.rate_per_s == 0

    def readings(self) -> Iterator[Reading]:
        per_event_s = 0.0 if self.blast else 1.0 / self.rate_per_s
        t0_wall = time.time()
        # Context event times are stream-relative; anchoring them here puts markers and
        # readings on one timeline so an overlap test means what it says.
        self._t0_wall_ms = int(t0_wall * 1000)
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
                origin=sample.origin.value if sample.origin else None,
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
