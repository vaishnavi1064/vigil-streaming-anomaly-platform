-- Postgres holds detected episodes and application state. It never holds raw readings:
-- serving data goes to ClickHouse (Phase 3) and the durable lake is Iceberg. Keeping raw
-- telemetry out is what lets this database stay small enough to be operationally boring.
--
-- Idempotent: safe to run on every startup.

CREATE TABLE IF NOT EXISTS context_events (
    -- The id the producer assigned (for example deploy-0007), not a surrogate key: it is
    -- how a marker is identified on the wire, and an episode's attribution has to survive
    -- a replay of the context topic without silently pointing at a different row.
    event_id        TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('deploy', 'pipeline')),
    t_start_ms      BIGINT NOT NULL,
    t_end_ms        BIGINT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    detail          TEXT NOT NULL,
    -- Channels the event can explain. Empty means fleet-wide. Scope is enforced, not
    -- decorative: an event must not explain an excursion on a channel it never touched.
    scope           TEXT[] NOT NULL DEFAULT '{}',
    -- Ground truth on synthetic runs only: whether the event actually perturbed telemetry.
    -- No conditioning logic reads this; the evaluation harness does.
    perturbed_telemetry BOOLEAN,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT context_events_span CHECK (t_end_ms >= t_start_ms)
);

CREATE INDEX IF NOT EXISTS context_events_span_idx ON context_events (t_start_ms, t_end_ms);
CREATE INDEX IF NOT EXISTS context_events_kind_idx ON context_events (kind);

CREATE TABLE IF NOT EXISTS episodes (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel         TEXT NOT NULL,
    t_start_ms      BIGINT NOT NULL,
    t_end_ms        BIGINT NOT NULL,
    -- The detector whose score was highest, not the only one that looked: every
    -- detector's opinion lives in episode_scores.
    raised_by       TEXT NOT NULL,
    peak_score      DOUBLE PRECISION NOT NULL,
    mean_score      DOUBLE PRECISION NOT NULL,
    window_count    INTEGER NOT NULL,
    threshold       DOUBLE PRECISION NOT NULL,
    status          TEXT NOT NULL DEFAULT 'real'
                    CHECK (status IN ('real', 'attributed', 'suppressed')),
    -- Set when conditioning attributes this episode to an operational event (Phase 3).
    attributed_to   TEXT REFERENCES context_events (event_id),
    explanation     TEXT,
    -- Ground truth from the synthetic source: which origins ('fault', 'deploy',
    -- 'pipeline') were actually injected inside this episode's span. Empty on live data.
    injected_origins TEXT[] NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT episodes_span CHECK (t_end_ms >= t_start_ms),
    -- One episode per channel per span. A restarted consumer replaying from its last
    -- committed offset re-derives the same episodes; without this the replay would
    -- duplicate them, and a reconciliation harness that then reported drift would be
    -- reporting our own bookkeeping rather than a pipeline fault.
    CONSTRAINT episodes_identity UNIQUE (channel, t_start_ms, raised_by)
);

CREATE INDEX IF NOT EXISTS episodes_recent_idx ON episodes (created_at DESC);
CREATE INDEX IF NOT EXISTS episodes_channel_span_idx ON episodes (channel, t_start_ms);
CREATE INDEX IF NOT EXISTS episodes_status_idx ON episodes (status);

CREATE TABLE IF NOT EXISTS episode_scores (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    episode_id      BIGINT NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    detector        TEXT NOT NULL,
    window_start_ms BIGINT NOT NULL,
    window_end_ms   BIGINT NOT NULL,
    score           DOUBLE PRECISION NOT NULL,
    -- Per window, not aggregated, so the hot-path budget in NFR-1 can be reported as a
    -- percentile over real work instead of an average that hides the tail.
    latency_ms      DOUBLE PRECISION NOT NULL,
    UNIQUE (episode_id, detector, window_start_ms)
);

CREATE INDEX IF NOT EXISTS episode_scores_episode_idx ON episode_scores (episode_id);
CREATE INDEX IF NOT EXISTS episode_scores_detector_idx ON episode_scores (detector);

CREATE TABLE IF NOT EXISTS agent_actions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    episode_id      BIGINT NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    diagnosis       TEXT NOT NULL,
    proposed_action JSONB NOT NULL,
    -- The deterministic gate's verdict. Nothing executes without one, so it is NOT NULL:
    -- an action row with no verdict would mean something ran ungated.
    gate_verdict    TEXT NOT NULL CHECK (gate_verdict IN ('approved', 'rejected')),
    gate_reason     TEXT NOT NULL,
    execution_result JSONB,
    trace           JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_actions_episode_idx ON agent_actions (episode_id);
