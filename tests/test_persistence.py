"""Tests for temporal persistence as a conditioning signal (ADR-053).

The second signal that can suppress with no context event behind it, and the first that
needs no second detector either. Two failure modes matter more than the rest:

- **Suppressing a short real fault.** A spike is an injected fault kind in this generator
  and it is short by construction, so the duration test is structurally biased against one
  whole population. The tests here pin the rescues that exist and the evaluation reports
  what the bias costs; neither hides it.
- **Protecting everything.** A recurrence window wide enough to be satisfied by coincidence
  turns the rescue into blanket protection and the signal into decoration. That is the v1
  lesson at a different width, so the recurrence test is pinned tight and the boundary is
  asserted rather than assumed.
"""

import pytest

from vigil.conditioning.persistence import PersistenceIndex
from vigil.conditioning.policy import (
    ConditioningPolicy,
    ConditioningThresholds,
    FlaggedWindowIndex,
    Verdict,
)
from vigil.conditioning.signals import StaticContextSource
from vigil.context import ContextEvent, ContextKind, Severity
from vigil.detectors.base import DetectorScore
from vigil.episodes import Episode, EpisodeBuilder, EpisodeStatus

FLEET = tuple(f"pump-{i:02d}.vibration_mm_s" for i in range(8))
SLIDE_MS = 10_000
WINDOW_MS = 30_000


def episode(
    channel: str = FLEET[0],
    start_ms: int = 60_000,
    windows: int = 1,
    onset_ms: int | None = None,
    threshold: float = 8.0,
):
    end = start_ms + WINDOW_MS + SLIDE_MS * (windows - 1)
    return Episode(
        channel=channel,
        t_start_ms=start_ms,
        t_end_ms=end,
        raised_by="zscore",
        peak_score=40.0,
        window_count=windows,
        threshold=threshold,
        onset_ms=start_ms if onset_ms is None else onset_ms,
    )


def window_score(channel: str, start_ms: int, score: float):
    return DetectorScore(
        detector="zscore",
        channel=channel,
        window_start_ms=start_ms,
        window_end_ms=start_ms + WINDOW_MS,
        score=score,
    )


def deploy(scope=FLEET[:4], event_id="deploy-0001"):
    return ContextEvent(
        event_id=event_id,
        kind=ContextKind.DEPLOY,
        t_start_ms=0,
        t_end_ms=200_000,
        severity=Severity.WARNING,
        detail="rollout of collector v1.2.3",
        scope=tuple(scope),
    )


def policy_with(events=(), flagged=(), window_scores=(), recorded_starts=(), **kw):
    index = FlaggedWindowIndex(window_ms=WINDOW_MS)
    for channel in flagged:
        index.record(channel, 60_000, 90_000)
    for channel, start in recorded_starts:
        index.record(channel, start, start + WINDOW_MS)
    persistence = PersistenceIndex()
    for score in window_scores:
        persistence.record(score)
    kw.setdefault("use_persistence", True)
    kw.setdefault("persistence_recurrence_ms", 30_000)
    return ConditioningPolicy(
        source=StaticContextSource(list(events)),
        index=index,
        persistence=persistence,
        thresholds=ConditioningThresholds(**kw),
    )


# ------------------------- the duration test -------------------------


def test_a_one_window_episode_that_never_came_back_is_suppressed():
    policy = policy_with()
    decision = policy.decide(episode(windows=1))
    assert decision.status is EpisodeStatus.ATTRIBUTED
    assert decision.verdict is Verdict.NO_PERSISTENCE
    assert policy.persistence_suppressed == 1


def test_an_episode_that_held_the_threshold_for_two_windows_is_kept():
    policy = policy_with()
    decision = policy.decide(episode(windows=2))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.NO_CONTEXT
    assert policy.persistence_suppressed == 0


@pytest.mark.parametrize(
    ("windows", "needed", "expected"),
    [
        (1, 2, Verdict.NO_PERSISTENCE),
        (2, 2, Verdict.NO_CONTEXT),
        (2, 3, Verdict.NO_PERSISTENCE),
        (3, 3, Verdict.NO_CONTEXT),
        (1, 1, Verdict.NO_CONTEXT),
    ],
)
def test_the_configured_window_count_is_where_the_decision_turns(windows, needed, expected):
    policy = policy_with(min_persistence_windows=needed, persistence_recurrence_ms=0)
    assert policy.decide(episode(windows=windows)).verdict is expected


def test_a_suppression_points_at_no_event_because_there_is_none_to_point_at():
    policy = policy_with()
    decision = policy.decide(episode(windows=1))
    assert decision.attributed_to is None
    assert decision.event is None


def test_the_reason_says_how_long_it_lasted():
    policy = policy_with()
    reason = policy.decide(episode(windows=1)).reason
    assert "exactly 1 window" in reason


# ------------------------- recurrence rescues a flicker -------------------------


