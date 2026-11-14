-- Experiment registry and results (plan §4, stage `experiments`).
-- Additive only: new tables, no rewrite of existing rows, no backfill.
--
-- An experiment run is REGISTERED with its manifest (criteria locked before
-- results exist, digest recorded) and its per-repeat results are appended
-- row by row. The digest column ties results to the exact criteria that
-- produced them: a manifest edited after results exist fails the digest
-- verification instead of silently reinterpreting old rows.
--
-- No promotion semantics live here: the registry stores comparisons and
-- verdicts as data; changing runtime config, operator settings or risk
-- limits is out of scope for this schema and for every writer of it.

CREATE TABLE IF NOT EXISTS experiment_runs (
    run_id           TEXT PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('walkforward', 'replay_experiment')),
    hypothesis       TEXT NOT NULL,
    manifest         JSONB NOT NULL,
    manifest_digest  TEXT NOT NULL,
    -- The criteria the run locked before its first result row exists.
    criteria         JSONB NOT NULL,
    seed             BIGINT NOT NULL,
    repeat_count     INTEGER NOT NULL CHECK (repeat_count >= 1),
    variants         JSONB NOT NULL,
    window_start     TIMESTAMPTZ,
    window_end       TIMESTAMPTZ,
    holdout_start    TIMESTAMPTZ,
    horizon_days     INTEGER NOT NULL CHECK (horizon_days >= 0),
    embargo_days     INTEGER NOT NULL CHECK (embargo_days >= 0),
    data_source      TEXT NOT NULL,
    git_sha          TEXT,
    config_hash      TEXT,
    artifact_dir     TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(variants) = 'array')
);

CREATE TABLE IF NOT EXISTS experiment_results (
    result_id        UUID PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES experiment_runs(run_id) ON DELETE CASCADE,
    variant          TEXT NOT NULL,
    repeat_index     INTEGER NOT NULL CHECK (repeat_index >= 0),
    seed             BIGINT NOT NULL,
    manifest_digest  TEXT NOT NULL,
    metric_name      TEXT NOT NULL,
    metric_value     NUMERIC,
    llm_tokens       BIGINT NOT NULL DEFAULT 0 CHECK (llm_tokens >= 0),
    llm_calls        BIGINT NOT NULL DEFAULT 0 CHECK (llm_calls >= 0),
    llm_latency_ms   NUMERIC NOT NULL DEFAULT 0 CHECK (llm_latency_ms >= 0),
    -- The full per-repeat report (equity curve, costs, parity log) as the
    -- engine produced it; never a hand-edited summary.
    report           JSONB NOT NULL,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One row per (run, variant, repeat): a replayed write is a duplicate,
    -- not a second result.
    UNIQUE (run_id, variant, repeat_index)
);
CREATE INDEX IF NOT EXISTS idx_experiment_results_run
    ON experiment_results(run_id, variant, repeat_index);

CREATE TABLE IF NOT EXISTS experiment_comparisons (
    comparison_id    UUID PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES experiment_runs(run_id) ON DELETE CASCADE,
    challenger       TEXT NOT NULL,
    control          TEXT NOT NULL,
    metric_name      TEXT NOT NULL,
    pairs            INTEGER NOT NULL CHECK (pairs >= 0),
    mean_difference  NUMERIC,
    interval_low     NUMERIC,
    interval_high    NUMERIC,
    confidence       NUMERIC,
    -- Verdicts come from tradingagents.experiments.stats: either variant of
    -- insufficient_evidence_* or consistent_difference. Never 'winner'.
    verdict          TEXT NOT NULL CHECK (verdict IN (
                         'insufficient_evidence_coverage',
                         'insufficient_evidence_interval',
                         'consistent_difference')),
    reason           TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, challenger, control, metric_name)
);
