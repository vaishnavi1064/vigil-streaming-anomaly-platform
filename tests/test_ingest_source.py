from vigil.ingest.source import IngestGapWatch, SequenceAssigner


def test_sequences_start_at_one_and_increase_per_channel():
    s = SequenceAssigner()
    assert [s.next_for("a") for _ in range(3)] == [1, 2, 3]
    assert s.next_for("b") == 1
    assert s.next_for("a") == 4


def test_sequences_are_independent_across_channels():
    s = SequenceAssigner()
    for _ in range(10):
        s.next_for("busy")
    assert s.next_for("quiet") == 1
    assert s.channels == 2
    assert s.total_assigned() == 11


def steady(watch: IngestGapWatch, channel: str, cadence: float, n: int, t0: float = 0.0) -> float:
    """Feed n arrivals on a fixed cadence; returns the last timestamp observed."""
    t = t0
    for i in range(n):
        t = t0 + i * cadence
        watch.observe(channel, t)
    return t


def test_no_gap_is_reported_on_a_steady_cadence():
    w = IngestGapWatch()
    steady(w, "c", 0.5, 60)
    assert w.gaps == []


def test_nothing_is_flagged_before_the_cadence_is_learned():
    # A cold channel has no established rhythm, so the first long pause is not evidence of
    # an outage -- it is evidence of not knowing the cadence yet.
    w = IngestGapWatch(warmup_samples=8)
    w.observe("c", 0.0)
    w.observe("c", 30.0)
    assert w.gaps == []


def test_a_stall_longer_than_the_learned_cadence_is_flagged():
    w = IngestGapWatch(gap_factor=6.0, min_gap_s=1.0)
    t = steady(w, "c", 0.5, 40)
    gap = w.observe("c", t + 10.0)
    assert gap is not None
    assert gap.channel == "c"
    assert gap.duration_s == 10.0
    assert gap.expected_cadence_s == 0.5
    # 10s of silence on a 0.5s cadence is roughly nineteen readings that never arrived.
    assert gap.estimated_missing == 19


def test_a_fast_and_a_slow_channel_get_their_own_thresholds():
    # One global timeout cannot serve both: the solar feed publishes strings hundreds of
    # times a second and site rollups every few seconds.
    w = IngestGapWatch(gap_factor=6.0, min_gap_s=0.01)
    t_fast = steady(w, "fast", 0.002, 40)
    t_slow = steady(w, "slow", 5.0, 40)
    # 0.1s is a serious outage for the fast channel and utterly normal for the slow one.
    assert w.observe("fast", t_fast + 0.1) is not None
    assert w.observe("slow", t_slow + 0.1) is None


def test_min_gap_floor_suppresses_noise_on_very_fast_channels():
    w = IngestGapWatch(gap_factor=6.0, min_gap_s=1.0)
    t = steady(w, "c", 0.002, 40)
    # 0.05s is 25x the cadence but well under the floor, so it is scheduler jitter.
    assert w.observe("c", t + 0.05) is None


def test_an_outage_does_not_poison_the_cadence_estimate():
    # If a stall were folded into the estimate, one long outage would raise the threshold
    # enough to hide every later one.
    w = IngestGapWatch(gap_factor=6.0, min_gap_s=1.0)
    t = steady(w, "c", 0.5, 40)
    assert w.observe("c", t + 10.0) is not None
    t += 10.0
    t = steady(w, "c", 0.5, 5, t0=t + 0.5)
    assert w.observe("c", t + 10.0) is not None
    assert len(w.gaps) == 2


def test_estimated_missing_totals_across_gaps():
    w = IngestGapWatch(gap_factor=4.0, min_gap_s=0.5)
    t = steady(w, "c", 1.0, 40)
    w.observe("c", t + 10.0)
    t = steady(w, "c", 1.0, 20, t0=t + 11.0)
    w.observe("c", t + 20.0)
    assert w.estimated_missing == sum(g.estimated_missing for g in w.gaps)
    assert w.estimated_missing > 25
