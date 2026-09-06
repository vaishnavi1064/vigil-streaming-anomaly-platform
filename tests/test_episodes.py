from vigil.detectors.base import DetectorScore
from vigil.episodes import EpisodeBuilder, EpisodeStatus


def s(channel: str, start_ms: int, score: float, detector: str = "zscore", size_ms: int = 30_000):
    return DetectorScore(
        detector=detector,
        channel=channel,
        window_start_ms=start_ms,
        window_end_ms=start_ms + size_ms,
        score=score,
        latency_ms=0.5,
    )


def test_a_single_flagged_window_opens_an_episode_but_does_not_close_it():
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    assert b.add(s("c", 0, 50.0)) is None
    assert b.open_count == 1


def test_consecutive_flagged_windows_merge_into_one_incident():
    # An operator is paged once per incident; counting each overlapping window separately
    # would let a chatty detector inflate both its FP count and any apparent reduction.
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    for i in range(6):
        b.add(s("c", i * 10_000, 50.0))
    episodes = list(b.close_all())
    assert len(episodes) == 1
    assert episodes[0].window_count == 6


def test_a_merged_episode_spans_from_its_first_window_to_its_last():
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    for i in range(4):
        b.add(s("c", i * 10_000, 20.0))
    ep = next(iter(b.close_all()))
    assert ep.t_start_ms == 0
    assert ep.t_end_ms == 3 * 10_000 + 30_000
    assert ep.duration_ms == 60_000


def test_a_quiet_gap_closes_an_episode_and_the_next_flag_starts_a_new_one():
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    b.add(s("c", 0, 50.0))
    closed = b.add(s("c", 200_000, 50.0))
    assert closed is not None
    assert closed.t_start_ms == 0
    assert b.open_count == 1


def test_a_below_threshold_window_advances_the_gap_rather_than_being_ignored():
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    b.add(s("c", 0, 50.0))
    assert b.add(s("c", 10_000, 1.0)) is None  # quiet, but not yet a long enough gap
    closed = b.add(s("c", 300_000, 0.5))
    assert closed is not None, "a long quiet stretch must close the open episode"
    assert b.open_count == 0


def test_one_calm_window_inside_an_event_does_not_split_it_in_two():
    # merge_gap defaults to two slides precisely so a momentary dip below threshold in the
    # middle of a genuine event does not become two incidents.
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    b.add(s("c", 0, 50.0))
    b.add(s("c", 10_000, 3.0))
    b.add(s("c", 20_000, 50.0))
    assert list(b.close_all())[0].window_count == 2
    assert b.episodes_emitted == 1


def test_episodes_on_different_channels_are_independent():
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    b.add(s("a", 0, 50.0))
    b.add(s("b", 0, 50.0))
    assert b.open_count == 2
    episodes = sorted(b.close_all(), key=lambda e: e.channel)
    assert [e.channel for e in episodes] == ["a", "b"]


def test_a_long_event_is_not_chopped_up_by_the_window_size():
    # A detector that cut a twenty-minute outage into forty tidy half-minute incidents
    # would be describing its own window size, not the world.
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    for i in range(120):
        b.add(s("c", i * 10_000, 30.0))
    episodes = list(b.close_all())
    assert len(episodes) == 1
    assert episodes[0].duration_ms == 119 * 10_000 + 30_000


def test_the_peak_score_and_its_detector_are_kept():
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    b.add(s("c", 0, 12.0, detector="zscore"))
    b.add(s("c", 10_000, 91.0, detector="chronos"))
    b.add(s("c", 20_000, 20.0, detector="zscore"))
    ep = next(iter(b.close_all()))
    assert ep.peak_score == 91.0
    assert ep.raised_by == "chronos"


def test_the_mean_score_averages_every_window_in_the_episode():
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    for value in (10.0, 20.0, 30.0):
        b.add(s("c", int(value) * 1_000, value))
    ep = next(iter(b.close_all()))
    assert ep.mean_score == 20.0


def test_every_window_score_is_retained_not_just_the_peak():
    # Per-window latency is what NFR-1 is reported against, so it cannot be collapsed.
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    for i in range(5):
        b.add(s("c", i * 10_000, 15.0))
    ep = next(iter(b.close_all()))
    assert len(ep.scores) == 5
    assert all(x.latency_ms == 0.5 for x in ep.scores)


def test_a_new_episode_starts_unconditioned_as_real():
    # This is the shadow baseline's default; conditioning changes it in Phase 3.
    b = EpisodeBuilder(threshold=8.0)
    b.add(s("c", 0, 50.0))
    ep = next(iter(b.close_all()))
    assert ep.status is EpisodeStatus.REAL
    assert ep.attributed_to is None


def test_ground_truth_origins_are_collected_and_deduplicated():
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    b.add(s("c", 0, 50.0), ("fault", "fault", None))
    b.add(s("c", 10_000, 50.0), (None, "deploy"))
    ep = next(iter(b.close_all()))
    assert set(ep.injected_origins) == {"fault", "deploy"}
    assert len(ep.injected_origins) == 2


def test_an_episode_over_clean_data_carries_no_origins():
    b = EpisodeBuilder(threshold=8.0)
    b.add(s("c", 0, 50.0), (None, None, None))
    assert next(iter(b.close_all())).injected_origins == ()


def test_nothing_below_threshold_ever_produces_an_episode():
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    for i in range(200):
        assert b.add(s("c", i * 10_000, 7.99)) is None
    assert list(b.close_all()) == []
    assert b.windows_flagged == 0


def test_close_all_leaves_the_builder_empty():
    b = EpisodeBuilder(threshold=8.0)
    b.add(s("a", 0, 50.0))
    b.add(s("b", 0, 50.0))
    assert len(list(b.close_all())) == 2
    assert b.open_count == 0
    assert list(b.close_all()) == []


def test_the_merge_count_reconciles_flagged_windows_against_episodes():
    # The detector reports "flagged N windows -> M episodes"; those numbers have to add up.
    b = EpisodeBuilder(threshold=8.0, merge_gap_ms=20_000)
    episodes = []
    for i in range(10):
        episodes.append(b.add(s("c", i * 10_000, 50.0)))
    for i in range(10):
        # The first flag after the gap closes the previous episode and returns it, so a
        # caller that only drained close_all() at the end would lose it.
        episodes.append(b.add(s("c", 500_000 + i * 10_000, 50.0)))
    episodes = [e for e in episodes if e is not None] + list(b.close_all())
    assert b.windows_flagged == 20
    assert b.episodes_emitted == 2
    # Every flagged window is accounted for by exactly one episode, so the summary line
    # "flagged N -> M episodes (merged N-M)" is arithmetic, not an approximation.
    assert sum(e.window_count for e in episodes) == b.windows_flagged
