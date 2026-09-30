"""Filesystem layout for server-local state (watchlist, approval queue, trail).

Server state lives beside the code under ``server/data/`` (gitignored): the
approval queue must survive restarts and stay inspectable next to the server
that owns it. The contract's engine-side state (``~/.tradingagents/``) is
untouched; tests redirect these paths via monkeypatch — every consumer reads
them through this module at call time.
"""

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

WATCHLIST_PATH = DATA_DIR / "watchlist.json"
APPROVALS_PATH = DATA_DIR / "approvals.jsonl"
SERVER_AUDIT_PATH = DATA_DIR / "audit.jsonl"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
