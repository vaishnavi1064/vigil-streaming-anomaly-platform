"""Tests for the identity invariants the zero-drift claim rests on.

These matter more than most: the reconciliation harness is load-bearing for the core
contribution, since the pipeline-health signal it emits is what conditioning consumes. A
harness that quietly under-reports loss would make both the correctness claim and the
false-positive claim worthless at the same time.
"""

from vigil.readings import Reading
from vigil.reconciliation.ledger import (
    ChannelLedger,
    HealthSeverity,
    HealthThresholds,
    ReconciliationLedger,
)


def r(channel: str, seq: int, ts_ms: int = 0) -> Reading:
    return Reading(channel=channel, seq=seq, event_ts_ms=ts_ms, value=1.0)


# --------------------------- per-channel identity ---------------------------


def test_a_dense_sequence_has_zero_drift():
    led = ChannelLedger("c")
    for seq in range(1, 1001):
        led.observe(seq)
    assert led.readings == 1000
    assert led.span == 1000
    assert led.drift == 0
    assert led.healthy


def test_a_gap_is_counted_as_exactly_the_readings_that_never_arrived():
    led = ChannelLedger("c")
    for seq in list(range(1, 11)) + list(range(21, 31)):
        led.observe(seq)
    assert led.missing == 10
    assert led.drift == 10
    assert not led.healthy


def test_several_gaps_accumulate():
    led = ChannelLedger("c")
    for seq in [1, 2, 3, 10, 11, 20]:
        led.observe(seq)
    assert led.missing == 6 + 8
    assert led.drift == led.missing


def test_a_redelivered_reading_is_counted_as_a_duplicate_not_a_new_one():
    # Counting it as new is how a count-based check reports a healthy pipeline while the
    # data has silently doubled.
    led = ChannelLedger("c")
    for seq in [1, 2, 3, 3, 4]:
        led.observe(seq)
    assert led.duplicates == 1
    assert led.readings == 5
    assert led.span == 4
    assert led.drift == -1
    assert not led.healthy


def test_an_out_of_order_reading_is_a_finding_not_something_to_absorb():
    # Readings are keyed by channel, so a channel occupies one partition and arrives in
    # order. Reordering therefore means something upstream is wrong.
    led = ChannelLedger("c")
    for seq in [1, 2, 3, 4, 5]:
        led.observe(seq)
    led.observe(2)
    assert led.regressions == 1
    assert led.duplicates == 0
    assert not led.healthy


def test_the_drift_identity_holds_for_a_mixed_failure():
    led = ChannelLedger("c")
    for seq in [1, 2, 5, 6, 6, 7]:
        led.observe(seq)
    assert led.span == 7
    assert led.readings == 6
    assert led.drift == 1
    assert led.missing == 2
    assert led.duplicates == 1


def test_a_channel_that_never_reported_has_no_span():
    assert ChannelLedger("c").span == 0
    assert ChannelLedger("c").drift == 0


def test_a_single_reading_is_a_healthy_channel():
    led = ChannelLedger("c")
    led.observe(42)
    assert led.drift == 0
    assert led.healthy


# --------------------------- windowed health signal ---------------------------


def test_a_clean_run_reports_zero_drift():
    led = ReconciliationLedger(window_ms=10_000)
    for i in range(1, 501):
        led.observe(r("a", i, ts_ms=i * 100), ingest_wall_ms=i * 100)
    assert led.total_drift == 0
    assert led.unhealthy_channels == []
    assert "ZERO DRIFT" in led.report()


def test_loss_shows_up_in_the_report():
    led = ReconciliationLedger(window_ms=10_000)
    for i in list(range(1, 51)) + list(range(101, 151)):
        led.observe(r("a", i, ts_ms=i * 100))
    assert led.total_drift == 50
    assert "DRIFT DETECTED" in led.report()


def test_channels_are_reconciled_independently():
    led = ReconciliationLedger(window_ms=10_000)
    for i in range(1, 101):
        led.observe(r("clean", i, ts_ms=i * 100))
    for i in [1, 2, 3, 50]:
        led.observe(r("lossy", i, ts_ms=i * 100))
    assert len(led.channels) == 2
    assert led.channels["clean"].healthy
    assert not led.channels["lossy"].healthy
    assert [c.channel for c in led.unhealthy_channels] == ["lossy"]


