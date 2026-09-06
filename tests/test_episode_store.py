"""Integration tests against a real Postgres.

Marked `integration`: they need `docker compose up -d`. Run the fast suite with
`pytest -m "not integration"`.

These use a real database rather than a fake because every behaviour asserted here is a
property of Postgres, not of our code: the upsert conflict target, the foreign key from an
episode to the context event that explains it, the CHECK constraints. A fake would only
assert that the fake agrees with itself.
"""

import os
import uuid

import psycopg
import pytest

from vigil.context import ContextEvent, ContextKind, Severity
from vigil.episodes import Episode, EpisodeStatus, ScoreSample
from vigil.settings import MissingSetting, PostgresSettings
from vigil.store import EpisodeStore

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def settings() -> PostgresSettings:
    try:
        s = PostgresSettings.from_env()
    except MissingSetting as exc:
        pytest.skip(f"postgres not configured: {exc}")
    try:
        psycopg.connect(s.dsn, connect_timeout=3).close()
    except psycopg.Error as exc:
        pytest.skip(f"postgres not reachable ({exc}); run `docker compose up -d`")
    return s


@pytest.fixture
def store(settings):
    """A store on a throwaway schema, so tests never touch real episodes."""
    schema = f"vigil_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(settings.dsn, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    os.environ["PGOPTIONS"] = f"-c search_path={schema}"
    try:
        s = EpisodeStore(settings)
        s.apply_schema()
        yield s
        s.close()
    finally:
        os.environ.pop("PGOPTIONS", None)
        with psycopg.connect(settings.dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def make_episode(channel="c", start=1_000, end=31_000, peak=42.0, **kw) -> Episode:
    return Episode(
        channel=channel,
        t_start_ms=start,
        t_end_ms=end,
        raised_by=kw.pop("raised_by", "zscore"),
        peak_score=peak,
        window_count=kw.pop("window_count", 3),
        threshold=8.0,
        scores=kw.pop(
            "scores",
            [ScoreSample("zscore", start, end, peak, 0.42)],
        ),
        **kw,
    )


def test_the_schema_applies_cleanly_and_is_idempotent(store):
    store.apply_schema()
    store.apply_schema()
    assert store.episode_count() == 0


def test_an_episode_round_trips_with_its_fields_intact(store):
    store.record_episode(make_episode(channel="pump-00.flow_m3_h", peak=91.5))
    row = store.recent_episodes()[0]
    assert row["channel"] == "pump-00.flow_m3_h"
    assert row["peak_score"] == 91.5
    assert row["status"] == "real"
    assert row["attributed_to"] is None


def test_replaying_the_same_episode_does_not_duplicate_it(store):
    # A consumer restarted from its last committed offset re-derives the same episodes. If
    # the sink duplicated them, a reconciliation harness would later report drift that was
    # really our own bookkeeping.
    ep = make_episode()
    first = store.record_episode(ep)
    second = store.record_episode(ep)
    assert first == second
    assert store.episode_count() == 1


def test_a_replay_carrying_more_information_widens_the_episode(store):
    # The off-critical-path model can score windows the hot path already raised on, so a
    # second write must be allowed to extend rather than be discarded.
    store.record_episode(make_episode(end=31_000, peak=20.0, window_count=2))
    store.record_episode(make_episode(end=95_000, peak=77.0, window_count=9))
    row = store.recent_episodes()[0]
    assert row["t_end_ms"] == 95_000
    assert row["peak_score"] == 77.0
    assert row["window_count"] == 9


def test_a_replay_never_shrinks_an_episode(store):
    store.record_episode(make_episode(end=95_000, peak=77.0, window_count=9))
    store.record_episode(make_episode(end=31_000, peak=20.0, window_count=2))
    row = store.recent_episodes()[0]
    assert row["t_end_ms"] == 95_000
    assert row["peak_score"] == 77.0


def test_two_detectors_raising_on_the_same_span_are_separate_episodes(store):
    store.record_episode(make_episode(raised_by="zscore"))
    store.record_episode(make_episode(raised_by="chronos"))
    assert store.episode_count() == 2


def test_per_window_scores_are_stored_for_the_latency_report(store):
    scores = [
        ScoreSample("zscore", 1_000 + i * 10_000, 31_000 + i * 10_000, 20.0, 0.1 * i)
        for i in range(5)
    ]
    store.record_episode(make_episode(scores=scores))
    stats = store.detector_latency_percentiles("zscore")
    assert stats["n"] == 5
    assert stats["max"] == pytest.approx(0.4)


def test_latency_percentiles_are_none_for_a_detector_that_never_ran(store):
    store.record_episode(make_episode())
    assert store.detector_latency_percentiles("chronos") is None


def test_scores_are_upserted_rather_than_duplicated_on_replay(store):
    ep = make_episode()
    store.record_episode(ep)
    store.record_episode(ep)
    assert store.detector_latency_percentiles("zscore")["n"] == 1


def test_a_context_event_round_trips(store):
    event = ContextEvent(
        event_id="deploy-0007",
        kind=ContextKind.DEPLOY,
        t_start_ms=1_000,
        t_end_ms=61_000,
        severity=Severity.WARNING,
        detail="rollout of collector v1.2.3",
        scope=("pump-00.flow_m3_h", "pump-01.flow_m3_h"),
        perturbed_telemetry=True,
    )
    store.record_context_event(event)
    store.record_context_event(event)  # idempotent on the producer-assigned id
    with store._conn.cursor() as cur:
        cur.execute("SELECT * FROM context_events")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["scope"] == ["pump-00.flow_m3_h", "pump-01.flow_m3_h"]
    assert rows[0]["perturbed_telemetry"] is True


def test_an_episode_can_be_attributed_to_a_stored_context_event(store):
    store.record_context_event(
        ContextEvent(
            event_id="deploy-0001",
            kind=ContextKind.DEPLOY,
            t_start_ms=0,
            t_end_ms=60_000,
            severity=Severity.INFO,
            detail="canary",
        )
    )
    store.record_episode(make_episode(status=EpisodeStatus.ATTRIBUTED, attributed_to="deploy-0001"))
    row = store.recent_episodes()[0]
    assert row["status"] == "attributed"
    assert row["attributed_to"] == "deploy-0001"


def test_attribution_to_an_unknown_event_is_rejected_by_the_database(store):
    # Attribution has to point at a real event; a dangling reference would mean an episode
    # claiming an explanation that nothing backs up.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.record_episode(
            make_episode(status=EpisodeStatus.ATTRIBUTED, attributed_to="deploy-does-not-exist")
        )


