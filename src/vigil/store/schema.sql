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
    -- Event time of the reading that drove the episode, as opposed to the boundary of the
    -- window that noticed it. Nullable: a detector that cannot name a driving sample says so
    -- rather than repeating the boundary and making the two indistinguishable.
    onset_ms        BIGINT,
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

-- Added after the table existed in deployed databases, so it cannot live in the CREATE
-- above: CREATE TABLE IF NOT EXISTS never alters an existing table.
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS onset_ms BIGINT;
-- Which conditioning test decided this episode. Same reason as onset_ms above: added
-- after the table existed. Null on the unconditioned pass, where nothing decided.
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS verdict TEXT;

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

-- The per-window pipeline-health signal from the reconciliation harness. This is both the
-- correctness evidence and, from Phase 3, the input the detector conditions on -- so it is
-- stored for every window including clean ones. A consumer must be able to tell "clean"
-- from "no signal": conditioning fails open on a missing signal (ADR-007), and collapsing
-- the two would make that rule unimplementable.
CREATE TABLE IF NOT EXISTS pipeline_health (
    window_start_ms BIGINT PRIMARY KEY,
    window_end_ms   BIGINT NOT NULL,
    channels        INTEGER NOT NULL,
    readings        BIGINT NOT NULL,
    missing         BIGINT NOT NULL,
    duplicates      BIGINT NOT NULL,
    regressions     BIGINT NOT NULL,
    max_lag_ms      BIGINT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('ok', 'info', 'warning', 'critical')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pipeline_health_span CHECK (window_end_ms > window_start_ms)
);

CREATE INDEX IF NOT EXISTS pipeline_health_span_idx
    ON pipeline_health (window_start_ms, window_end_ms);
CREATE INDEX IF NOT EXISTS pipeline_health_disturbed_idx
    ON pipeline_health (severity) WHERE severity <> 'ok';

-- One row per reconciliation run: the evidence behind any zero-drift claim. Kept as history
-- rather than a single current value, so a claim can be traced to the run that produced it.
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic            TEXT NOT NULL,
    duration_s       DOUBLE PRECISION NOT NULL,
    readings         BIGINT NOT NULL,
    channels         INTEGER NOT NULL,
    -- Sum of per-channel (span - readings). Zero is the claim.
    drift            BIGINT NOT NULL,
    missing          BIGINT NOT NULL,
    duplicates       BIGINT NOT NULL,
    regressions      BIGINT NOT NULL,
    -- The independent check: what the broker retained vs. what we consumed.
    broker_available BIGINT NOT NULL,
    broker_consumed  BIGINT NOT NULL,
    offset_drift     BIGINT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reconciliation_runs_recent_idx ON reconciliation_runs (created_at DESC);
