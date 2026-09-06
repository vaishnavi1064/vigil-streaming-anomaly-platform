import pytest

from vigil.readings import Reading
from vigil.windows import ChannelHistory, SlidingWindowAssigner, window_starts


def r(channel: str, ts_ms: int, value: float = 1.0, injected: str | None = None) -> Reading:
    return Reading(channel=channel, seq=ts_ms, event_ts_ms=ts_ms, value=value, injected=injected)


def feed(assigner: SlidingWindowAssigner, channel: str, start_ms: int, n: int, step_ms: int):
    closed = []
    for i in range(n):
        closed.extend(assigner.add(r(channel, start_ms + i * step_ms, float(i))))
    return closed


def test_a_point_belongs_to_every_sliding_window_covering_it():
    # 30s windows advancing 10s means three overlapping views of every point.
    assert sorted(window_starts(25_000, 30_000, 10_000)) == [0, 10_000, 20_000]


def test_tumbling_windows_assign_each_point_exactly_once():
    for ts in (0, 4_999, 5_000, 12_345):
        assert len(list(window_starts(ts, 10_000, 10_000))) == 1


def test_a_point_on_a_boundary_belongs_to_the_window_it_starts():
    # Windows are half-open [start, end): the point at 10_000 starts the 10_000 window and
    # must not also land in [0, 10_000).
    assert 10_000 in window_starts(10_000, 10_000, 10_000)
    assert 0 not in window_starts(10_000, 10_000, 10_000)


def test_size_must_be_a_whole_multiple_of_slide():
    # Otherwise boundaries drift and two runs over the same data yield different windows.
    with pytest.raises(ValueError, match="whole multiple"):
        SlidingWindowAssigner(size_ms=30_000, slide_ms=7_000)


def test_a_window_closes_only_once_the_watermark_passes_its_end():
    a = SlidingWindowAssigner(
        size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=2_000, min_points=2
    )
    closed = feed(a, "c", 0, 10, 1_000)  # up to t=9_000, watermark 7_000
    assert closed == []
    # t=13_000 pushes the watermark to 11_000, which is past the end of [0, 10_000).
    closed = a.add(r("c", 13_000))
    assert [w.start_ms for w in closed] == [0]


def test_allowed_lateness_holds_a_window_open_for_stragglers():
    a = SlidingWindowAssigner(
        size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=3_000, min_points=2
    )
    feed(a, "c", 0, 10, 1_000)
    assert a.add(r("c", 11_000)) == []  # watermark 8_000, window not yet closed
    late_but_admitted = a.add(r("c", 9_500, value=99.0))
    assert late_but_admitted == []
    closed = a.add(r("c", 14_000))
    assert 99.0 in closed[0].values


def test_an_event_behind_the_watermark_is_counted_and_dropped():
    a = SlidingWindowAssigner(
        size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=1_000, min_points=2
    )
    feed(a, "c", 0, 10, 1_000)
    a.add(r("c", 20_000))  # watermark 19_000
    assert a.add(r("c", 5_000, value=-1.0)) == []
    assert a.late_readings == 1


def test_a_late_event_never_reopens_a_window_that_was_already_scored():
    # A second, different score for a window the detector has already acted on is exactly
    # the quiet inconsistency the reconciliation harness exists to catch.
    a = SlidingWindowAssigner(
        size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=1_000, min_points=2
    )
    feed(a, "c", 0, 10, 1_000)
    closed = a.add(r("c", 20_000))
    assert [w.start_ms for w in closed] == [0, 10_000] or [w.start_ms for w in closed] == [0]
    scored = {w.start_ms: w.values for w in closed}
    a.add(r("c", 1_000, value=-999.0))
    reclosed = a.close_all()
    for w in reclosed:
        if w.start_ms in scored:
            pytest.fail("a closed window was emitted a second time")
    assert a.late_readings == 1


def test_watermarks_are_per_channel_so_one_silent_device_cannot_stall_the_fleet():
    a = SlidingWindowAssigner(
        size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=1_000, min_points=2
    )
    feed(a, "quiet", 0, 5, 1_000)
    closed = feed(a, "busy", 0, 40, 1_000)
    assert [w.channel for w in closed] == ["busy"] * len(closed)
    assert closed, "the busy channel must progress despite the quiet one"
    assert a.watermark_for("quiet") == 4_000 - 1_000
    assert a.watermark_for("busy") == 39_000 - 1_000


