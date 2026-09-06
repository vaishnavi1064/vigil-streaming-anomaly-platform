"""Tests for the scorer that decides whether the core claim holds.

The scorer is the referee, so it has to be incorruptible in one specific way: a policy that
suppresses everything must score badly. Several tests below simulate exactly that policy and
assert the scorer catches it. If they ever stop failing the blanket suppressor, every
false-positive number this project reports becomes meaningless.
"""

import json

import pytest

from vigil.evaluation.paired import (
    GroundTruth,
    ObservedEpisode,
    PairedComparison,
    TruthEpisode,
    TruthWindow,
    compare_fail_open,
    score_pass,
)


def fault(channel="a", start=100_000, end=160_000):
    return TruthEpisode(channel, "level_shift", "fault", start, end)


def artifact(channel="a", start=100_000, end=160_000):
    return TruthEpisode(channel, "level_shift", "deploy", start, end)


def window(event_id="deploy-1", start=90_000, end=200_000, scope=("a", "b"), perturbed=True):
    return TruthWindow(event_id, start, end, scope, perturbed)


def paged(channel="a", start=100_000, end=160_000):
    return ObservedEpisode(channel, start, end, "real", "zscore", 40.0)


def muted(channel="a", start=100_000, end=160_000, event="deploy-1"):
    return ObservedEpisode(channel, start, end, "attributed", "zscore", 40.0, attributed_to=event)


# ------------------------- ground truth -------------------------


