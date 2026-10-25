"""PostgreSQL persistence for the control panel's operator-facing state.

One concern: the records an operator reads or sets that used to be small files
— approval queue, watchlist, scheduler state, operator overrides and the server
action trail. They are stored in ``server/migrations/0002_control_plane.sql``
and ``0003_operator_settings.sql``.

The execution audit the RiskGuard replays stays a JSONL file by design
(``exec_log_path``): it is a cross-process safety contract, not API state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from server import db


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- approvals -------------------------------------------------------------


def load_approvals() -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT payload FROM control_approvals ORDER BY id"
        ).fetchall()
    return [row["payload"] for row in rows]


def _next_approval_id(conn) -> str:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS next FROM control_approvals"
    ).fetchone()
    return f"ap_{int(row['next']):04d}"


def insert_approval(item: dict) -> dict:
    """Insert one approval; the numeric id is assigned here and in the payload."""
    with db.session() as conn:
        item = dict(item)
        item["id"] = _next_approval_id(conn)
        conn.execute(
            "INSERT INTO control_approvals (id, payload) VALUES (%s, %s::jsonb)",
            (int(item["id"][3:]), _json(item)),
        )
    return item


def upsert_approval(item: dict) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO control_approvals (id, payload) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET payload = excluded.payload",
            (int(item["id"][3:]), _json(item)),
        )


def save_approval(item: dict) -> None:
    upsert_approval(item)


# --- watchlist -------------------------------------------------------------


def load_watchlist() -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT symbol, added_at FROM control_watchlist ORDER BY sort_order, symbol"
        ).fetchall()
    return [{"symbol": row["symbol"], "added_at": row["added_at"]} for row in rows]


def replace_watchlist(items: list[dict]) -> None:
    with db.session() as conn:
        conn.execute("DELETE FROM control_watchlist")
        if items:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO control_watchlist (symbol, added_at, sort_order) "
                    "VALUES (%s, %s, %s)",
                    [
                        (item["symbol"], item["added_at"], index)
                        for index, item in enumerate(items)
                    ],
                )


# --- scheduler state -------------------------------------------------------


def load_autoloop_state() -> dict:
    with db.read() as conn:
        row = conn.execute(
            "SELECT payload FROM control_settings WHERE key = 'autoloop'"
        ).fetchone()
    return row["payload"] if row is not None else {}


def save_autoloop_state(state: dict) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO control_settings (key, payload) VALUES ('autoloop', %s::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET payload = excluded.payload",
            (_json(state),),
        )


# --- server action trail ---------------------------------------------------


def append_server_audit(event: dict) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO control_server_audit (payload) VALUES (%s::jsonb)",
            (_json(event),),
        )


def load_server_audit() -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT payload FROM control_server_audit ORDER BY id"
        ).fetchall()
    return [row["payload"] for row in rows]


# --- operator overrides ----------------------------------------------------


def load_operator_setting_rows() -> list[dict]:
    """Every stored override with its audit metadata, ordered by key."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at, updated_by "
            "FROM control_operator_settings ORDER BY key"
        ).fetchall()
    return [dict(row) for row in rows]


def load_operator_settings() -> dict:
    """The overrides as a plain ``{key: value}`` map for ``set_config``."""
    return {row["key"]: row["value"] for row in load_operator_setting_rows()}


def set_operator_setting(key: str, value, *, updated_by: str | None = None) -> None:
    with db.session() as conn:
        conn.execute(
            """INSERT INTO control_operator_settings (key, value, updated_at, updated_by)
               VALUES (%s, %s::jsonb, %s, %s)
               ON CONFLICT (key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
            (key, _json(value), _now(), updated_by),
        )


def delete_operator_setting(key: str) -> bool:
    with db.session() as conn:
        cursor = conn.execute(
            "DELETE FROM control_operator_settings WHERE key = %s", (key,)
        )
    return cursor.rowcount == 1
