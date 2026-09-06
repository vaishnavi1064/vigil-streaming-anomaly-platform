"""The ingestion boundary: sources of readings, and the path that publishes them to Kafka.

Two sources implement one interface. The live solar fleet feed is the showcase; the
synthetic generator is the harness -- it is what makes throughput, chaos and correctness
tests reproducible, which a public feed with no SLA can never be.
"""

from vigil.ingest.publisher import ReadingPublisher
from vigil.ingest.source import IngestGap, IngestGapWatch, ReadingSource, SequenceAssigner

__all__ = [
    "IngestGap",
    "IngestGapWatch",
    "ReadingPublisher",
    "ReadingSource",
    "SequenceAssigner",
]
