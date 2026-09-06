"""Tests for the dashboard API.

Integration-marked: the API's whole job is shaping stored data, so testing it against a
stub database would test the stub. Uses a throwaway schema like the store tests.
"""

import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from vigil.episodes import Episode, ScoreSample
from vigil.settings import MissingSetting, PostgresSettings
from vigil.store import EpisodeStore

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    try:
        settings = PostgresSettings.from_env()
        psycopg.connect(settings.dsn, connect_timeout=3).close()
    except (MissingSetting, psycopg.Error) as exc:
        pytest.skip(f"postgres unavailable ({exc}); run `docker compose up -d`")

    schema = f"vigil_api_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(settings.dsn, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    os.environ["PGOPTIONS"] = f"-c search_path={schema}"
    try:
        store = EpisodeStore(settings)
        store.apply_schema()
        for i in range(3):
            store.record_episode(
                Episode(
                    channel=f"pump-0{i}.flow_m3_h",
                    t_start_ms=1_000 + i * 100_000,
                    t_end_ms=31_000 + i * 100_000,
                    raised_by="zscore" if i < 2 else "chronos-bolt-tiny",
                    peak_score=10.0 + i,
                    window_count=2 + i,
                    threshold=8.0,
                    scores=[
                        ScoreSample(
                            "zscore" if i < 2 else "chronos-bolt-tiny",
                            1_000 + i * 100_000,
                            31_000 + i * 100_000,
                            10.0 + i,
                            0.25 * (i + 1),
                        )
                    ],
                    injected_origins=("fault",) if i == 0 else (),
                )
            )
        store.close()

        from vigil.api.app import app

        yield TestClient(app)
    finally:
        os.environ.pop("PGOPTIONS", None)
        with psycopg.connect(settings.dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_health_reports_postgres_by_using_it_not_by_assuming_it(client):
    body = client.get("/health").json()
    assert body["postgres"] == "up"
    assert body["episodes"] == 3
    assert body["uptime_s"] >= 0


def test_episodes_are_returned_newest_first(client):
    body = client.get("/episodes").json()
    assert body["count"] == 3
    ids = [e["id"] for e in body["episodes"]]
    assert ids == sorted(ids, reverse=True)


def test_an_episode_carries_the_fields_the_dashboard_renders(client):
    e = client.get("/episodes").json()["episodes"][0]
    assert set(e) >= {
        "id",
        "channel",
        "raised_by",
        "peak_score",
        "window_count",
        "duration_s",
        "status",
        "attributed_to",
        "injected_origins",
        "created_at",
    }


def test_the_episode_limit_is_clamped_to_something_sane(client):
    assert client.get("/episodes?limit=100000").status_code == 200
    assert client.get("/episodes?limit=0").status_code == 200


def test_a_single_episode_comes_back_with_its_per_window_scores(client):
    episode_id = client.get("/episodes").json()["episodes"][-1]["id"]
    body = client.get(f"/episodes/{episode_id}").json()
    assert body["id"] == episode_id
    assert body["scores"]
    assert set(body["scores"][0]) >= {"detector", "score", "latency_ms"}


def test_an_unknown_episode_is_a_404_not_a_500(client):
    assert client.get("/episodes/99999999").status_code == 404


def test_detectors_are_listed_separately_with_their_own_latency(client):
    body = client.get("/detectors").json()
    names = {d["detector"] for d in body["detectors"]}
    assert names == {"zscore", "chronos-bolt-tiny"}
    for d in body["detectors"]:
        assert d["latency_ms"]["n"] >= 1


def test_the_hot_path_budget_is_published_so_the_ui_need_not_hardcode_it(client):
    assert client.get("/detectors").json()["hot_path_budget_ms_p99"] == 250


def test_the_context_endpoint_exists_and_is_empty_before_phase_three(client):
    body = client.get("/context").json()
    assert body["count"] == 0


def test_the_dashboard_page_renders_and_names_its_missing_panel(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Vigil" in html
    # A panel reading "drift: 0" when nothing measures drift would be worse than none, so
    # the page has to say why it is missing rather than quietly omit it.
    assert "Reconciliation and drift panel" in html


def test_the_dashboard_carries_no_emoji(client):
    # CLAUDE.md: no emoji anywhere, including generated markup.
    html = client.get("/").text
    assert not any(ord(ch) > 0x2500 and ord(ch) != 0x2014 for ch in html), (
        "non-ascii glyph outside the documented set found in the dashboard"
    )
