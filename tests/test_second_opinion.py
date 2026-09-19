"""Tests for cross-detector agreement as a conditioning signal (ADR-050).

This is the first signal in the policy that can suppress an episode with no context event
behind it, so it is also the first one that can go wrong quietly. Two failures matter more
than the rest and both have their own section here:

- **Silence read as dissent.** The corroborating detector is cold for its first 48 buckets
  on a channel, can be unavailable entirely, and drops windows when it falls behind. Any
  of those read as "found nothing" would be blanket suppression with a second opinion's
  name on it, which is exactly the v1 failure wearing a different coat.
- **Agreement not protecting.** An episode both detectors saw is the one thing this
  mechanism must never suppress, whatever else concluded otherwise.
"""

import pytest

from vigil.conditioning.policy import (
    ConditioningPolicy,
    ConditioningThresholds,
    FlaggedWindowIndex,
    Verdict,
)
from vigil.conditioning.second_opinion import SecondOpinionIndex
from vigil.conditioning.signals import StaticContextSource
from vigil.context import ContextEvent, ContextKind, Severity
from vigil.detectors.base import DetectorScore
from vigil.episodes import Episode, EpisodeStatus

FLEET = tuple(f"pump-{i:02d}.vibration_mm_s" for i in range(8))


def episode(channel: str = FLEET[0], start_ms: int = 60_000, end_ms: int = 90_000):
    return Episode(
        channel=channel,
        t_start_ms=start_ms,
        t_end_ms=end_ms,
        raised_by="zscore",
        peak_score=40.0,
        window_count=3,
        threshold=8.0,
    )


def model_score(channel: str, start_ms: int, end_ms: int, score: float):
    return DetectorScore(
        detector="chronos-bolt-tiny",
        channel=channel,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        score=score,
    )


def deploy(scope=FLEET[:4], event_id="deploy-0001"):
    return ContextEvent(
        event_id=event_id,
        kind=ContextKind.DEPLOY,
        t_start_ms=0,
        t_end_ms=120_000,
        severity=Severity.WARNING,
        detail="rollout of collector v1.2.3",
        scope=tuple(scope),
    )


def policy_with(events=(), flagged=(), model_scores=(), **kw):
    index = FlaggedWindowIndex(window_ms=30_000)
    for channel in flagged:
        index.record(channel, 60_000, 90_000)
    second = SecondOpinionIndex()
    for score in model_scores:
        second.record(score)
    kw.setdefault("use_second_opinion", True)
    return ConditioningPolicy(
        source=StaticContextSource(list(events)),
        index=index,
        second_opinion=second,
        thresholds=ConditioningThresholds(**kw),
    )


# ------------------------- silence is not dissent -------------------------


def test_a_channel_the_model_never_scored_is_not_evidence_against_the_episode():
    # The cold-start case. The model needs 48 buckets of history before it has an opinion
    # at all, and the run's first episodes land inside that window.
    policy = policy_with(model_scores=[model_score(FLEET[1], 0, 30_000, 0.4)])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.NO_CONTEXT
    assert policy.second_opinions["abstained"] == 1


def test_a_span_the_model_has_not_reached_yet_is_not_evidence_against_the_episode():
    # Scored this channel, but only up to 70 s, and the episode runs to 90 s. Concluding
    # from that is the G-7 mistake one evidence source along.
    policy = policy_with(model_scores=[model_score(FLEET[0], 40_000, 70_000, 0.5)])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert policy.second_opinions["abstained"] == 1
    assert policy.second_opinion_suppressed == 0


def test_no_index_at_all_leaves_every_decision_exactly_as_it_was():
    # The v1-v4 policy. Absence of the second detector must be absence of the signal, not
    # a third behaviour.
    policy = ConditioningPolicy(
        source=StaticContextSource([]),
        index=FlaggedWindowIndex(),
        thresholds=ConditioningThresholds(use_second_opinion=True),
    )
    decision = policy.decide(episode())
    assert decision.verdict is Verdict.NO_CONTEXT
    assert policy.second_opinions == {}


def test_a_dropped_window_in_the_middle_of_a_span_is_still_a_complete_view():
    # Progress past the span end plus one overlapping window is the coverage rule. A hole
    # inside the span lowers the evidence but does not invalidate it -- what it must not
    # do is silently become a second, undeclared abstention rule.
    policy = policy_with(
        model_scores=[
            model_score(FLEET[0], 50_000, 80_000, 0.6),
            model_score(FLEET[0], 70_000, 100_000, 0.7),
        ]
    )
    decision = policy.decide(episode(FLEET[0]))
    assert decision.verdict is Verdict.SECOND_OPINION_DISSENTS


