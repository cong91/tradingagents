-- Control-panel state in PostgreSQL.
--
-- Source of truth for server/db.py, server/job_store.py and server/run_store.py.
-- The engine execution audit (exec_log_path) deliberately stays a JSONL file:
-- RiskGuard replays it as the trading safety contract.
--
-- The server stores ISO-8601 strings for timestamps today, so the columns are
-- TEXT/JSONB to keep API payloads byte-compatible during the cutover. Orders
-- and money belong to the separate ledger migration, not this table set.

CREATE TABLE IF NOT EXISTS control_jobs (
    job_id          TEXT PRIMARY KEY,
    mode            TEXT NOT NULL,
    status          TEXT NOT NULL,
    auto            BOOLEAN NOT NULL DEFAULT false,
    top_n           INTEGER,
    tickers         JSONB NOT NULL DEFAULT '[]'::jsonb,
    asset_types     JSONB NOT NULL DEFAULT '{}'::jsonb,
    screening       JSONB,
    error           TEXT,
    created_at      TEXT NOT NULL,
    finished_at     TEXT,
    last_progress_at TEXT,
    current_ticker  TEXT
);
CREATE INDEX IF NOT EXISTS idx_control_jobs_created
    ON control_jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS control_job_results (
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES control_jobs(job_id) ON DELETE CASCADE,
    ticker      TEXT NOT NULL,
    asset_type  TEXT,
    signal      TEXT,
    reason      TEXT,
    error       TEXT,
    approval_id TEXT,
    mock_source TEXT,
    plan        JSONB,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_control_job_results_job
    ON control_job_results(job_id, id);

CREATE TABLE IF NOT EXISTS control_runs (
    run_id       TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    asset_type   TEXT NOT NULL,
    mode         TEXT NOT NULL,
    status       TEXT NOT NULL,
    signal       TEXT,
    is_review    BOOLEAN,
    error        JSONB,
    report_paths JSONB,
    analysts     JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_control_runs_created
    ON control_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS control_run_events (
    id      BIGSERIAL PRIMARY KEY,
    run_id  TEXT NOT NULL REFERENCES control_runs(run_id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    type    TEXT NOT NULL,
    role    TEXT,
    stage   TEXT,
    agent   TEXT,
    replay  BOOLEAN NOT NULL DEFAULT false,
    payload JSONB NOT NULL,
    UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS futures_signal_claims (
    signal_id   TEXT PRIMARY KEY,
    claimed_at  TEXT NOT NULL,
    lease_until TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'claimed',
    completed_at TEXT,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_control_futures_claims_lease
    ON futures_signal_claims(lease_until);

CREATE TABLE IF NOT EXISTS bracket_recovery_tasks (
    task_id        TEXT PRIMARY KEY,
    symbol         TEXT NOT NULL,
    entry_order_id TEXT NOT NULL,
    close_side     TEXT NOT NULL,
    quantity       DOUBLE PRECISION NOT NULL,
    legs           JSONB NOT NULL,
    placed_ids     JSONB NOT NULL DEFAULT '[]'::jsonb,
    status         TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    updated_at     TEXT NOT NULL,
    venue_key      TEXT
);
CREATE INDEX IF NOT EXISTS idx_control_bracket_recovery_status
    ON bracket_recovery_tasks(status);

CREATE TABLE IF NOT EXISTS position_states (
    position_key TEXT PRIMARY KEY,
    revision     INTEGER NOT NULL,
    state        JSONB NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_monitor_leases (
    position_key TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    lease_until  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_control_position_monitor_lease_until
    ON position_monitor_leases(lease_until);

CREATE TABLE IF NOT EXISTS futures_cycles (
    cycle_id         TEXT PRIMARY KEY,
    symbol           TEXT NOT NULL,
    four_hour_open   TEXT NOT NULL,
    one_hour_open    TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    status           TEXT NOT NULL,
    phase            TEXT NOT NULL,
    payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    finished_at      TEXT,
    UNIQUE (symbol, four_hour_open, one_hour_open, strategy_version)
);
CREATE INDEX IF NOT EXISTS idx_control_futures_cycles_updated
    ON futures_cycles(updated_at DESC);

-- Approvals, watchlist, scheduler state and the server action trail used to be
-- JSON/JSONL files. They are operator-visible records, so they belong with the
-- rest of the control-plane state instead of a mutable file on disk.
CREATE TABLE IF NOT EXISTS control_approvals (
    id      BIGINT PRIMARY KEY,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS control_watchlist (
    symbol     TEXT PRIMARY KEY,
    added_at   TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS control_settings (
    key     TEXT PRIMARY KEY,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS control_server_audit (
    id      BIGSERIAL PRIMARY KEY,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_control_server_audit_created
    ON control_server_audit(id DESC);