def test_a_second_departure_on_the_same_channel_rescues_a_one_window_episode():
    policy = policy_with(recorded_starts=[(FLEET[0], 60_000), (FLEET[0], 80_000)])
    decision = policy.decide(episode(FLEET[0], start_ms=60_000, windows=1))
    assert decision.status is EpisodeStatus.REAL
    assert policy.persistence_suppressed == 0


def test_a_departure_on_a_different_channel_is_not_a_recurrence():
    # Cross-channel coincidence is what the corroboration half is for. Counting it here
    # would make two unrelated channels protect each other.
    policy = policy_with(recorded_starts=[(FLEET[0], 60_000), (FLEET[1], 70_000)])
    assert policy.decide(episode(FLEET[0], start_ms=60_000, windows=1)).verdict is (
        Verdict.NO_PERSISTENCE
    )


def test_a_departure_outside_the_recurrence_width_does_not_rescue():
    policy = policy_with(
        recorded_starts=[(FLEET[0], 60_000), (FLEET[0], 140_000)],
        persistence_recurrence_ms=30_000,
    )
    assert policy.decide(episode(FLEET[0], start_ms=60_000, windows=1)).verdict is (
        Verdict.NO_PERSISTENCE
    )


def test_the_episodes_own_entry_is_not_its_own_recurrence():
    # The index is filled when an episode opens, so the episode asking the question is
    # already in it. Counting that would rescue every flicker and make the test inert.
    policy = policy_with(recorded_starts=[(FLEET[0], 60_000)])
    assert policy.decide(episode(FLEET[0], start_ms=60_000, windows=1)).verdict is (
        Verdict.NO_PERSISTENCE
    )


def test_recurrence_can_be_switched_off_entirely():
    policy = policy_with(
        recorded_starts=[(FLEET[0], 60_000), (FLEET[0], 80_000)],
        persistence_recurrence_ms=0,
    )
    assert policy.decide(episode(FLEET[0], start_ms=60_000, windows=1)).verdict is (
        Verdict.NO_PERSISTENCE
    )


# ------------------------- the shoulder, off by default -------------------------


def test_a_shoulder_does_not_rescue_anything_at_the_default_fraction():
    policy = policy_with(
        window_scores=[window_score(FLEET[0], 30_000, 7.9), window_score(FLEET[0], 40_000, 7.5)],
        persistence_recurrence_ms=0,
    )
    assert policy.decide(episode(FLEET[0], start_ms=60_000, windows=1)).verdict is (
        Verdict.NO_PERSISTENCE
    )


def test_a_shoulder_rescues_when_the_fraction_is_turned_on():
    policy = policy_with(
        window_scores=[window_score(FLEET[0], 30_000, 7.9)],
        persistence_recurrence_ms=0,
        persistence_shoulder_fraction=0.5,
    )
    decision = policy.decide(episode(FLEET[0], start_ms=60_000, windows=1))
    # Rescued, and the episode keeps the verdict the context half reached: a rescue is the
    # absence of a suppression, not a new reason to overwrite an existing one with.
    assert decision.status is EpisodeStatus.REAL
    assert policy.persistence_outcomes == {"persistent": 1}
    assert policy.persistence_suppressed == 0


def test_a_quiet_preceding_window_is_not_a_shoulder():
    policy = policy_with(
        window_scores=[window_score(FLEET[0], 30_000, 0.4)],
        persistence_recurrence_ms=0,
        persistence_shoulder_fraction=0.5,
    )
    assert policy.decide(episode(FLEET[0], start_ms=60_000, windows=1)).verdict is (
        Verdict.NO_PERSISTENCE
    )


def test_the_shoulder_run_stops_at_the_first_quiet_window():
    index = PersistenceIndex()
    index.record(window_score(FLEET[0], 0, 7.0))
    index.record(window_score(FLEET[0], 10_000, 0.2))
    index.record(window_score(FLEET[0], 20_000, 7.0))
    assert index.shoulder_before(FLEET[0], 50_000, threshold=8.0, fraction=0.5) == 1


def test_a_window_after_the_episode_is_never_a_shoulder():
    # Backwards only, deliberately: the forward side needs windows the verdict barrier
    # does not promise for a short episode, and a test available to long episodes and not
    # to short ones would be measuring duration twice.
    index = PersistenceIndex()
    index.record(window_score(FLEET[0], 90_000, 7.9))
    assert index.shoulder_before(FLEET[0], 60_000, threshold=8.0, fraction=0.5) == 0


def test_the_shoulder_is_off_when_the_fraction_is_zero():
    index = PersistenceIndex()
    index.record(window_score(FLEET[0], 0, 100.0))
    assert index.shoulder_before(FLEET[0], 50_000, threshold=8.0, fraction=0.0) == 0


# ------------------------- what it must not do -------------------------