def test_a_plan_round_trips_from_disk(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "faults": {
                    "a": [
                        {
                            "kind": "spike",
                            "origin": "fault",
                            "t_start_ms": 1000,
                            "t_end_ms": 2000,
                        }
                    ]
                },
                "deploys": [
                    {
                        "event": {
                            "event_id": "deploy-1",
                            "kind": "deploy",
                            "t_start_ms": 0,
                            "t_end_ms": 5000,
                            "severity": "warning",
                            "detail": "x",
                            "scope": ["a"],
                            "perturbed_telemetry": True,
                        },
                        "artifacts": {
                            "a": [
                                {
                                    "kind": "spike",
                                    "origin": "deploy",
                                    "t_start_ms": 100,
                                    "t_end_ms": 200,
                                }
                            ]
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    truth = GroundTruth.from_plan(plan)
    assert len(truth.faults) == 1
    assert len(truth.artifacts) == 1
    assert len(truth.windows) == 1
    assert truth.faults[0].is_real
    assert not truth.artifacts[0].is_real


def test_faults_are_partitioned_into_inside_and_outside_context_windows():
    truth = GroundTruth(
        faults=[fault("a", 100_000, 160_000), fault("c", 900_000, 960_000)],
        windows=[window(scope=("a", "b"))],
    )
    assert [f.channel for f in truth.faults_inside_windows()] == ["a"]
    assert [f.channel for f in truth.faults_outside_windows()] == ["c"]


def test_a_fault_on_an_out_of_scope_channel_counts_as_outside():
    # The window did not touch that channel, so it is not "inside" it in any useful sense.
    truth = GroundTruth(faults=[fault("z")], windows=[window(scope=("a", "b"))])
    assert truth.faults_inside_windows() == []


def test_faults_inside_quiet_windows_are_identified_separately():
    # The sharpest population: during a quiet deploy there is no artifact at all, so
    # anything suppressed there could only be blanket muting.
    truth = GroundTruth(
        faults=[fault("a"), fault("b", 400_000, 460_000)],
        windows=[
            window("noisy", 90_000, 200_000, ("a",), perturbed=True),
            window("quiet", 390_000, 500_000, ("b",), perturbed=False),
        ],
    )
    assert [f.channel for f in truth.faults_inside_quiet_windows()] == ["b"]


# ------------------------- scoring one pass -------------------------


def test_a_paged_episode_overlapping_a_fault_counts_as_detecting_it():
    truth = GroundTruth(faults=[fault("a")])
    result = score_pass("x", [paged("a")], truth)
    assert result.faults_detected == 1
    assert result.recall == 1.0


def test_an_attributed_episode_does_not_count_as_a_detection():
    # It exists in the database, but nobody was woken up, so the fault was not caught.
    truth = GroundTruth(faults=[fault("a")])
    result = score_pass("x", [muted("a")], truth)
    assert result.faults_detected == 0
    assert result.recall == 0.0
    assert result.attributed == 1


def test_an_episode_on_a_different_channel_does_not_count_as_a_detection():
    truth = GroundTruth(faults=[fault("a")])
    assert score_pass("x", [paged("zzz")], truth).faults_detected == 0


def test_an_episode_at_a_different_time_does_not_count_as_a_detection():
    truth = GroundTruth(faults=[fault("a", 100_000, 160_000)])
    assert score_pass("x", [paged("a", 900_000, 960_000)], truth).faults_detected == 0


def test_matching_allows_slack_for_window_alignment_but_not_for_being_wrong():
    # A windowed detector cannot reproduce an episode's exact boundaries, and demanding it
    # would measure window alignment rather than detection.
    truth = GroundTruth(faults=[fault("a", 100_000, 160_000)])
    assert score_pass("x", [paged("a", 165_000, 180_000)], truth, slack_ms=30_000).recall == 1.0
    assert score_pass("x", [paged("a", 400_000, 430_000)], truth, slack_ms=30_000).recall == 0.0


def test_a_page_over_an_injected_artifact_is_a_false_page():
    truth = GroundTruth(artifacts=[artifact("a")])
    result = score_pass("x", [paged("a")], truth)
    assert result.artifact_pages == 1
    assert result.false_pages == 1


def test_a_page_over_nothing_at_all_is_also_a_false_page():
    # Noise the detector invented is just as much a false page as an artifact it misread.
    result = score_pass("x", [paged("a")], GroundTruth())
    assert result.unexplained_pages == 1
    assert result.false_pages == 1


def test_a_page_over_a_real_fault_is_not_a_false_page():
    truth = GroundTruth(faults=[fault("a")])
    result = score_pass("x", [paged("a")], truth)
    assert result.false_pages == 0
    assert result.precision == 1.0


def test_counting_is_incident_level_so_one_episode_is_one_page():
    # Episodes are already merged incidents. If the scorer counted windows, a chatty
    # detector could inflate both its false-positive count and its apparent reduction.
    truth = GroundTruth(faults=[fault("a", 100_000, 400_000)])
    result = score_pass("x", [paged("a", 100_000, 400_000)], truth)
    assert result.paged == 1
    assert result.faults_detected == 1


# ------------------------- the pair, and the trap -------------------------


def build_comparison(shadow_obs, cond_obs, truth):
    return PairedComparison(
        shadow=score_pass("shadow", shadow_obs, truth),
        conditioned=score_pass("conditioned", cond_obs, truth),
    )


def test_suppressing_artifacts_only_is_the_result_we_want():
    truth = GroundTruth(
        faults=[fault("real-1", 500_000, 560_000)],
        artifacts=[artifact("art-1"), artifact("art-2")],
        windows=[window(scope=("art-1", "art-2"))],
    )
    shadow = [paged("art-1"), paged("art-2"), paged("real-1", 500_000, 560_000)]
    conditioned = [muted("art-1"), muted("art-2"), paged("real-1", 500_000, 560_000)]
    comparison = build_comparison(shadow, conditioned, truth)
    assert comparison.fp_reduction == 1.0
    assert comparison.recall_loss == 0.0
    assert comparison.meets_target


def test_a_blanket_suppressor_fails_the_pair_however_good_its_fp_reduction():
    # THE test. A policy that mutes everything inside a window posts a perfect
    # false-positive reduction and must still fail, because it lost the real fault with it.
    truth = GroundTruth(
        faults=[fault("real-1")],
        artifacts=[artifact("art-1"), artifact("art-2")],
        windows=[window(scope=("art-1", "art-2", "real-1"))],
    )
    shadow = [paged("art-1"), paged("art-2"), paged("real-1")]
    conditioned = [muted("art-1"), muted("art-2"), muted("real-1")]
    comparison = build_comparison(shadow, conditioned, truth)
    assert comparison.fp_reduction == 1.0, "the suppressor does remove every false page"
    assert comparison.recall_loss == 1.0, "and loses every real fault doing it"
    assert not comparison.meets_target


def test_recall_loss_inside_windows_is_reported_separately():
    # The column that localises the damage: a blanket suppressor loses faults inside
    # windows specifically, and an aggregate recall figure can hide that behind the
    # outside-window population.
    truth = GroundTruth(
        faults=[fault("a"), fault("far", 900_000, 960_000)],
        windows=[window(scope=("a",))],
    )
    shadow = [paged("a"), paged("far", 900_000, 960_000)]
    conditioned = [muted("a"), paged("far", 900_000, 960_000)]
    comparison = build_comparison(shadow, conditioned, truth)
    assert comparison.recall_loss == pytest.approx(0.5)
    assert comparison.recall_loss_inside == 1.0


def test_doing_nothing_at_all_also_fails_the_target():
    truth = GroundTruth(faults=[fault("real-1")], artifacts=[artifact("art-1")])
    both = [paged("art-1"), paged("real-1")]
    comparison = build_comparison(both, both, truth)
    assert comparison.fp_reduction == 0.0
    assert comparison.recall_loss == 0.0
    assert not comparison.meets_target


def test_the_target_needs_both_halves_not_either():
    truth = GroundTruth(
        faults=[fault("r1"), fault("r2", 300_000, 360_000)],
        artifacts=[
            artifact(f"a{i}", 500_000 + i * 100_000, 560_000 + i * 100_000) for i in range(10)
        ],
        windows=[window(scope=tuple(f"a{i}" for i in range(10)))],
    )
    shadow = [paged(f"a{i}", 500_000 + i * 100_000, 560_000 + i * 100_000) for i in range(10)]
    shadow += [paged("r1"), paged("r2", 300_000, 360_000)]
    # Removes 6 of 10 false pages (60%, clears the 40% bar) but drops one real fault.
    conditioned = [muted(f"a{i}", 500_000 + i * 100_000, 560_000 + i * 100_000) for i in range(6)]
    conditioned += [
        paged(f"a{i}", 500_000 + i * 100_000, 560_000 + i * 100_000) for i in range(6, 10)
    ]
    conditioned += [paged("r1"), muted("r2", 300_000, 360_000)]
    comparison = build_comparison(shadow, conditioned, truth)
    assert comparison.fp_reduction >= 0.40
    assert comparison.recall_loss > 0.05
    assert not comparison.meets_target


def test_the_table_shows_every_column_a_reviewer_would_ask_for():
    truth = GroundTruth(faults=[fault("a")], artifacts=[artifact("b")], windows=[window()])
    table = build_comparison([paged("a"), paged("b")], [paged("a"), muted("b")], truth).table()
    for header in (
        "False pages",
        "Recall, all real faults",
        "Recall, faults OUTSIDE windows",
        "Recall, faults INSIDE windows",
        "Recall, faults in QUIET windows",
        "Precision",
    ):
        assert header in table


def test_the_verdict_states_both_halves_and_whether_each_was_met():
    truth = GroundTruth(faults=[fault("a")], artifacts=[artifact("b")])
    verdict = build_comparison([paged("a"), paged("b")], [paged("a"), muted("b")], truth).verdict()
    assert "false-positive reduction" in verdict
    assert "recall loss" in verdict
    assert "NFR-8" in verdict


def test_no_false_pages_in_the_baseline_reports_zero_reduction_not_a_division_error():
    truth = GroundTruth(faults=[fault("a")])
    comparison = build_comparison([paged("a")], [paged("a")], truth)
    assert comparison.fp_reduction == 0.0


def test_a_plan_written_by_loadgen_puts_faults_and_windows_on_the_same_clock(tmp_path):
    """The regression guard for a bug that silently deleted an entire test population.

    `_write_plan` anchored fault times to wall clock but wrote deploy windows
    stream-relative. The two never overlapped, so `faults_inside_windows` read zero even
    though the scenario had scheduled thirteen -- and the population that exists to catch
    blanket suppression was invisible to the scorer while the run still reported a
    confident-looking number.
    """
    import loadgen

    from vigil.ingest.synthetic_source import SyntheticFleetSource

    source = SyntheticFleetSource(
        channels=8, rate_per_s=1000, duration_s=1800, scenario=True, seed=99
    )
    source._t0_wall_ms = 1_788_000_000_000  # as readings() would set it
    plan_path = tmp_path / "plan.json"
    loadgen._write_plan(plan_path, source)

    scheduled_inside = len(source.plan.faults_inside_deploy_windows())
    loaded = GroundTruth.from_plan(plan_path)

    assert scheduled_inside > 0, "the scenario must schedule faults inside windows to test this"
    assert len(loaded.faults_inside_windows()) == scheduled_inside

    # Both populations must sit on the wall clock, not one on each.
    anchor = source._t0_wall_ms
    assert all(w.t_start_ms >= anchor for w in loaded.windows)
    assert all(f.t_start_ms >= anchor for f in loaded.faults)


def test_a_plan_with_quiet_windows_round_trips_that_flag(tmp_path):
    import loadgen

    from vigil.ingest.synthetic_source import SyntheticFleetSource

    source = SyntheticFleetSource(
        channels=8, rate_per_s=1000, duration_s=1800, scenario=True, seed=99
    )
    source._t0_wall_ms = 1_788_000_000_000
    plan_path = tmp_path / "plan.json"
    loadgen._write_plan(plan_path, source)

    loaded = GroundTruth.from_plan(plan_path)
    assert any(not w.perturbed for w in loaded.windows), "quiet windows must survive the plan"
    assert any(w.perturbed for w in loaded.windows)


# ------------------------- fail-open (ADR-007) -------------------------
# The check exists because the failure it guards against is silent: if conditioning
# suppresses while its signal source is down, the alerting path goes quiet exactly when an
# operator has least reason to suspect it.


def test_a_signal_less_conditioned_pass_matching_the_baseline_holds_fail_open():
    shadow = [paged("a"), paged("b", 200_000, 260_000)]
    check = compare_fail_open(shadow, list(shadow))

    assert check.held
    assert check.identical == 2
    assert "HELD" in check.line()


def test_an_episode_muted_without_any_context_breaks_fail_open():
    shadow = [paged("a"), paged("b", 200_000, 260_000)]
    signal_less = [muted("a"), paged("b", 200_000, 260_000)]

    check = compare_fail_open(shadow, signal_less)

    assert not check.held
    assert check.suppressed == 1
    assert check.attributed == 1
    assert "BROKEN" in check.line()


def test_an_episode_the_signal_less_pass_never_raised_breaks_fail_open():
    """Conditioning must not change which episodes exist, only how they are judged."""
    shadow = [paged("a"), paged("b", 200_000, 260_000)]

    check = compare_fail_open(shadow, [paged("a")])

    assert not check.held
    assert check.missing_from_fail_open == 1


def test_identity_ignores_status_so_a_changed_verdict_is_not_two_differences():
    """A muted episode is one changed verdict, not a disappearance plus an appearance."""
    check = compare_fail_open([paged("a")], [muted("a")])

    assert check.missing_from_fail_open == 0
    assert check.extra_in_fail_open == 0
    assert check.identical == 0
    assert check.suppressed == 1