# ------------------------- dissent, which is the point -------------------------


def test_an_episode_the_other_detector_watched_and_did_not_see_is_suppressed():
    policy = policy_with(
        model_scores=[
            model_score(FLEET[0], 60_000, 90_000, 0.8),
            model_score(FLEET[0], 70_000, 100_000, 0.5),
        ]
    )
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.ATTRIBUTED
    assert decision.verdict is Verdict.SECOND_OPINION_DISSENTS
    assert policy.second_opinion_suppressed == 1


def test_a_suppression_with_no_context_event_points_at_nothing():
    # The whole reason this signal exists: it answers a false page with no external cause,
    # so there is no event id to record and the field must stay null rather than borrow one.
    policy = policy_with(
        model_scores=[model_score(FLEET[0], 60_000, 100_000, 0.2)],
    )
    decision = policy.decide(episode(FLEET[0]))
    assert decision.verdict is Verdict.SECOND_OPINION_DISSENTS
    assert decision.attributed_to is None
    assert decision.event is None


def test_the_reason_says_what_the_other_detector_saw_and_where_the_line_was():
    policy = policy_with(
        model_scores=[model_score(FLEET[0], 60_000, 100_000, 1.4)],
        second_opinion_agrees_at=3.0,
    )
    reason = policy.decide(episode(FLEET[0])).reason
    assert "1.4" in reason
    assert "3" in reason


@pytest.mark.parametrize(
    ("peak", "expected"),
    [
        (0.5, Verdict.SECOND_OPINION_DISSENTS),
        (2.9, Verdict.SECOND_OPINION_DISSENTS),
        (3.0, Verdict.NO_CONTEXT),
        (9.0, Verdict.NO_CONTEXT),
    ],
)
def test_the_agreement_line_is_where_the_decision_turns(peak, expected):
    policy = policy_with(
        model_scores=[model_score(FLEET[0], 60_000, 100_000, peak)],
        second_opinion_agrees_at=3.0,
    )
    assert policy.decide(episode(FLEET[0])).verdict is expected


# ------------------------- agreement protects -------------------------


def test_an_episode_both_detectors_saw_is_never_suppressed_by_this_signal():
    policy = policy_with(model_scores=[model_score(FLEET[0], 60_000, 100_000, 7.5)])
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert policy.second_opinion_suppressed == 0


def test_agreement_overrides_a_context_event_that_could_have_explained_it():
    # Three in-scope channels moved together inside a deploy, which is the shape v4
    # attributes. The second detector saw the excursion too, and agreement outranks an
    # explanation.
    policy = policy_with(
        events=[deploy()],
        flagged=FLEET[:3],
        model_scores=[model_score(FLEET[0], 60_000, 100_000, 6.2)],
        require_blast_radius=False,
    )
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.SECOND_OPINION_AGREES
    assert policy.second_opinion_vetoed == 1
    assert "deploy-0001" in decision.reason


def test_the_veto_can_be_turned_off_and_then_the_context_half_decides_alone():
    policy = policy_with(
        events=[deploy()],
        flagged=FLEET[:3],
        model_scores=[model_score(FLEET[0], 60_000, 100_000, 6.2)],
        require_blast_radius=False,
        second_opinion_protects_attributed=False,
    )
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.ATTRIBUTED
    assert decision.verdict is Verdict.CORROBORATED
    assert policy.second_opinion_vetoed == 0


def test_a_veto_can_only_add_pages_never_remove_them():
    # Stated as a test because it is the argument for the veto being safe to leave on: it
    # moves episodes from attributed to real and never the other way.
    policy = policy_with(
        events=[deploy()],
        flagged=FLEET[:3],
        model_scores=[model_score(c, 60_000, 100_000, 6.2) for c in FLEET[:3]],
        require_blast_radius=False,
    )
    decisions = [policy.decide(episode(c)) for c in FLEET[:3]]
    assert all(d.status is EpisodeStatus.REAL for d in decisions)
    assert policy.attributed == 0


# ------------------------- the safety properties held elsewhere -------------------------