def test_a_clean_window_still_emits_a_signal():
    # A consumer must be able to tell "the window was clean" from "no signal arrived":
    # conditioning fails open on a missing signal (ADR-007), so the two must not look alike.
    led = ReconciliationLedger(window_ms=10_000)
    for i in range(1, 301):
        led.observe(r("a", i, ts_ms=i * 100), ingest_wall_ms=i * 100)
    health = led.close_due(grace_windows=0)
    assert health
    assert all(h.severity is HealthSeverity.OK for h in health)
    assert all(not h.disturbed for h in health)


def test_a_window_containing_loss_is_marked_disturbed():
    led = ReconciliationLedger(window_ms=10_000)
    for i in list(range(1, 20)) + list(range(60, 120)):
        led.observe(r("a", i, ts_ms=i * 100), ingest_wall_ms=i * 100)
    disturbed = [h for h in led.close_all() if h.disturbed]
    assert disturbed
    assert sum(h.missing for h in disturbed) == 40  # seq 20..59 never arrived


def test_health_windows_align_to_the_detection_grid():
    # If the two used different grids, "does this disturbance overlap that anomaly" would
    # become an approximation, and the core mechanism rests on it.
    led = ReconciliationLedger(window_ms=30_000)
    led.observe(r("a", 1, ts_ms=45_000))
    led.observe(r("a", 2, ts_ms=95_000))
    health = led.close_all()
    assert [h.window_start_ms for h in health] == [30_000, 90_000]
    assert all(h.window_end_ms - h.window_start_ms == 30_000 for h in health)


def test_a_window_is_held_open_while_the_stream_is_still_inside_it():
    # Otherwise readings still arriving would be scored as missing because the harness was
    # impatient rather than because anything was lost.
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("a", 1, ts_ms=0))
    led.observe(r("a", 2, ts_ms=5_000))
    assert led.close_due(grace_windows=1) == []
    led.observe(r("a", 3, ts_ms=25_000))
    assert [h.window_start_ms for h in led.close_due(grace_windows=1)] == [0]


def test_close_all_flushes_everything_still_open():
    led = ReconciliationLedger(window_ms=10_000)
    for i in range(1, 100):
        led.observe(r("a", i, ts_ms=i * 100))
    assert led.close_all()
    assert led.close_all() == []


def test_lag_is_the_gap_between_event_time_and_when_we_saw_it():
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("a", 1, ts_ms=1_000), ingest_wall_ms=1_500)
    led.observe(r("a", 2, ts_ms=2_000), ingest_wall_ms=42_000)
    health = led.close_all()[0]
    assert health.max_lag_ms == 40_000


def test_lag_and_loss_are_graded_separately():
    # They mean different things: lag says the pipeline is behind, loss says it dropped
    # something. A conditioned detector should treat them differently.
    t = HealthThresholds()
    assert t.grade(missing=0, duplicates=0, regressions=0, max_lag_ms=10_000) is HealthSeverity.INFO
    assert (
        t.grade(missing=1, duplicates=0, regressions=0, max_lag_ms=0) is HealthSeverity.WARNING
    )
    assert (
        t.grade(missing=500, duplicates=0, regressions=0, max_lag_ms=0) is HealthSeverity.CRITICAL
    )
    assert (
        t.grade(missing=0, duplicates=0, regressions=0, max_lag_ms=200_000)
        is HealthSeverity.CRITICAL
    )


def test_any_loss_at_all_counts_as_a_disturbance():
    # The premise is that the sequence is dense, so a single missing reading is a real
    # departure, not noise to be tolerated.
    t = HealthThresholds()
    assert t.grade(1, 0, 0, 0) is not HealthSeverity.OK


def test_a_perfectly_quiet_window_grades_ok():
    assert HealthThresholds().grade(0, 0, 0, 0) is HealthSeverity.OK


def test_reordering_alone_is_enough_to_mark_a_window():
    assert HealthThresholds().grade(0, 0, 1, 0) is HealthSeverity.WARNING


def test_window_drift_nets_missing_against_duplicates():
    led = ReconciliationLedger(window_ms=100_000)
    for seq in [1, 2, 5, 5, 6]:
        led.observe(r("a", seq, ts_ms=1_000))
    health = led.close_all()[0]
    assert health.missing == 2
    assert health.duplicates == 1
    assert health.drift == 1


def test_state_stays_bounded_per_channel_regardless_of_stream_length():
    # Tracking which sequence numbers were seen would cost memory proportional to the
    # stream; the whole design is O(1) per channel.
    led = ReconciliationLedger(window_ms=10_000)
    for i in range(1, 50_001):
        led.observe(r("a", i, ts_ms=i * 10))
        led.close_due()
    assert len(led.channels) == 1
    assert led.total_drift == 0
    assert len(led._windows) <= 3


