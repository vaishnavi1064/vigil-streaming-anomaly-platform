"""Integration test for bounding a blast-mode run by volume.

Marked `integration`: needs `docker compose up -d`.

The scale harness drains a fixed backlog, and every sweep point has to drain the *same*
backlog or the points are not comparable. Blast mode is unpaced, so a duration cannot
express "this many readings" -- an earlier version divided a count by a rate, fell back to a
30-second run when the rate was zero, and filled 3.9 million records for a 300,000 request.
The sweep still measured something coherent, but not what the command asked for.

So the bound is asserted here against a real broker, because what is being tested is that
the producer stops after N records land, which is a property of the publisher and the broker
together rather than of the loop in isolation.
"""

import contextlib
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from vigil.settings import KafkaSettings, MissingSetting

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


@pytest.fixture(scope="module")
def kafka() -> KafkaSettings:
    try:
        settings = KafkaSettings.from_env()
    except MissingSetting as exc:
        pytest.skip(f"kafka not configured: {exc}")
    try:
        from confluent_kafka.admin import AdminClient

        admin = AdminClient({"bootstrap.servers": settings.bootstrap})
        if not admin.list_topics(timeout=5).topics:
            pytest.skip("kafka reachable but reports no topics")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"kafka not reachable ({exc}); run `docker compose up -d`")
    return settings


@pytest.fixture
def scratch_topic(kafka):
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": kafka.bootstrap})
    name = f"scratch.bounds.{uuid.uuid4().hex[:8]}"
    for future in admin.create_topics(
        [NewTopic(name, num_partitions=1, replication_factor=1)]
    ).values():
        future.result()
    yield name
    for future in admin.delete_topics([name], operation_timeout=30).values():
        with contextlib.suppress(Exception):
            future.result()


def produce(topic: str, *args: str) -> str:
    result = subprocess.run(
        [str(PYTHON), str(REPO / "loadgen.py"), "--topic", topic, *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


def delivered(output: str) -> int:
    for line in output.splitlines():
        if line.startswith("delivered "):
            return int(line.split()[1].replace(",", ""))
    raise AssertionError(f"no delivery line in output:\n{output}")


def test_blast_mode_stops_at_the_requested_volume(scratch_topic):
    output = produce(
        scratch_topic,
        "--rate",
        "0",
        "--duration",
        "0",
        "--max-readings",
        "5000",
        "--channels",
        "4",
        "--report-interval",
        "60",
    )

    assert delivered(output) == 5000


def test_the_bound_is_a_ceiling_not_a_target(scratch_topic):
    """A paced run that ends before the ceiling is not extended to reach it."""
    output = produce(
        scratch_topic,
        "--rate",
        "500",
        "--duration",
        "2",
        "--max-readings",
        "1000000",
        "--channels",
        "4",
        "--report-interval",
        "60",
    )

    count = delivered(output)
    assert 0 < count < 5000