def test_an_episode_decided_with_no_context_at_all_is_still_raised_unconditioned():
    # ADR-007. A context outage means the episode is raised, and a second detector that
    # happened to see nothing must not quietly convert that promise into a suppression.
    class Unavailable(StaticContextSource):
        def signals_for(self, window):
            lookup = super().signals_for(window)
            return type(lookup)(
                available=False, events=(), source="kafka", reason="broker unreachable"
            )

    policy = ConditioningPolicy(
        source=Unavailable([]),
        index=FlaggedWindowIndex(),
        second_opinion=SecondOpinionIndex(),
        thresholds=ConditioningThresholds(use_second_opinion=True),
    )
    policy.second_opinion.record(model_score(FLEET[0], 60_000, 100_000, 0.1))
    decision = policy.decide(episode(FLEET[0]))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.FAIL_OPEN
    assert policy.second_opinion_suppressed == 0


def test_advisory_mode_records_the_same_opinion_and_changes_nothing():
    # The ablation. Same evidence, same timing, no effect -- so the difference between the
    # two passes is the decision and nothing else.
    scores = [model_score(FLEET[0], 60_000, 100_000, 0.3)]
    acting = policy_with(model_scores=scores)
    advisory = policy_with(model_scores=scores, use_second_opinion=False)
    assert acting.decide(episode(FLEET[0])).status is EpisodeStatus.ATTRIBUTED
    assert advisory.decide(episode(FLEET[0])).status is EpisodeStatus.REAL
    assert advisory.second_opinions == acting.second_opinions
    assert advisory.second_opinion_suppressed == 0


def test_an_episode_already_attributed_by_context_is_not_suppressed_twice():
    policy = policy_with(
        events=[deploy()],
        flagged=FLEET[:3],
        model_scores=[model_score(FLEET[0], 60_000, 100_000, 0.4)],
        require_blast_radius=False,
    )
    decision = policy.decide(episode(FLEET[0]))
    assert decision.verdict is Verdict.CORROBORATED
    assert policy.second_opinion_suppressed == 0


def test_the_decision_is_written_onto_the_episode_with_its_verdict():
    policy = policy_with(model_scores=[model_score(FLEET[0], 60_000, 100_000, 0.4)])
    ep = episode(FLEET[0])
    policy.apply(ep)
    assert ep.status is EpisodeStatus.ATTRIBUTED
    assert ep.verdict == "second_opinion_dissents"
    assert ep.attributed_to is None


# ------------------------- the index -------------------------


def test_progress_is_the_slowest_channel_because_the_question_spans_them():
    index = SecondOpinionIndex()
    index.record(model_score(FLEET[0], 0, 30_000, 1.0))
    index.record(model_score(FLEET[1], 0, 90_000, 1.0))
    assert index.progress_ms() == 30_000


def test_a_channel_the_model_has_fallen_far_behind_on_stops_holding_the_rest():
    # ADR-019's idleness rule, applied to this watermark for the same reason it applies to
    # the fleet one: one channel must not stall every verdict.
    index = SecondOpinionIndex()
    index.record(model_score(FLEET[0], 0, 30_000, 1.0))
    index.record(model_score(FLEET[1], 0, 300_000, 1.0))
    assert index.progress_ms(idle_ms=60_000) == 300_000
    assert index.progress_ms(idle_ms=0) == 30_000


def test_an_empty_index_has_no_progress_rather_than_progress_of_zero():
    assert SecondOpinionIndex().progress_ms() is None


def test_the_peak_across_overlapping_windows_is_what_agreement_is_judged_on():
    index = SecondOpinionIndex()
    index.record(model_score(FLEET[0], 60_000, 90_000, 1.0))
    index.record(model_score(FLEET[0], 70_000, 100_000, 5.0))
    opinion = index.opinion(FLEET[0], 60_000, 90_000, agrees_at=3.0)
    assert opinion.agrees
    assert opinion.peak_score == 5.0
    assert opinion.windows == 2


def test_a_window_outside_the_span_is_not_the_models_opinion_of_it():
    index = SecondOpinionIndex()
    index.record(model_score(FLEET[0], 0, 30_000, 9.0))
    index.record(model_score(FLEET[0], 60_000, 95_000, 0.2))
    opinion = index.opinion(FLEET[0], 60_000, 90_000, agrees_at=3.0)
    assert opinion.dissents
    assert opinion.windows == 1


def test_eviction_bounds_the_index_without_losing_the_live_span():
    index = SecondOpinionIndex()
    index.record(model_score(FLEET[0], 0, 30_000, 1.0))
    index.record(model_score(FLEET[0], 600_000, 630_000, 1.0))
    index.evict_before(500_000)
    assert index.opinion(FLEET[0], 0, 30_000, 3.0).covered is False
    assert index.opinion(FLEET[0], 600_000, 630_000, 3.0).covered is True
