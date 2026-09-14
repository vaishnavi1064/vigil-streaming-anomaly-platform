"""The Iceberg event lake on object storage.

Separate from `vigil.warehouse` (ClickHouse, serving) and `vigil.store` (Postgres, episodes
and app state) because ADR-006 keeps the three apart deliberately: this one is the durable
record and the thing reconciliation audits against, not the thing a dashboard queries.
"""

from vigil.lake.iceberg import (
    OFFSETS_PROPERTY,
    READINGS_TABLE,
    ROWS_PROPERTY,
    ChannelIdentity,
    ReadingsLake,
)

__all__ = [
    "OFFSETS_PROPERTY",
    "READINGS_TABLE",
    "ROWS_PROPERTY",
    "ChannelIdentity",
    "ReadingsLake",
]
