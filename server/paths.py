"""Filesystem layout for server-local state (watchlist, approval queue, trail).

Server state lives beside the code under ``server/data/`` (gitignored): the
approval queue must survive restarts and stay inspectable next to the server
that owns it. The contract's engine-side state (``~/.tradingagents/``) is
untouched; tests redirect these paths via monkeypatch — every consumer reads
them through this module at call time.

Job/run state moved to SQLite (``control.db``, see ``server/db.py``): those
records are what the UI lists, filters and pages, which a JSONL file cannot
answer. The small single-record files below (watchlist, autoloop state,
operator arming) stay files — they are read whole, never queried.
"""

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

WATCHLIST_PATH = DATA_DIR / "watchlist.json"
APPROVALS_PATH = DATA_DIR / "approvals.jsonl"
SERVER_AUDIT_PATH = DATA_DIR / "audit.jsonl"
AUTOLOOP_PATH = DATA_DIR / "autoloop.json"
# Operator-owned arming overrides (e.g. {"exec_auto_confirm": true}): the
# shipped defaults stay fail-closed; an operator arms the gates through this
# local, gitignored file instead of editing engine defaults.
OPERATOR_CONFIG_PATH = DATA_DIR / "operator_config.json"


def db_path() -> Path:
    """The SQLite file holding API-layer state (jobs, runs, agent events).

    Resolved at call time from ``DATA_DIR`` so a test that redirects the data
    directory moves the database with it — the same contract as the constants
    above. The engine's execution audit stays a JSONL file: that log is the
    RiskGuard's replay contract shared across processes, not API state.
    """
    return DATA_DIR / "control.db"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