def test_the_signal_summary_names_every_field_an_operator_needs():
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("a", 1, ts_ms=0), ingest_wall_ms=0)
    summary = led.close_all()[0].summary()
    for field in ("readings", "missing", "dupes", "reordered", "lag"):
        assert field in summary


# --------------------------- late windows and lag grading ---------------------------


def test_a_window_that_already_closed_is_never_reopened():
    # Replaying a topic consumes partitions at wildly different event-time offsets, so
    # readings for an already-closed window keep arriving. Re-opening produced hundreds of
    # thousands of one-reading phantom health windows on a seven-minute run.
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("a", 1, ts_ms=1_000))
    led.observe(r("a", 2, ts_ms=95_000))
    assert led.close_due(grace_windows=1)
    before = len(led._windows)
    led.observe(r("b", 1, ts_ms=1_500))
    assert len(led._windows) == before
    assert led.late_readings == 1


def test_a_late_reading_still_counts_toward_the_identity_invariant():
    # Only the per-window attribution is lost. Dropping it from the ledger too would make
    # the drift figure wrong, which is the one number that must not be.
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("a", 1, ts_ms=1_000))
    led.observe(r("a", 2, ts_ms=95_000))
    led.close_due(grace_windows=1)
    led.observe(r("a", 3, ts_ms=1_500))
    assert led.late_readings == 1
    assert led.total_readings == 3
    assert led.channels["a"].readings == 3
    assert led.total_drift == 0


def test_lateness_is_reported_rather_than_swallowed():
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("a", 1, ts_ms=1_000))
    led.observe(r("a", 2, ts_ms=95_000))
    led.close_due(grace_windows=1)
    led.observe(r("a", 3, ts_ms=1_500))
    assert "late" in led.report()


def test_close_all_also_seals_the_windows_it_flushed():
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("a", 1, ts_ms=1_000))
    led.close_all()
    led.observe(r("a", 2, ts_ms=1_200))
    assert led.late_readings == 1
    assert led.close_all() == []


def test_lag_grading_can_be_switched_off_for_a_replay():
    # Replaying an hour-old topic honestly reports an hour of lag. That is a fact about the
    # data's age, not a live disturbance, and grading on it would mark every window of every
    # replay critical and suppress everything downstream.
    live = HealthThresholds(grade_lag=True)
    replay = HealthThresholds(grade_lag=False)
    assert live.grade(0, 0, 0, 3_600_000) is HealthSeverity.CRITICAL
    assert replay.grade(0, 0, 0, 3_600_000) is HealthSeverity.OK


def test_switching_lag_grading_off_does_not_hide_actual_loss():
    replay = HealthThresholds(grade_lag=False)
    assert replay.grade(5, 0, 0, 3_600_000) is HealthSeverity.WARNING
    assert replay.grade(500, 0, 0, 0) is HealthSeverity.CRITICAL


def test_lag_is_still_recorded_even_when_it_is_not_graded():
    # The measurement stays honest; only the severity judgement changes.
    led = ReconciliationLedger(
        window_ms=10_000, thresholds=HealthThresholds(grade_lag=False)
    )
    led.observe(r("a", 1, ts_ms=1_000), ingest_wall_ms=3_601_000)
    health = led.close_all()[0]
    assert health.max_lag_ms == 3_600_000
    assert health.severity is HealthSeverity.OK


# --------------------------- watermarks across sources ---------------------------


def test_the_watermark_is_the_slowest_source_not_the_fastest():
    # Taking the maximum is the classic mistake: during a replay Kafka hands partitions over
    # in bursts, so one races minutes ahead and closes windows the others have not reached.
    # Measured on a 7-minute replay, the max-based version stranded 74% of readings as late.
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("a", 1, ts_ms=500_000), ingest_wall_ms=1_000, source="p0")
    led.observe(r("b", 1, ts_ms=20_000), ingest_wall_ms=1_000, source="p1")
    assert led.watermark_ms() == 20_000


def test_a_fast_source_cannot_close_a_window_a_slow_one_has_not_reached():
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("fast", 1, ts_ms=1_000), ingest_wall_ms=1_000, source="p0")
    led.observe(r("slow", 1, ts_ms=1_000), ingest_wall_ms=1_000, source="p1")
    for i in range(2, 40):
        led.observe(r("fast", i, ts_ms=i * 10_000), ingest_wall_ms=1_000, source="p0")
    assert led.close_due(grace_windows=1) == []
    assert led.late_readings == 0


