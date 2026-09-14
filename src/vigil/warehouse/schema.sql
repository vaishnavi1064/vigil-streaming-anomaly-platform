-- ClickHouse holds the high-volume serving data: raw readings and per-window detector
-- scores. It does not hold episodes -- those stay in Postgres, which is the transactional
-- source of truth for them (ADR-006, ADR-045). One writer per table, no mirrors, so there
-- is no copy that can silently disagree with another.
--
-- Idempotent: safe to run on every startup.

-- Raw telemetry. ReplacingMergeTree keyed on the identity the whole platform already uses
-- (channel, seq), so a replayed Kafka batch collapses to one row per reading instead of
-- inflating every count computed here. The dedupe is eventual -- it happens on merge -- so
-- any query that must be exact says FINAL. `readings_exact` below is that view.
CREATE TABLE IF NOT EXISTS readings
(
    channel     LowCardinality(String),
    seq         UInt64,
    event_ts    DateTime64(3, 'UTC'),
    value       Float64,
    -- Ground truth from the synthetic source; empty string on live data. Stored because
    -- the evaluation harness reads it, never because a detector does.
    injected    LowCardinality(String) DEFAULT '',
    origin      LowCardinality(String) DEFAULT '',
    -- When the sink wrote it, which is what ReplacingMergeTree keeps on collision: the
    -- later write wins, so a corrected replay supersedes what it replaced.
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMMDD(event_ts)
ORDER BY (channel, seq)
SETTINGS index_granularity = 8192;

-- Event-time lookups scan by channel and time, but the primary key is (channel, seq).
-- seq is monotonic in event time within a channel, so partition pruning plus this index
-- is what keeps a "last 10 minutes on channel X" query from reading the whole part.
ALTER TABLE readings ADD INDEX IF NOT EXISTS readings_event_ts_idx event_ts TYPE minmax GRANULARITY 4;

-- Per-window detector scores, one row per (channel, detector, window). Same dedupe reason
-- as readings: the scores topic is consumed at least once.
CREATE TABLE IF NOT EXISTS window_scores
(
    channel         LowCardinality(String),
    detector        LowCardinality(String),
    window_start_ms UInt64,
    window_end_ms   UInt64,
    window_start    DateTime64(3, 'UTC'),
    score           Float64,
    -- Nullable, and in practice always null on this topic: the Flink job that produces it
    -- does not report per-window scoring latency. A fabricated 0.0 would be indistinguishable
    -- from a genuinely fast window and would corrupt the NFR-1 percentiles, which are
    -- reported from the in-process detector's own timings in Postgres instead.
    latency_ms      Nullable(Float64),
    ingested_at     DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMMDD(window_start)
ORDER BY (channel, detector, window_start_ms)
SETTINGS index_granularity = 8192;

-- Per-channel per-minute rollup, maintained incrementally as readings land. This is the
-- reason ClickHouse is here at all: the dashboard's time-series panel reads a few thousand
-- pre-aggregated rows instead of scanning millions of raw ones.
--
-- AggregatingMergeTree with *State/*Merge rather than plain sums: a materialized view fires
-- once per inserted block, so partial aggregates have to be mergeable across blocks or the
-- rollup silently reports only the last block it saw.
--
-- The count is uniqExact over seq, not count(). A materialized view runs on the rows being
-- inserted and never sees the ReplacingMergeTree dedupe that happens later at merge time,
-- so count() here reports rows written and double-counts a replayed batch -- measured: a
-- 100-reading batch replayed once made this rollup say 200. Counting distinct seq is
-- immune to that, and min/max are too because duplicating a value cannot change either.
--
-- value_avg is the one aggregate that stays vulnerable. Sum and count both inflate on a
-- replay, so the mean is exact when a partition was written once or replayed whole, and
-- biased when a replay covered only part of it. Anything that cannot tolerate that reads
-- readings_exact instead and pays for FINAL.
CREATE TABLE IF NOT EXISTS readings_per_minute
(
    channel    LowCardinality(String),
    minute     DateTime('UTC'),
    readings   AggregateFunction(uniqExact, UInt64),
    value_avg  AggregateFunction(avg, Float64),
    value_min  AggregateFunction(min, Float64),
    value_max  AggregateFunction(max, Float64),
    seq_min    AggregateFunction(min, UInt64),
    seq_max    AggregateFunction(max, UInt64)
)
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMM(minute)
ORDER BY (channel, minute);

CREATE MATERIALIZED VIEW IF NOT EXISTS readings_per_minute_mv TO readings_per_minute AS
SELECT
    channel,
    toStartOfMinute(event_ts) AS minute,
    uniqExactState(seq)       AS readings,
    avgState(value)           AS value_avg,
    minState(value)           AS value_min,
    maxState(value)           AS value_max,
    minState(seq)             AS seq_min,
    maxState(seq)             AS seq_max
FROM readings
GROUP BY channel, minute;

-- The exact view, for anything that must not see a pre-merge duplicate. FINAL costs a
-- merge at query time, which is why it is a separate view rather than the default path:
-- callers choose correctness or speed explicitly instead of getting one by accident.
CREATE VIEW IF NOT EXISTS readings_exact AS SELECT * FROM readings FINAL;
