-- Additive, read-only forecast evaluation evidence.
-- Rows are keyed by cycle + forecast digest + stable horizon key and contain no execution gate.
-- The worker initializes its cursor at enable time; it never backfills older cycles.
CREATE TABLE IF NOT EXISTS forecast_evaluations (
    evaluation_id UUID PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    forecast_digest TEXT NOT NULL,
    horizon_key TEXT NOT NULL,
    forecast_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(forecast_snapshot) = 'object'),
    venue_scope JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(venue_scope) = 'object'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'evaluated', 'unavailable', 'invalid')),
    forecast_as_of TIMESTAMPTZ,
    forecast_created_at TIMESTAMPTZ,
    forecast_recorded_at TIMESTAMPTZ,
    target_as_of TIMESTAMPTZ,
    timeframe TEXT,
    profile TEXT,
    effective_lookback_bars INTEGER
        CHECK (effective_lookback_bars IS NULL OR effective_lookback_bars > 0),
    horizon_bars INTEGER CHECK (horizon_bars IS NULL OR horizon_bars > 0),
    horizon_hours NUMERIC,
    generation_kind TEXT,
    direction TEXT,
    direction_ratio JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(direction_ratio) = 'object'),
    band JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(band) = 'object'),
    reference_close NUMERIC CHECK (reference_close IS NULL OR reference_close > 0),
    predicted_close NUMERIC CHECK (predicted_close IS NULL OR predicted_close > 0),
    target_close NUMERIC CHECK (target_close IS NULL OR target_close > 0),
    target_return NUMERIC,
    absolute_error NUMERIC CHECK (absolute_error IS NULL OR absolute_error >= 0),
    absolute_error_pct NUMERIC CHECK (absolute_error_pct IS NULL OR absolute_error_pct >= 0),
    directional_correct BOOLEAN,
    band_contains_target BOOLEAN,
    reason TEXT,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(warnings) = 'array'),
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(provenance) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    evaluated_at TIMESTAMPTZ,
    UNIQUE (cycle_id, forecast_digest, horizon_key)
);
CREATE INDEX IF NOT EXISTS idx_forecast_evaluations_symbol_time
    ON forecast_evaluations(symbol, forecast_as_of DESC NULLS LAST, created_at DESC, evaluation_id);
CREATE INDEX IF NOT EXISTS idx_forecast_evaluations_status_due
    ON forecast_evaluations(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_forecast_evaluations_global_time
    ON forecast_evaluations(forecast_as_of DESC NULLS LAST, created_at DESC, evaluation_id);
CREATE INDEX IF NOT EXISTS idx_futures_cycles_forecast_scan
    ON futures_cycles(updated_at, cycle_id)
    WHERE status IN ('completed', 'rejected', 'failed', 'interrupted')
      AND payload ? 'kronos_forecast';

CREATE TABLE IF NOT EXISTS forecast_evaluation_state (
    state_key TEXT PRIMARY KEY,
    cursor_value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