def test_an_invalid_status_is_rejected_by_the_database(store):
    with pytest.raises(psycopg.errors.CheckViolation):
        with store._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO episodes (channel, t_start_ms, t_end_ms, raised_by, peak_score,"
                " mean_score, window_count, threshold, status)"
                " VALUES ('c', 0, 1, 'z', 1, 1, 1, 1, 'maybe')"
            )


def test_a_backwards_span_is_rejected_by_the_database(store):
    with pytest.raises(psycopg.errors.CheckViolation):
        with store._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO episodes (channel, t_start_ms, t_end_ms, raised_by, peak_score,"
                " mean_score, window_count, threshold)"
                " VALUES ('c', 5000, 1000, 'z', 1, 1, 1, 1)"
            )


def test_ground_truth_origins_survive_the_round_trip(store):
    store.record_episode(make_episode(injected_origins=("fault", "deploy")))
    assert store.recent_episodes()[0]["injected_origins"] == ["fault", "deploy"]


def test_deleting_an_episode_takes_its_scores_with_it(store):
    episode_id = store.record_episode(make_episode())
    with store._conn.cursor() as cur:
        cur.execute("DELETE FROM episodes WHERE id = %s", (episode_id,))
        cur.execute("SELECT count(*) AS n FROM episode_scores")
        assert cur.fetchone()["n"] == 0


def test_recent_episodes_returns_newest_first_and_honours_the_limit(store):
    for i in range(5):
        store.record_episode(make_episode(start=i * 100_000, end=i * 100_000 + 30_000))
    rows = store.recent_episodes(limit=3)
    assert len(rows) == 3
    assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)


def test_raw_readings_have_nowhere_to_go_in_this_schema(store):
    # ADR-006: Postgres holds episodes and app state; serving data goes to ClickHouse and
    # the durable lake is Iceberg. A readings table appearing here would be the regression.
    with store._conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        )
        tables = {r["table_name"] for r in cur.fetchall()}
    assert tables == {"context_events", "episodes", "episode_scores", "agent_actions"}
