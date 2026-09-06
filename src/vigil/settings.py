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
