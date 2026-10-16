"""SQLite store for the control panel's own state.

One concern: the durable, queryable record behind the API layer. Everything an
operator must still see after a restart — daily jobs and their per-coin
results, analysis runs and the agent event stream each produced — lives here
instead of in a process-global dict, so the UI can list, filter and page it
long after the worker thread that wrote it is gone.

Stdlib ``sqlite3`` on purpose: no new dependency, one file on disk, real SQL
for the queries the UI asks. WAL keeps a reader (an API request) from blocking
on the writer (a job worker), and ``busy_timeout`` absorbs the short overlap.

Scope boundary: this is API-layer state. The engine's execution audit
(``exec_log_path``) stays a JSONL file — the RiskGuard replays it across
processes as the trading safety contract, and moving it would put a database
between the guard and the evidence it refuses orders on.
"""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from server import paths

# Bumped when a migration below changes the shape; recorded in PRAGMA
# user_version so a future release can tell which schema a file carries.
SCHEMA_VERSION = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    mode         TEXT NOT NULL,
    status       TEXT NOT NULL,
    auto         INTEGER NOT NULL DEFAULT 0,
    top_n        INTEGER,
    tickers      TEXT NOT NULL DEFAULT '[]',
    asset_types  TEXT NOT NULL DEFAULT '{}',
    screening    TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    finished_at  TEXT,
    last_progress_at TEXT,
    current_ticker TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS job_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    ticker      TEXT NOT NULL,
    asset_type  TEXT,
    signal      TEXT,
    reason      TEXT,
    error       TEXT,
    approval_id TEXT,
    mock_source TEXT,
    plan        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_results_job ON job_results(job_id, id);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    asset_type   TEXT NOT NULL,
    mode         TEXT NOT NULL,
    status       TEXT NOT NULL,
    signal       TEXT,
    is_review    INTEGER,
    error        TEXT,
    report_paths TEXT,
    analysts     TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

CREATE TABLE IF NOT EXISTS run_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    type    TEXT NOT NULL,
    role    TEXT,
    stage   TEXT,
    agent   TEXT,
    replay  INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    UNIQUE(run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, seq);

CREATE TABLE IF NOT EXISTS futures_signal_claims (
    signal_id   TEXT PRIMARY KEY,
    claimed_at  TEXT NOT NULL,
    lease_until TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'claimed',
    completed_at TEXT,
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_futures_claims_lease ON futures_signal_claims(lease_until);

CREATE TABLE IF NOT EXISTS bracket_recovery_tasks (
    task_id      TEXT PRIMARY KEY,
    symbol       TEXT NOT NULL,
    entry_order_id TEXT NOT NULL,
    close_side   TEXT NOT NULL,
    quantity     REAL NOT NULL,
    legs         TEXT NOT NULL,
    placed_ids   TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bracket_recovery_status ON bracket_recovery_tasks(status);

CREATE TABLE IF NOT EXISTS position_states (
    position_key TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS futures_cycles (
    cycle_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    four_hour_open TEXT NOT NULL,
    one_hour_open TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(symbol, four_hour_open, one_hour_open, strategy_version)
);
CREATE INDEX IF NOT EXISTS idx_futures_cycles_updated ON futures_cycles(updated_at DESC);
"""

_init_lock = threading.Lock()
_initialized_for: Path | None = None


def _ensure_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Create the schema once per resolved path.

    Lazy rather than startup-only: the test apps mount individual routers
    without main.py's lifespan, and a store must never depend on the caller
    remembering to initialize it.
    """
    global _initialized_for
    if _initialized_for == path:
        return
    with _init_lock:
        if _initialized_for == path:
            return
        conn.executescript(_SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "last_progress_at" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN last_progress_at TEXT")
        if "current_ticker" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN current_ticker TEXT")
        claim_columns = {row[1] for row in conn.execute("PRAGMA table_info(futures_signal_claims)")}
        if "completed_at" not in claim_columns:
            conn.execute("ALTER TABLE futures_signal_claims ADD COLUMN completed_at TEXT")
        if "reason" not in claim_columns:
            conn.execute("ALTER TABLE futures_signal_claims ADD COLUMN reason TEXT")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _initialized_for = path


def connect() -> sqlite3.Connection:
    """A fresh connection with row access by name and the schema in place.

    Per-call connections, not a shared one: FastAPI handlers run on a thread
    pool and a job worker writes from its own thread, so a module-level handle
    would need ``check_same_thread=False`` plus external locking. Opening is
    cheap and SQLite serializes writers itself.
    """
    path = paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    _ensure_schema(conn, path)
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """A connection whose writes commit as one transaction (or roll back whole)."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    """A connection for SELECTs; nothing is written, nothing to commit."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def is_readable() -> bool:
    """Whether the database answers — the health endpoint's probe."""
    try:
        with read() as conn:
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        return False
    return True
