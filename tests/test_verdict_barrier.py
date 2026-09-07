"""The event-time barrier and the evidence it exists to wait for (G-7).

v1 to v3 of the conditioning measurement all found the same thing: the corroboration test
almost never concluded a channel had moved alone. The diagnosis was that it almost never
concluded anything -- the in-scope siblings that would have exonerated or corroborated an
episode had usually not closed yet when its verdict fired. These tests hold both halves of
the fix in place:

  1. A channel's departure is evidence the moment its first flagged window closes, not when
     its episode eventually ends. An excursion that runs for two minutes is evidence about
     its first second.
  2. A verdict waits behind a fleet watermark, so it is taken against the whole fleet's
     event time rather than against whichever channels happened to finish first.

The test that matters most runs the real detection spine over a stream built so a short
excursion closes long before the long one beside it, and asserts the short one's verdict saw
the long one -- evidence the closed-episode index could not have had.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from vigil.conditioning.barrier import CorroborationBarrier
from vigil.conditioning.policy import (
    ConditioningPolicy,
    ConditioningThresholds,
    FlaggedWindowIndex,
    Verdict,
)
from vigil.conditioning.signals import StaticContextSource
from vigil.context import ContextEvent, ContextKind, Severity
from vigil.detectors.base import DetectorScore
from vigil.episodes import Episode, EpisodeBuilder
from vigil.readings import Reading
from vigil.windows import SlidingWindowAssigner


def episode(channel: str, onset_ms: int, end_ms: int | None = None) -> Episode:
    start = (onset_ms // 10_000) * 10_000
    return Episode(
        channel=channel,
        t_start_ms=start,
        t_end_ms=end_ms if end_ms is not None else start + 30_000,
        raised_by="zscore",
        peak_score=40.0,
        window_count=3,
        threshold=8.0,
        onset_ms=onset_ms,
    )


# ------------------------- the barrier itself -------------------------


def test_a_verdict_waits_until_the_watermark_passes_the_buffer():
    barrier = CorroborationBarrier(buffer_ms=30_000)
    barrier.hold(episode("a", 100_000))
    assert barrier.release(120_000) == []
    assert barrier.pending == 1
    released = barrier.release(130_000)
    assert [e.channel for e in released] == ["a"]
    assert barrier.pending == 0


def test_the_buffer_is_measured_from_the_onset_not_the_episode_end():
    # A long episode has already waited out its buffer by the time it closes, so it is
    # released at once. The evidence that matters is what happened around the moment the
    # channel departed, and that is long past.
    barrier = CorroborationBarrier(buffer_ms=30_000)
    barrier.hold(episode("a", 100_000, end_ms=400_000))
    assert [e.channel for e in barrier.release(131_000)] == ["a"]


def test_a_zero_buffer_reproduces_the_racing_behaviour_that_was_measured():
    # Kept as a switch rather than removed: v1-v3 are only reproducible with it.
    barrier = CorroborationBarrier(buffer_ms=0)
    barrier.hold(episode("a", 100_000))
    assert [e.channel for e in barrier.release(100_000)] == ["a"]


def test_released_episodes_come_out_in_onset_order():
    barrier = CorroborationBarrier(buffer_ms=10_000)
    for channel, onset in (("c", 30_000), ("a", 10_000), ("b", 20_000)):
        barrier.hold(episode(channel, onset))
    assert [e.channel for e in barrier.release(100_000)] == ["a", "b", "c"]


def test_a_stream_that_ends_releases_what_it_is_still_holding():
    # The last episodes of a bounded run have no data after them to advance the watermark
    # past their buffer. They are decided on the evidence that exists, and counted apart.
    barrier = CorroborationBarrier(buffer_ms=30_000)
    barrier.hold(episode("a", 100_000))
    assert barrier.release(110_000) == []
    assert [e.channel for e in barrier.flush()] == ["a"]
    assert barrier.released_on_flush == 1
    assert barrier.released_on_watermark == 0


def test_nothing_is_released_before_any_watermark_exists():
    barrier = CorroborationBarrier(buffer_ms=30_000)
    barrier.hold(episode("a", 100_000))
    assert barrier.release(None) == []


def test_the_hold_report_states_what_the_barrier_actually_cost():
    # The cost is the delay past the moment the episode closed, not the configured buffer:
    # an episode that outlasted its own buffer waited for nothing and must not be counted
    # as though it had.
    barrier = CorroborationBarrier(buffer_ms=30_000)
    barrier.hold(episode("a", 100_000, end_ms=130_000), watermark_ms=125_000)
    barrier.hold(episode("b", 100_000, end_ms=400_000), watermark_ms=140_000)
    barrier.release(140_000)
    report = barrier.hold_report()
    assert "buffer 30s" in report
    assert "delayed past episode close 1 of 2" in report
    assert "p95 15.0s" in report


# ------------------------- the fleet watermark -------------------------


def reading(channel: str, ts_ms: int, value: float = 0.0) -> Reading:
    return Reading(channel=channel, seq=ts_ms, event_ts_ms=ts_ms, value=value)


def test_the_fleet_watermark_is_the_slowest_channel_not_the_fastest():
    # A cross-channel question is only answerable once the slowest channel has passed the
    # span it asks about. Taking the maximum would decide against channels not yet heard.
    assigner = SlidingWindowAssigner()
    assigner.add(reading("a", 500_000))
    assigner.add(reading("b", 200_000))
    assert assigner.watermark_for("a") == 495_000
    assert assigner.fleet_watermark_ms() == 195_000


def test_an_idle_channel_stops_holding_the_fleet_back():
    # ADR-019's idleness rule. Without it one silent device freezes every verdict.
    assigner = SlidingWindowAssigner()
    assigner.add(reading("a", 500_000))
    assigner.add(reading("stalled", 100_000))
    assert assigner.fleet_watermark_ms(idle_ms=60_000) == 495_000
    assert assigner.fleet_watermark_ms(idle_ms=0) == 95_000


def test_a_fleet_with_no_readings_has_no_watermark():
    assert SlidingWindowAssigner().fleet_watermark_ms() is None


# ------------------------- evidence at the moment it exists -------------------------


def window_score(channel: str, window_start_ms: int, value: float, onset_ms: int) -> DetectorScore:
    return DetectorScore(
        detector="zscore",
        channel=channel,
        window_start_ms=window_start_ms,
        window_end_ms=window_start_ms + 30_000,
        score=value,
        latency_ms=0.1,
        onset_ms=onset_ms,
    )


def test_the_builder_announces_an_episode_when_it_opens_not_when_it_closes():
    opened: list[Episode] = []
    builder = EpisodeBuilder(threshold=8.0, on_open=opened.append)
    assert builder.add(window_score("a", 0, 40.0, onset_ms=3_500)) is None
    assert [e.began_ms for e in opened] == [3_500]
    assert builder.episodes_opened == 1
    assert builder.episodes_emitted == 0


def test_a_sustained_excursion_announces_itself_once_not_once_per_window():
    opened: list[Episode] = []
    builder = EpisodeBuilder(threshold=8.0, on_open=opened.append)
    for i in range(6):
        builder.add(window_score("a", i * 10_000, 40.0, onset_ms=3_500 + i * 10_000))
    assert len(opened) == 1


def test_a_channel_still_flagging_is_evidence_for_a_channel_that_has_finished():
    """The G-7 mechanism, in miniature.

    The long channel departs at 100.0 s and keeps flagging; a second channel departs at
    101.5 s and is done almost at once. A closed-episode index sees nothing when the second
    is judged, because the first will not close for another two minutes. An index filled
    when an episode opens has the departure, which is the fact it needed.
    """
    open_index = FlaggedWindowIndex(synchrony_ms=5_000)
    builder = EpisodeBuilder(
        threshold=8.0,
        on_open=lambda e: open_index.record(e.channel, e.began_ms, e.t_end_ms),
    )
    builder.add(window_score("long", 90_000, 40.0, onset_ms=100_000))

    closed_only = FlaggedWindowIndex(synchrony_ms=5_000)
    assert closed_only.synchronous_with(101_500) == set()
    assert open_index.synchronous_with(101_500) == {"long"}


# ------------------------- the two halves, through the real spine -------------------------


class RecordingStore:
    """Stands in for Postgres. Records what was written and in what order."""

    def __init__(self) -> None:
        self.episodes: list[Episode] = []
        self.events: list[ContextEvent] = []

    def record_episode(self, episode: Episode) -> int:
        self.episodes.append(episode)
        return len(self.episodes)

    def record_context_event(self, event: ContextEvent) -> None:
        self.events.append(event)


@dataclass
class WatchfulPolicy(ConditioningPolicy):
    """A policy that remembers what the index held when each verdict was taken."""

    def __post_init__(self) -> None:
        self.witnessed: list[tuple[str, set[str]]] = []

    def decide(self, ep: Episode):
        self.witnessed.append((ep.channel, set(self.index.synchronous_with(ep.began_ms, 5_000))))
        return super().decide(ep)


CHANNELS = ("pump-00.a", "pump-00.b", "pump-00.c")
LONG, SHORT = CHANNELS[0], CHANNELS[1]


def paired_excursion_stream(seed: int = 20260907) -> list[Reading]:
    """One long excursion and one short one, departing 1.5 s apart under the same deploy.

    Built so the short channel's episode closes well over a minute before the long one's,
    which is the ordering the closed-episode index could not see through.
    """
    rng = random.Random(seed)
    readings: list[Reading] = []
    for t_ms in range(0, 400_000, 1_000):
        for channel in CHANNELS:
            value = rng.gauss(0.0, 1.0)
            departed = (channel == LONG and 100_000 <= t_ms <= 260_000) or (
                channel == SHORT and 101_500 <= t_ms <= 118_000
            )
            readings.append(reading(channel, t_ms, value + 12.0 * departed))
    return readings


def deploy_over(scope: tuple[str, ...]) -> ContextEvent:
    return ContextEvent(
        event_id="deploy-0001",
        kind=ContextKind.DEPLOY,
        t_start_ms=60_000,
        t_end_ms=300_000,
        severity=Severity.WARNING,
        detail="rollout of collector v1.4.0",
        scope=scope,
    )


def run_spine(*, verdict_buffer_ms: int):
    from detector import DetectionSpine

    policy = WatchfulPolicy(
        source=StaticContextSource([deploy_over(CHANNELS)]),
        index=FlaggedWindowIndex(synchrony_ms=5_000),
        thresholds=ConditioningThresholds(synchrony_ms=5_000),
    )
    store = RecordingStore()
    spine = DetectionSpine(
        store,
        window_ms=30_000,
        slide_ms=10_000,
        lateness_ms=5_000,
        min_points=8,
        threshold=8.0,
        merge_gap_ms=20_000,
        warmup_samples=60,
        conditioning=policy,
        verdict_buffer_ms=verdict_buffer_ms,
        explanation_worker=None,
    )
    for r in paired_excursion_stream():
        spine.consume(r)
    spine.drain()
    return store, policy, spine


@pytest.fixture(scope="module")
def buffered_run():
    return run_spine(verdict_buffer_ms=30_000)


def test_both_excursions_are_found(buffered_run):
    store, _, _ = buffered_run
    channels = {e.channel for e in store.episodes}
    assert LONG in channels
    assert SHORT in channels


def test_the_short_excursion_sees_the_long_one_at_verdict_time(buffered_run):
    """The claim the barrier and the open-episode index exist to make true.

    Before this change the index held only closed episodes, so at the moment the short
    channel was judged the long one -- still flagging, another two minutes from closing --
    was invisible, and the verdict was taken against an empty fleet.
    """
    _, policy, _ = buffered_run
    seen = dict(policy.witnessed)
    assert LONG in seen[SHORT], (
        f"the long excursion was not visible when {SHORT} was judged: {policy.witnessed}"
    )


def test_the_long_excursion_had_not_closed_when_the_short_one_was_judged(buffered_run):
    """Without this, the test above could pass for the wrong reason.

    It only demonstrates G-7 if the evidence really was unavailable to a closed-episode
    index -- that is, if the long episode was still open at that moment.
    """
    store, policy, _ = buffered_run
    judged_short_at = [i for i, (c, _) in enumerate(policy.witnessed) if c == SHORT]
    assert judged_short_at, "the short excursion was never judged"
    stored_before = {e.channel for e in store.episodes[: judged_short_at[0]]}
    assert LONG not in stored_before


def test_the_short_excursion_is_attributed_to_the_deploy(buffered_run):
    # Two in-scope channels departing 1.5 s apart is what a deploy looks like, and with the
    # evidence present the policy can finally say so.
    _, policy, _ = buffered_run
    assert policy.verdicts.get(str(Verdict.CORROBORATED), 0) >= 1


def test_the_barrier_holds_every_episode_and_ends_empty(buffered_run):
    _, _, spine = buffered_run
    assert spine.barrier is not None
    assert spine.barrier.held_total == spine.episodes_written
    assert spine.barrier.pending == 0


def test_the_evidence_report_counts_both_populations(buffered_run):
    _, policy, _ = buffered_run
    report = policy.evidence_report()
    assert "scoped decisions" in report
    assert "synchronous" in report
