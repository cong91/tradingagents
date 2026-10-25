"""One-off importer for the control panel's legacy JSON/JSONL state files.

The approval queue, watchlist, scheduler state and server action trail used to
be files under ``server/data``. They now live in PostgreSQL (``control_store``),
so this command copies whatever the old files still hold and leaves the
originals untouched.

Run it explicitly, with the API stopped so nothing writes concurrently::

    python -m server.import_control_files --source-dir server/data --dry-run
    python -m server.import_control_files --source-dir server/data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from server import control_store


def _read_json(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"skip: {path.name}: unreadable ({exc})")
        return None


def _read_jsonl(path: Path) -> list[dict]:
    """Every parseable object in a JSONL file; a torn last line is skipped."""
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"skip: {path.name}: unreadable ({exc})")
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def import_approvals(source_dir: Path, *, dry_run: bool) -> int:
    rows = _read_jsonl(source_dir / "approvals.jsonl")
    if dry_run:
        print(f"plan: approvals: {len(rows)} rows")
        return 0
    imported = 0
    for item in rows:
        if not str(item.get("id", "")).startswith("ap_"):
            continue
        control_store.upsert_approval(item)
        imported += 1
    print(f"imported: approvals: {imported} rows (idempotent)")
    return imported


def import_watchlist(source_dir: Path, *, dry_run: bool) -> int:
    data = _read_json(source_dir / "watchlist.json")
    entries = data.get("symbols") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        print("skip: watchlist: no symbols list")
        return 0
    items = [
        {"symbol": str(entry["symbol"]), "added_at": str(entry.get("added_at") or "")}
        for entry in entries
        if isinstance(entry, dict) and entry.get("symbol")
    ]
    if dry_run:
        print(f"plan: watchlist: {len(items)} rows")
        return 0
    if items and not control_store.load_watchlist():
        control_store.replace_watchlist(items)
        print(f"imported: watchlist: {len(items)} rows")
        return len(items)
    print("skip: watchlist: PostgreSQL already has entries")
    return 0


def import_autoloop(source_dir: Path, *, dry_run: bool) -> int:
    data = _read_json(source_dir / "autoloop.json")
    if not isinstance(data, dict) or not data:
        print("skip: autoloop: no state")
        return 0
    if dry_run:
        print(f"plan: autoloop: enabled={data.get('enabled')} "
              f"interval={data.get('interval_minutes')}")
        return 0
    if control_store.load_autoloop_state():
        print("skip: autoloop: PostgreSQL already has state")
        return 0
    control_store.save_autoloop_state(data)
    print("imported: autoloop: 1 row")
    return 1


def import_server_audit(source_dir: Path, *, dry_run: bool) -> int:
    rows = _read_jsonl(source_dir / "audit.jsonl")
    if dry_run:
        print(f"plan: server_audit: {len(rows)} rows")
        return 0
    if control_store.load_server_audit():
        print("skip: server_audit: PostgreSQL already has a trail")
        return 0
    for event in rows:
        control_store.append_server_audit(event)
    print(f"imported: server_audit: {len(rows)} rows")
    return len(rows)


def import_operator_config(source_dir: Path, *, dry_run: bool) -> int:
    """Legacy ``operator_config.json`` -> ``control_operator_settings``.

    Keys outside the allowlist are reported and skipped: the old file accepted
    any key, while the table only stores what ``operator_config._ALLOWED``
    declares, so an unknown key must not silently become live config.
    """
    from server.operator_config import _ALLOWED

    data = _read_json(source_dir / "operator_config.json")
    if not isinstance(data, dict) or not data:
        print("skip: operator_config: no overrides")
        return 0

    accepted = {key: value for key, value in data.items() if key in _ALLOWED}
    rejected = sorted(key for key in data if key not in _ALLOWED)
    if rejected:
        print(f"skip: operator_config: keys outside the allowlist {rejected}")

    if dry_run:
        print(f"plan: operator_config: {len(accepted)} rows -> {sorted(accepted)}")
        return 0
    if control_store.load_operator_settings():
        print("skip: operator_config: PostgreSQL already has overrides")
        return 0
    for key, value in accepted.items():
        control_store.set_operator_setting(key, value, updated_by="import_control_files")
    print(f"imported: operator_config: {len(accepted)} rows")
    return len(accepted)


_IMPORTERS = (
    ("approvals", import_approvals),
    ("watchlist", import_watchlist),
    ("autoloop", import_autoloop),
    ("server_audit", import_server_audit),
    ("operator_config", import_operator_config),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import legacy JSON/JSONL control state into PostgreSQL")
    parser.add_argument("--source-dir", required=True, type=Path, help="directory holding the legacy files")
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    args = parser.parse_args()

    if not args.source_dir.is_dir():
        parser.error(f"source directory not found: {args.source_dir}")

    for _name, importer in _IMPORTERS:
        importer(args.source_dir, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
