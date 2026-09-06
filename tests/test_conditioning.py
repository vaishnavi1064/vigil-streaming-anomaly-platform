"""Tests for the core contribution.

The most important tests in the repo. If conditioning quietly suppresses real anomalies, the
platform is worse than having no conditioning at all -- it would be a detector that has
learned to go quiet exactly when something is changing, which is when things break.

So the suite is organised around the ways this can go wrong rather than around the code:
blanket suppression, ignoring scope, attributing to a signal that could not have caused the
effect, and failing closed when context is missing.
"""

import pytest

from vigil.conditioning.policy import (
    ConditioningPolicy,
    ConditioningThresholds,
    FlaggedWindowIndex,
    Verdict,
)
from vigil.conditioning.signals import (
    CompositeContextSource,
    SignalWindow,
    StaticContextSource,
)
from vigil.context import ContextEvent, ContextKind, Severity
from vigil.episodes import Episode, EpisodeStatus

FLEET = tuple(f"pump-{i:02d}.vibration_mm_s" for i in range(8))


def episode(channel: str, start_ms: int = 60_000, end_ms: int = 90_000, peak: float = 40.0):
    return Episode(
        channel=channel,
        t_start_ms=start_ms,
        t_end_ms=end_ms,
        raised_by="zscore",
        peak_score=peak,
        window_count=3,
        threshold=8.0,
    )


def deploy(
    event_id: str = "deploy-0001",
    start_ms: int = 0,
    end_ms: int = 120_000,
    scope: tuple[str, ...] = FLEET[:4],
    severity: Severity = Severity.WARNING,
):
    return ContextEvent(
        event_id=event_id,
        kind=ContextKind.DEPLOY,
        t_start_ms=start_ms,
        t_end_ms=end_ms,
        severity=severity,
        detail="rollout of collector v1.2.3",
        scope=scope,
    )


def pipeline(
    event_id: str = "pipeline-60000",
    start_ms: int = 60_000,
    end_ms: int = 90_000,
    missing: int = 0,
    duplicates: int = 0,
    reordered: int = 0,
    lag_ms: int = 0,
    severity: Severity = Severity.WARNING,
):
    return ContextEvent(
        event_id=event_id,
        kind=ContextKind.PIPELINE,
        t_start_ms=start_ms,
        t_end_ms=end_ms,
        severity=severity,
        detail=(
            f"readings=18000 channels=8 missing={missing} duplicates={duplicates} "
            f"reordered={reordered} max_lag_ms={lag_ms} severity={severity}"
        ),
        scope=(),
    )


def policy_with(events, flagged=(), **kw):
    index = FlaggedWindowIndex(window_ms=30_000)
    for channel in flagged:
        index.record(channel, 60_000, 90_000)
    return ConditioningPolicy(
        source=StaticContextSource(list(events)),
        index=index,
        thresholds=ConditioningThresholds(**kw),
    )


# ------------------------- the safety property -------------------------


def test_missing_context_raises_the_episode_rather_than_suppressing_it():
    # ADR-007, and the one rule in this module that is not a heuristic. A signal outage must
    # never be able to hide a real anomaly.
    policy = ConditioningPolicy(source=StaticContextSource([], available=False))
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.FAIL_OPEN
    assert policy.failed_open == 1


def test_failing_open_says_why_in_the_reason():
    policy = ConditioningPolicy(source=StaticContextSource([], available=False))
    assert "unavailable" in policy.decide(episode(FLEET[0])).reason


def test_a_composite_source_fails_open_if_any_constituent_is_down():
    # Treating a partial answer as complete would suppress on the strength of the sources
    # that happened to be up, while the one that would have exonerated it was down.
    composite = CompositeContextSource(
        sources=[
            StaticContextSource([deploy()], name="deploys"),
            StaticContextSource([], name="pipeline", available=False),
        ]
    )
    policy = ConditioningPolicy(source=composite)
    assert policy.decide(episode(FLEET[0])).verdict is Verdict.FAIL_OPEN


def test_a_composite_source_merges_when_everything_is_up():
    composite = CompositeContextSource(
        sources=[
            StaticContextSource([deploy()], name="deploys"),
            StaticContextSource([pipeline(missing=50)], name="pipeline"),
        ]
    )
    lookup = composite.signals_for(SignalWindow(FLEET[0], 60_000, 90_000))
    assert lookup.available
    assert len(lookup.events) == 2


