"""Environment-backed configuration.

Every value is required. There are deliberately no defaults for anything that names a
host, a topic, or a credential: a typo in .env should stop the process with the name of
the offending variable, not start a service that quietly talks to the wrong place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env(dotenv_path: Path | None = None) -> None:
    """Load .env from the repo root. Real environment variables win over the file."""
    load_dotenv(dotenv_path or _REPO_ROOT / ".env", override=False)


class MissingSetting(RuntimeError):
    """Raised when a required environment variable is absent or empty."""


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingSetting(
            f"{name} is not set. Copy .env.example to .env and fill it in "
            f"(see the variable's comment there for what it means)."
        )
    return value


def required_int(name: str) -> int:
    raw = required(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise MissingSetting(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class KafkaSettings:
    bootstrap: str
    readings_topic: str
    readings_partitions: int
    context_topic: str
    scores_topic: str

    @classmethod
    def from_env(cls) -> KafkaSettings:
        load_env()
        return cls(
            bootstrap=required("KAFKA_BOOTSTRAP"),
            readings_topic=required("READINGS_TOPIC"),
            readings_partitions=required_int("READINGS_PARTITIONS"),
            context_topic=required("CONTEXT_TOPIC"),
            scores_topic=required("SCORES_TOPIC"),
        )


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_env(cls) -> PostgresSettings:
        load_env()
        return cls(
            host=required("POSTGRES_HOST"),
            port=required_int("POSTGRES_PORT"),
            user=required("POSTGRES_USER"),
            password=required("POSTGRES_PASSWORD"),
            database=required("POSTGRES_DB"),
        )

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"

    def __repr__(self) -> str:  # keep the password out of tracebacks and logs
        return (
            f"PostgresSettings(host={self.host!r}, port={self.port}, "
            f"user={self.user!r}, password=<redacted>, database={self.database!r})"
        )


@dataclass(frozen=True)
class MqttSettings:
    """The public solar-fleet feed. No credentials: the broker is open and anonymous."""

    host: str
    port: int
    topic: str

    @classmethod
    def from_env(cls) -> MqttSettings:
        load_env()
        return cls(
            host=required("MQTT_HOST"),
            port=required_int("MQTT_PORT"),
            topic=required("MQTT_TOPIC"),
        )


@dataclass(frozen=True)
class ClickHouseSettings:
    """The serving store (ADR-006, ADR-045).

    Required like Kafka and Postgres, not optional like the explainer: the dashboard's
    serving reads have nowhere else to go, so a missing variable here should stop the
    process with its name rather than start an API that returns empty panels.
    """

    host: str
    http_port: int
    user: str
    password: str
    database: str
    connect_timeout_s: int = 10

    @classmethod
    def from_env(cls) -> ClickHouseSettings:
        load_env()
        return cls(
            host=required("CLICKHOUSE_HOST"),
            http_port=required_int("CLICKHOUSE_HTTP_PORT"),
            user=required("CLICKHOUSE_USER"),
            password=required("CLICKHOUSE_PASSWORD"),
            database=required("CLICKHOUSE_DB"),
        )


@dataclass(frozen=True)
class LakeSettings:
    """The Iceberg lake: a JDBC catalog in Postgres, data files on MinIO (ADR-043)."""

    s3_endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    namespace: str
    catalog_uri: str

    @property
    def warehouse(self) -> str:
        return f"s3://{self.bucket}"

    @classmethod
    def from_env(cls) -> LakeSettings:
        load_env()
        pg = PostgresSettings.from_env()
        return cls(
            s3_endpoint=required("LAKE_S3_ENDPOINT"),
            access_key=required("MINIO_ROOT_USER"),
            secret_key=required("MINIO_ROOT_PASSWORD"),
            bucket=required("LAKE_BUCKET"),
            namespace=required("LAKE_NAMESPACE"),
            # The catalog is a set of tables in the application database. pyiceberg speaks
            # SQLAlchemy here, which wants the psycopg driver named explicitly.
            catalog_uri=(
                f"postgresql+psycopg://{pg.user}:{pg.password}@{pg.host}:{pg.port}/{pg.database}"
            ),
        )


@dataclass(frozen=True)
class VlmSettings:
    """The hosted explainer endpoint (ADR-004, B-1).

    Every field is optional, which is the opposite of every other settings class here and is
    deliberate. The rest of the platform fails loudly on a missing variable because it cannot
    do its job without one. The explainer can: with no key it reports itself unavailable,
    detection is untouched, and the episode simply carries no explanation. Making these
    required would turn a missing API key into a dead pipeline.
    """

    endpoint: str
    api_key: str
    model: str
    timeout_s: float = 20.0

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.api_key and self.model)

    @classmethod
    def from_env(cls) -> VlmSettings:
        load_env()
        return cls(
            endpoint=os.environ.get("VLM_ENDPOINT", "").strip(),
            api_key=os.environ.get("VLM_API_KEY", "").strip(),
            model=os.environ.get("VLM_MODEL", "").strip(),
            timeout_s=float(os.environ.get("VLM_TIMEOUT_S", "20").strip() or 20),
        )


@dataclass(frozen=True)
class ClaudeVisionSettings:
    """Claude as the chart reader, keyed on `ANTHROPIC_API_KEY` and nothing else.

    Optional for the same reason `VlmSettings` is: a missing key must cost an explanation,
    never a pipeline. The key is read from the environment only -- there is no constructor
    default, no file path, and no fallback to an SDK credential chain, so "is this configured"
    has one answer and a key cannot arrive from somewhere nobody documented.
    """

    api_key: str
    model: str = "claude-sonnet-5"
    timeout_s: float = 20.0
    max_tokens: int = 400

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> ClaudeVisionSettings:
        load_env()
        return cls(
            api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            model=os.environ.get("VLM_CLAUDE_MODEL", "").strip() or "claude-sonnet-5",
            timeout_s=float(os.environ.get("VLM_TIMEOUT_S", "20").strip() or 20),
            max_tokens=int(os.environ.get("VLM_MAX_TOKENS", "400").strip() or 400),
        )
