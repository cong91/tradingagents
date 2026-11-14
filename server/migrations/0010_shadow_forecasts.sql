-- Shadow forecasts and their evaluations (plan §6, stage `shadow`).
-- Additive only: new tables, no rewrite of existing rows, no backfill.
--
-- A shadow forecast is an OBSERVATION: the runtime's orders, risk decisions and
-- protection never read this table. Rows are written by the independent
-- shadow_worker after a cycle is already terminal, so a slow, failing or absent
-- provider cannot change an order.
--
-- Honesty constraints encoded here:
--   * ``sample_count``/``paths`` describe HOW MANY model samples were drawn;
--     ``calibration_label`` says what that ratio is (sample ratio, not a
--     calibrated probability) and is NOT NULL so the distinction cannot be lost;
--   * ``generation_kind`` separates a forecast made forward (at the decision
--     moment) from a retrospective replay, so a backfilled row can never be
--     presented as a live prediction;
--   * ``data_open_time`` is the open time of the last CLOSED candle the forecast
--     consumed, plus the timeframe, so look-ahead is detectable;
--   * ``status`` carries the real outcome of the attempt — ``unavailable`` and
--     ``error`` are first-class, never rendered as a forecast;
--   * provenance (source/model revision, artifact hash, seed, deadline) is
--     recorded per row; nothing is inferred after the fact.

CREATE TABLE IF NOT EXISTS shadow_forecasts (
    forecast_id          UUID PRIMARY KEY,
    -- NULL for an observation that is not tied to one cycle (e.g. a replay row).
    cycle_id             TEXT,
    symbol               TEXT NOT NULL,
    timeframe            TEXT NOT NULL,
    horizon_hours        INTEGER NOT NULL CHECK (horizon_hours > 0),
    provider             TEXT NOT NULL,
    -- 'kronos:<revision>' or 'filter:<name>' in practice; free-form so a new
    -- provider does not need a migration.
    model_revision       TEXT NOT NULL,
    source_revision      TEXT,
    artifact_hash        TEXT,
    data_open_time       TIMESTAMPTZ NOT NULL,
    -- The moment the forecast actually came into existence. A late replay is
    -- marked retrospective in generation_kind and never presented as forward.
    prediction_created_at TIMESTAMPTZ NOT NULL,
    generation_kind      TEXT NOT NULL CHECK (generation_kind IN ('forward', 'retrospective')),
    outcome_group        TEXT NOT NULL CHECK (outcome_group IN ('executed', 'rejected', 'abstained')),
    outcome_reason       TEXT NOT NULL,
    -- Number of independent paths; each path is one sample_count=1 draw, so the
    -- ratio below is a sample ratio over N paths, not a probability.
    sample_count         INTEGER NOT NULL CHECK (sample_count >= 0),
    per_path_sample_count INTEGER NOT NULL DEFAULT 1 CHECK (per_path_sample_count = 1),
    distribution_kind    TEXT NOT NULL CHECK (distribution_kind IN ('model_samples', 'deterministic_rule')),
    calibration_label    TEXT NOT NULL,
    calibration_note     TEXT NOT NULL,
    direction            TEXT CHECK (direction IN ('up', 'down', 'flat')),
    -- Aggregated per-path statistics (direction ratio, percentile band, dispersion).
    samples              JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Per-path summaries (end/min/max/direction) in draw order.
    paths                JSONB NOT NULL DEFAULT '[]'::jsonb,
    status               TEXT NOT NULL CHECK (status IN ('ok', 'unavailable', 'error', 'timeout')),
    reason               TEXT,
    seed                 BIGINT NOT NULL,
    deadline_seconds     NUMERIC NOT NULL CHECK (deadline_seconds > 0),
    duration_ms          NUMERIC,
    params               JSONB NOT NULL DEFAULT '{}'::jsonb,
    provenance           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Idempotency: the same provider over the same closed candle, horizon and
    -- revision is one observation. A replay of the same window writes nothing.
    UNIQUE (provider, symbol, data_open_time, horizon_hours, model_revision)
);
CREATE INDEX IF NOT EXISTS idx_shadow_forecasts_symbol_time
    ON shadow_forecasts(symbol, data_open_time DESC);

CREATE TABLE IF NOT EXISTS shadow_evaluations (
    evaluation_id        UUID PRIMARY KEY,
    forecast_id          UUID NOT NULL REFERENCES shadow_forecasts(forecast_id) ON DELETE CASCADE,
    status               TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'unavailable')),
    reason               TEXT,
    -- Direction of the realized close against the reference close the forecast
    -- saw. NULL when the horizon has not fully realized yet.
    realized_direction   TEXT CHECK (realized_direction IN ('up', 'down', 'flat')),
    realized_direction_hit BOOLEAN,
    -- Did the realized path stay inside the forecast's percentile band? A rule
    -- provider has no band, so this stays NULL with the reason recorded.
    band_coverage        BOOLEAN,
    -- Which protective level the realized path reached first (worst-first when a
    -- candle contains both), reusing the simulation's own matcher.
    stop_first_rate      NUMERIC,
    net_r                NUMERIC,
    net_r_reason         TEXT,
    realized             JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One evaluation per forecast: re-running the evaluation overwrites nothing.
    UNIQUE (forecast_id)
);

CREATE TABLE IF NOT EXISTS shadow_state (
    state_key            TEXT PRIMARY KEY,
    cursor_value         TEXT NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