def test_the_context_half_still_attributes_a_flicker_it_can_explain():
    # Persistence suppresses only where nothing else already did, so a corroborated deploy
    # artifact keeps its own verdict and the reason an operator would want.
    policy = policy_with(
        events=[deploy()],
        flagged=FLEET[:3],
        require_blast_radius=False,
        persistence_recurrence_ms=0,
    )
    decision = policy.decide(episode(FLEET[0], start_ms=60_000, windows=1))
    assert decision.verdict is Verdict.CORROBORATED
    assert policy.persistence_suppressed == 0


def test_a_persistent_episode_does_not_veto_a_context_attribution_by_default():
    # The asymmetry against the second detector's veto, asserted rather than described.
    # Most episodes are persistent; letting duration refuse attributions would replace the
    # context half rather than leave it unchanged.
    policy = policy_with(events=[deploy()], flagged=FLEET[:3], require_blast_radius=False)
    decision = policy.decide(episode(FLEET[0], start_ms=60_000, windows=4))
    assert decision.status is EpisodeStatus.ATTRIBUTED
    assert decision.verdict is Verdict.CORROBORATED
    assert policy.persistence_vetoed == 0


def test_the_veto_can_be_switched_on_and_then_duration_outranks_the_deploy():
    policy = policy_with(
        events=[deploy()],
        flagged=FLEET[:3],
        require_blast_radius=False,
        persistence_protects_attributed=True,
    )
    decision = policy.decide(episode(FLEET[0], start_ms=60_000, windows=4))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.PERSISTENT
    assert policy.persistence_vetoed == 1


def test_an_episode_decided_with_no_context_at_all_is_still_raised_unconditioned():
    # ADR-007. A context outage means the episode is raised, and a one-window excursion
    # must not quietly convert that promise into a suppression.
    class Unavailable(StaticContextSource):
        def signals_for(self, window):
            lookup = super().signals_for(window)
            return type(lookup)(
                available=False, events=(), source="kafka", reason="broker unreachable"
            )

    policy = ConditioningPolicy(
        source=Unavailable([]),
        index=FlaggedWindowIndex(),
        persistence=PersistenceIndex(),
        thresholds=ConditioningThresholds(use_persistence=True, persistence_recurrence_ms=0),
    )
    decision = policy.decide(episode(windows=1))
    assert decision.status is EpisodeStatus.REAL
    assert decision.verdict is Verdict.FAIL_OPEN
    assert policy.persistence_suppressed == 0


def test_advisory_mode_records_the_same_evidence_and_changes_nothing():
    acting = policy_with(persistence_recurrence_ms=0)
    advisory = policy_with(use_persistence=False, persistence_recurrence_ms=0)
    assert acting.decide(episode(windows=1)).status is EpisodeStatus.ATTRIBUTED
    assert advisory.decide(episode(windows=1)).status is EpisodeStatus.REAL
    assert advisory.persistence_outcomes == acting.persistence_outcomes
    assert advisory.persistence_windows_seen == acting.persistence_windows_seen
    assert advisory.persistence_suppressed == 0


def test_no_index_and_no_flag_leaves_every_decision_exactly_as_it_was():
    policy = ConditioningPolicy(
        source=StaticContextSource([]),
        index=FlaggedWindowIndex(),
        thresholds=ConditioningThresholds(),
    )
    assert policy.decide(episode(windows=1)).verdict is Verdict.NO_CONTEXT
    assert policy.persistence_outcomes == {}


# ------------------------- the geometry the default rests on -------------------------


def test_an_excursion_lasting_one_slide_really_does_occupy_two_windows():
    # The default is 2 because of this, so it is asserted against the real builder rather
    # than taken on trust from a comment.
    builder = EpisodeBuilder(threshold=8.0, merge_gap_ms=2 * SLIDE_MS)
    for start in (0, SLIDE_MS):
        builder.add(window_score(FLEET[0], start, 40.0))
    builder.add(window_score(FLEET[0], 4 * SLIDE_MS, 0.1))
    closed = builder.close_all()
    assert [e.window_count for e in closed] == [2]


def test_a_single_dominating_window_produces_exactly_one():
    builder = EpisodeBuilder(threshold=8.0, merge_gap_ms=2 * SLIDE_MS)
    builder.add(window_score(FLEET[0], 0, 40.0))
    for start in (SLIDE_MS, 2 * SLIDE_MS, 3 * SLIDE_MS, 4 * SLIDE_MS):
        builder.add(window_score(FLEET[0], start, 0.2))
    assert [e.window_count for e in builder.close_all()] == [1]


def test_the_index_can_be_evicted_without_losing_the_live_span():
    index = PersistenceIndex()
    index.record(window_score(FLEET[0], 0, 9.0))
    index.record(window_score(FLEET[0], 600_000, 9.0))
    index.evict_before(500_000)
    assert index.scored_windows(FLEET[0]) == 1