# ------------------------- no context, no conditioning -------------------------


def test_an_episode_with_no_overlapping_context_is_raised():
    policy = policy_with([])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.NO_CONTEXT


def test_an_episode_outside_every_context_window_is_raised():
    policy = policy_with([deploy(start_ms=0, end_ms=30_000)])
    decision = policy.decide(episode(FLEET[0], start_ms=600_000, end_ms=630_000))
    assert decision.status is EpisodeStatus.REAL


# ------------------------- scope -------------------------


def test_a_deploy_cannot_explain_a_channel_it_never_touched():
    # The cheapest route from suppression to over-suppression, closed first.
    policy = policy_with([deploy(scope=FLEET[:4])], flagged=FLEET[:4])
    decision = policy.decide(episode(FLEET[6]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.OUT_OF_SCOPE


def test_the_out_of_scope_reason_names_the_channel_and_the_event():
    policy = policy_with([deploy(scope=FLEET[:2])], flagged=FLEET[:2])
    reason = policy.decide(episode(FLEET[7])).reason
    assert FLEET[7] in reason
    assert "deploy-0001" in reason


# ------------------------- corroboration: the discriminator -------------------------


def test_a_channel_moving_alone_during_a_deploy_is_still_a_real_anomaly():
    # The single most important test here. A real fault that happens during a deploy is
    # still a real fault, and this is the population the adversarial generator plants
    # specifically to catch a policy that mutes everything inside a window.
    policy = policy_with([deploy(scope=FLEET[:4])], flagged=[FLEET[0]])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.ISOLATED
    assert decision.corroborating_channels == 1


def test_channels_moving_together_during_a_deploy_are_attributed_to_it():
    policy = policy_with([deploy(scope=FLEET[:4])], flagged=FLEET[:4])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.ATTRIBUTED
    assert decision.verdict is Verdict.CORROBORATED
    assert decision.attributed_to == "deploy-0001"
    assert decision.corroborating_channels == 4


def test_a_single_channel_cannot_corroborate_itself():
    # Otherwise the policy collapses straight back into blanket suppression.
    policy = policy_with(
        [deploy(scope=FLEET[:4])], flagged=[FLEET[0]], min_corroborating_channels=2
    )
    assert policy.decide(episode(FLEET[0])).status is EpisodeStatus.REAL


def test_a_deploy_touching_one_channel_explains_nothing():
    # A scope of one offers no corroboration in either direction: no sibling's calm can
    # exonerate the channel and none's movement can implicate it. "Cannot tell" raises, by
    # the same reasoning as fail-open -- and attributing here would hand anyone a trivial
    # way to suppress everything by declaring every deploy single-channel.
    policy = policy_with([deploy(scope=(FLEET[0],))], flagged=[FLEET[0]])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.ISOLATED
    assert "nothing can corroborate" in decision.reason


def test_a_small_fraction_of_a_large_scope_is_not_corroboration():
    # Two channels out of forty moving is not what a change to all forty looks like.
    wide = tuple(f"ch-{i:02d}" for i in range(40))
    policy = policy_with(
        [deploy(scope=wide)],
        flagged=wide[:2],
        min_corroborating_channels=2,
        min_scope_fraction=0.25,
    )
    assert policy.decide(episode(wide[0])).status is EpisodeStatus.REAL


def test_a_large_fraction_of_a_large_scope_is_corroboration():
    wide = tuple(f"ch-{i:02d}" for i in range(40))
    policy = policy_with([deploy(scope=wide)], flagged=wide[:20], min_scope_fraction=0.25)
    assert policy.decide(episode(wide[0])).status is EpisodeStatus.ATTRIBUTED


def test_the_isolated_reason_explains_the_reasoning_to_an_operator():
    policy = policy_with([deploy(scope=FLEET[:4])], flagged=[FLEET[0]])
    reason = policy.decide(episode(FLEET[0])).reason
    assert "moved alone" in reason
    assert "would not single one out" in reason


# ------------------------- pipeline plausibility -------------------------


def test_a_pipeline_event_that_lost_readings_explains_an_excursion():
    policy = policy_with([pipeline(missing=250, severity=Severity.CRITICAL)])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.ATTRIBUTED
    assert decision.verdict is Verdict.PIPELINE_DISTURBED


def test_a_pipeline_event_that_duplicated_readings_explains_an_excursion():
    policy = policy_with([pipeline(duplicates=40)])
    assert policy.decide(episode(FLEET[0])).status is EpisodeStatus.ATTRIBUTED


def test_a_clean_pipeline_window_explains_nothing():
    # Attributing to a signal that could not have caused the effect is superstition. A
    # health record showing no loss and no duplication offers no mechanism.
    policy = policy_with([pipeline(missing=0, duplicates=0, lag_ms=0, severity=Severity.INFO)])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.IMPLAUSIBLE


def test_lag_alone_does_not_explain_a_value_excursion():
    # Lag delays a reading; it does not change it.
    policy = policy_with(
        [pipeline(missing=0, duplicates=0, lag_ms=90_000, severity=Severity.WARNING)]
    )
    assert policy.decide(episode(FLEET[0])).status is EpisodeStatus.REAL


def test_a_pipeline_event_ignores_scope_because_a_disturbance_is_fleet_wide():
    policy = policy_with([pipeline(missing=100)])
    for channel in FLEET:
        assert policy.decide(episode(channel)).status is EpisodeStatus.ATTRIBUTED


def test_reordering_alone_is_enough_of_a_mechanism():
    policy = policy_with([pipeline(reordered=12)])
    assert policy.decide(episode(FLEET[0])).status is EpisodeStatus.ATTRIBUTED


# ------------------------- several overlapping signals -------------------------


def test_an_episode_explained_by_any_overlapping_signal_is_attributed():
    policy = policy_with(
        [deploy(scope=FLEET[:4]), pipeline(missing=300)],
        flagged=[FLEET[0]],
    )
    decision = policy.decide(episode(FLEET[0]))
    # Isolated for the deploy, but the pipeline genuinely lost data, which explains it.
    assert decision.status is EpisodeStatus.ATTRIBUTED
    assert decision.verdict is Verdict.PIPELINE_DISTURBED


def test_an_episode_no_overlapping_signal_can_explain_stays_real():
    policy = policy_with(
        [deploy(scope=FLEET[:4]), pipeline(missing=0, severity=Severity.INFO)],
        flagged=[FLEET[0]],
    )
    assert policy.decide(episode(FLEET[0])).status is EpisodeStatus.REAL


def test_an_out_of_scope_deploy_does_not_mask_a_better_reason():
    policy = policy_with(
        [deploy(scope=(FLEET[5],)), pipeline(missing=0, severity=Severity.INFO)],
        flagged=[FLEET[0]],
    )
    assert policy.decide(episode(FLEET[0])).verdict is Verdict.IMPLAUSIBLE


# ------------------------- application and bookkeeping -------------------------


def test_applying_a_decision_writes_it_onto_the_episode():
    policy = policy_with([deploy(scope=FLEET[:4])], flagged=FLEET[:4])
    ep = episode(FLEET[0])
    policy.apply(ep)
    assert ep.status is EpisodeStatus.ATTRIBUTED
    assert ep.attributed_to == "deploy-0001"


def test_an_attributed_episode_is_recorded_not_deleted():
    # "attributed" means not paged, not gone. The episode stays queryable, which is what
    # lets an operator check the system's reasoning.
    policy = policy_with([deploy(scope=FLEET[:4])], flagged=FLEET[:4])
    ep = episode(FLEET[0])
    policy.apply(ep)
    assert ep.status is not EpisodeStatus.SUPPRESSED
    assert ep.peak_score == 40.0


def test_the_summary_reports_the_verdict_breakdown():
    policy = policy_with([deploy(scope=FLEET[:4])], flagged=FLEET[:4])
    for channel in FLEET[:4]:
        policy.decide(episode(channel))
    policy.decide(episode(FLEET[7]))
    summary = policy.summary()
    assert "decided 5" in summary
    assert "corroborated=4" in summary
    assert "out_of_scope=1" in summary


# ------------------------- the flagged-window index -------------------------


def test_the_index_reports_channels_flagged_in_an_overlapping_window():
    index = FlaggedWindowIndex(window_ms=30_000)
    index.record("a", 60_000, 90_000)
    index.record("b", 70_000, 80_000)
    assert index.flagged_in(60_000, 90_000) >= {"a", "b"}


def test_the_index_does_not_report_channels_from_a_different_time():
    index = FlaggedWindowIndex(window_ms=30_000)
    index.record("a", 0, 30_000)
    assert "a" not in index.flagged_in(600_000, 630_000)


def test_the_index_can_be_evicted_so_it_does_not_grow_without_bound():
    index = FlaggedWindowIndex(window_ms=30_000)
    for i in range(200):
        index.record("a", i * 30_000, (i + 1) * 30_000)
    assert index.total_recorded == 200
    index.evict_before(150 * 30_000)
    assert index.total_recorded == 50


def test_evicting_every_start_for_a_channel_drops_the_channel():
    index = FlaggedWindowIndex(window_ms=30_000)
    index.record("a", 1_000, 31_000)
    index.evict_before(500_000)
    assert len(index) == 0


@pytest.mark.parametrize("channels", [1, 2, 4, 8])
def test_corroboration_scales_with_how_many_siblings_moved(channels):
    policy = policy_with([deploy(scope=FLEET)], flagged=FLEET[:channels])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.corroborating_channels == channels
    expected = EpisodeStatus.ATTRIBUTED if channels >= 2 else EpisodeStatus.REAL
    assert decision.status is expected


def test_an_attribution_carries_the_event_not_just_its_id():
    # An episode's attributed_to is a foreign key into context_events, so whoever persists
    # the episode must be able to persist the event it points at. Returning only the id left
    # the caller holding a reference it could not satisfy, and the database refused the
    # write -- which is how this was found.
    policy = policy_with([deploy(scope=FLEET[:4])], flagged=FLEET[:4])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.attributed_to == "deploy-0001"
    assert decision.event is not None
    assert decision.event.event_id == "deploy-0001"


def test_a_raised_episode_carries_no_event_to_persist():
    policy = policy_with([])
    assert policy.decide(episode(FLEET[0])).event is None


# ------------------------- synchrony: the v1 failure -------------------------


def test_channels_flagged_in_the_same_window_but_not_together_do_not_corroborate():
    """The v1 failure, pinned.

    Asking only "were this channel's in-scope siblings flagged in this window" returned
    `isolated` zero times out of 79 episodes on a real run, and suppressed every real fault
    inside a quiet deploy window: at realistic anomaly density two in-scope channels are
    flagged in almost any 30-second bucket by coincidence.
    """
    index = FlaggedWindowIndex(window_ms=30_000, synchrony_ms=5_000)
    index.record(FLEET[0], 60_000, 90_000)
    index.record(FLEET[1], 82_000, 112_000)  # same window, 22s apart
    policy = ConditioningPolicy(
        source=StaticContextSource([deploy(scope=FLEET[:4])]),
        index=index,
        thresholds=ConditioningThresholds(synchrony_ms=5_000),
    )
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.ISOLATED


def test_channels_moving_within_the_synchrony_window_do_corroborate():
    # A collector restart blips the channels it serves at the same instant.
    index = FlaggedWindowIndex(window_ms=30_000, synchrony_ms=5_000)
    for channel in FLEET[:4]:
        index.record(channel, 60_000, 90_000)
    index.record(FLEET[1], 61_500, 91_500)
    policy = ConditioningPolicy(
        source=StaticContextSource([deploy(scope=FLEET[:4])]),
        index=index,
        thresholds=ConditioningThresholds(synchrony_ms=5_000),
    )
    assert policy.decide(episode(FLEET[0])).status is EpisodeStatus.ATTRIBUTED


def test_the_synchrony_tolerance_is_what_decides_the_boundary():
    index = FlaggedWindowIndex(window_ms=30_000)
    index.record(FLEET[0], 60_000, 90_000)
    index.record(FLEET[1], 68_000, 98_000)  # 8s apart

    def decide(synchrony_ms):
        return ConditioningPolicy(
            source=StaticContextSource([deploy(scope=FLEET[:4])]),
            index=index,
            thresholds=ConditioningThresholds(synchrony_ms=synchrony_ms),
        ).decide(episode(FLEET[0]))

    assert decide(5_000).verdict is Verdict.ISOLATED
    assert decide(10_000).verdict is Verdict.CORROBORATED


def test_the_index_still_answers_the_loose_question_for_comparison():
    # Kept so strict and loose can be measured against each other rather than one silently
    # replacing the other.
    index = FlaggedWindowIndex(window_ms=30_000, synchrony_ms=1_000)
    index.record(FLEET[0], 60_000, 90_000)
    index.record(FLEET[1], 85_000, 115_000)
    assert index.flagged_in(60_000, 90_000) == {FLEET[0], FLEET[1]}
    assert index.synchronous_with(60_000) == {FLEET[0]}
