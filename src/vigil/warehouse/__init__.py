"""The ClickHouse serving store.

Separate from `vigil.store` (Postgres, episodes and app state) and `vigil.lake` (Iceberg,
the durable event lake) because the three answer different questions and ADR-006 keeps them
apart deliberately. Nothing in the detection spine imports this: the serving store being
down must cost a dashboard panel, never a detection.
"""

from vigil.warehouse.clickhouse import SCHEMA_PATH, ReadingsWarehouse

__all__ = ["SCHEMA_PATH", "ReadingsWarehouse"]