def test_windows_close_once_every_source_has_passed_them():
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("a", 1, ts_ms=1_000), ingest_wall_ms=1_000, source="p0")
    led.observe(r("b", 1, ts_ms=1_000), ingest_wall_ms=1_000, source="p1")
    led.observe(r("a", 2, ts_ms=90_000), ingest_wall_ms=1_000, source="p0")
    assert led.close_due(grace_windows=1) == []
    led.observe(r("b", 2, ts_ms=90_000), ingest_wall_ms=1_000, source="p1")
    assert [h.window_start_ms for h in led.close_due(grace_windows=1)] == [0]


def test_an_idle_source_stops_holding_the_watermark_back():
    # Kafka partitions go quiet routinely. Without an idleness timeout one silent partition
    # would stall every window forever -- the problem Flink solves the same way.
    led = ReconciliationLedger(window_ms=10_000, source_idle_timeout_s=5.0)
    led.observe(r("quiet", 1, ts_ms=1_000), ingest_wall_ms=1_000, source="p0")
    led.observe(r("busy", 1, ts_ms=1_000), ingest_wall_ms=1_000, source="p1")
    # p1 keeps going for another minute of wall time; p0 says nothing more.
    for i in range(2, 20):
        led.observe(r("busy", i, ts_ms=i * 10_000), ingest_wall_ms=1_000 + i * 5_000, source="p1")
    assert led.watermark_ms() == 190_000
    assert led.close_due(grace_windows=1)


def test_every_source_going_idle_at_once_still_yields_a_watermark():
    # Otherwise a completely quiet stream would deadlock instead of simply having nothing new.
    led = ReconciliationLedger(window_ms=10_000, source_idle_timeout_s=0.0)
    led.observe(r("a", 1, ts_ms=50_000), ingest_wall_ms=1_000, source="p0")
    led.observe(r("b", 1, ts_ms=20_000), ingest_wall_ms=1_000, source="p1")
    assert led.watermark_ms() == 20_000


def test_sources_are_counted():
    led = ReconciliationLedger(window_ms=10_000)
    for p in range(6):
        led.observe(r(f"c{p}", 1, ts_ms=1_000), ingest_wall_ms=1_000, source=f"p{p}")
    assert led.sources_tracked == 6


def test_without_a_named_source_it_falls_back_to_the_highest_window():
    # Unit tests and the offline benchmark drive the ledger with no partition concept.
    led = ReconciliationLedger(window_ms=10_000)
    led.observe(r("a", 1, ts_ms=95_000))
    assert led.watermark_ms() == 90_000


def test_a_registered_but_silent_source_blocks_the_watermark_entirely():
    # A partition Kafka has not got round to serving has made no claim about how far event
    # time has advanced. Assuming one would invent the information the watermark exists to
    # establish -- and treating it as absent stranded 51% of readings on a replay.
    led = ReconciliationLedger(window_ms=10_000)
    led.register_source("p0", wall_s=0.0)
    led.register_source("p1", wall_s=0.0)
    led.observe(r("a", 1, ts_ms=90_000), ingest_wall_ms=0, source="p0")
    assert led.watermark_ms() is None
    assert led.close_due(grace_windows=1) == []
    led.observe(r("b", 1, ts_ms=40_000), ingest_wall_ms=0, source="p1")
    assert led.watermark_ms() == 40_000


def test_a_registered_source_that_stays_silent_eventually_stops_blocking():
    # Otherwise an empty partition would stall the harness forever.
    led = ReconciliationLedger(window_ms=10_000, source_idle_timeout_s=5.0)
    led.register_source("p0", wall_s=0.0)
    led.register_source("empty", wall_s=0.0)
    for i in range(1, 20):
        led.observe(r("a", i, ts_ms=i * 10_000), ingest_wall_ms=i * 10_000, source="p0")
    assert led.watermark_ms() == 190_000


def test_a_revoked_partition_stops_holding_the_watermark():
    led = ReconciliationLedger(window_ms=10_000)
    led.register_source("p0", wall_s=0.0)
    led.register_source("p1", wall_s=0.0)
    led.observe(r("a", 1, ts_ms=90_000), ingest_wall_ms=0, source="p0")
    assert led.watermark_ms() is None
    led.forget_source("p1")
    assert led.watermark_ms() == 90_000


def test_registering_the_same_source_twice_does_not_reset_its_progress():
    led = ReconciliationLedger(window_ms=10_000)
    led.register_source("p0", wall_s=0.0)
    led.observe(r("a", 1, ts_ms=90_000), ingest_wall_ms=0, source="p0")
    led.register_source("p0", wall_s=0.0)
    assert led.watermark_ms() == 90_000