def test_a_thin_window_is_dropped_rather_than_scored():
    # Two or three samples cannot support a statement about dispersion; scoring them would
    # manufacture confident nonsense.
    a = SlidingWindowAssigner(size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=0, min_points=8)
    for ts in (0, 1_000, 2_000):
        a.add(r("c", ts))
    a.add(r("c", 30_000))
    assert a.dropped_thin_windows >= 1
    assert a.emitted_windows == 0


def test_values_come_out_ordered_by_event_time_not_arrival():
    a = SlidingWindowAssigner(
        size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=8_000, min_points=2
    )
    for ts in (5_000, 1_000, 9_000, 3_000):
        a.add(r("c", ts, value=ts / 1000.0))
    closed = a.add(r("c", 20_000))
    window = next(w for w in closed if w.start_ms == 0)
    assert window.event_ts_ms == tuple(sorted(window.event_ts_ms))
    assert window.values == (1.0, 3.0, 5.0, 9.0)
    assert a.late_readings == 0


def test_lateness_is_measured_against_the_high_water_mark_not_arrival_order():
    # Arriving out of order is fine; arriving behind the watermark is not. With 5s of
    # allowed lateness, a jump to t=9000 puts the watermark at 4000 and a straggler at
    # t=3000 is genuinely too late -- even though only four events have been seen.
    a = SlidingWindowAssigner(
        size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=5_000, min_points=2
    )
    for ts in (5_000, 1_000, 9_000, 3_000):
        a.add(r("c", ts, value=ts / 1000.0))
    closed = a.add(r("c", 20_000))
    window = next(w for w in closed if w.start_ms == 0)
    assert window.values == (1.0, 5.0, 9.0)
    assert a.late_readings == 1


def test_close_all_flushes_what_the_watermark_never_reached():
    a = SlidingWindowAssigner(
        size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=1_000, min_points=2
    )
    feed(a, "c", 0, 8, 1_000)
    assert a.open_window_count > 0
    assert a.close_all()
    assert a.open_window_count == 0


def test_injected_labels_ride_along_with_the_window():
    a = SlidingWindowAssigner(size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=0, min_points=2)
    a.add(r("c", 1_000, injected="spike"))
    for ts in range(2_000, 9_000, 1_000):
        a.add(r("c", ts))
    closed = a.add(r("c", 25_000))
    window = next(w for w in closed if w.start_ms == 0)
    assert window.contains_injected_anomaly
    assert window.injected.count("spike") == 1


def test_a_window_with_no_injected_points_is_not_marked():
    a = SlidingWindowAssigner(size_ms=10_000, slide_ms=10_000, allowed_lateness_ms=0, min_points=2)
    feed(a, "c", 0, 9, 1_000)
    closed = a.add(r("c", 25_000))
    assert not any(w.contains_injected_anomaly for w in closed)


def test_overlapping_windows_share_the_points_they_both_cover():
    a = SlidingWindowAssigner(size_ms=30_000, slide_ms=10_000, allowed_lateness_ms=0, min_points=2)
    # Windows close as the watermark advances, so the ones emitted during the feed have to
    # be collected too -- close_all only flushes what the watermark never reached.
    closed = feed(a, "c", 0, 60, 1_000) + a.close_all()
    by_start = {w.start_ms: w for w in closed}
    shared = set(by_start[0].event_ts_ms) & set(by_start[10_000].event_ts_ms)
    assert shared, "30s windows sliding 10s must overlap by 20s"
    assert len(shared) == 20


def test_window_geometry_is_reported_honestly():
    a = SlidingWindowAssigner(size_ms=30_000, slide_ms=10_000, allowed_lateness_ms=0, min_points=2)
    feed(a, "c", 0, 60, 1_000)
    w = a.close_all()[0]
    assert w.duration_ms == 30_000
    assert w.count == len(w.values) == len(w.event_ts_ms)
    assert all(w.start_ms <= t < w.end_ms for t in w.event_ts_ms)


def test_channel_history_is_bounded_so_per_key_state_cannot_grow_without_limit():
    h = ChannelHistory(capacity=100)
    h.extend("c", tuple(float(i) for i in range(1000)))
    assert len(h.tail("c")) == 100
    assert h.tail("c")[-1] == 999.0


def test_channel_histories_are_independent():
    h = ChannelHistory(capacity=10)
    h.extend("a", (1.0, 2.0))
    h.extend("b", (9.0,))
    assert h.tail("a") == (1.0, 2.0)
    assert h.tail("b") == (9.0,)
    assert len(h) == 2
