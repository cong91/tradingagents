"""Offline checks for the bounded entry-recovery snapshot query."""

from unittest.mock import MagicMock, Mock

import pytest

from tradingagents.execution import futures_entry_recovery_snapshot as snapshot
from tradingagents.execution.futures_entry_journal import _ACTIVE_STATUSES

pytestmark = pytest.mark.unit

SYMBOL = "ABC/USDT:USDT"
ENTRY_ID = "venue-order"
INTENT_ID = f"entry-intent:{SYMBOL}:client-order"


@pytest.fixture
def snapshot_cursor(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    read = MagicMock()
    read.return_value.__enter__.return_value = connection
    monkeypatch.setattr(snapshot.db, "read", read)
    return read, connection, cursor


def test_snapshot_uses_one_bounded_select_for_all_relevant_tasks(snapshot_cursor):
    read, connection, cursor = snapshot_cursor
    intent = {"task_id": INTENT_ID}
    bracket = {"task_id": f"bracket:{SYMBOL}:{ENTRY_ID}", "status": "completed"}
    cursor.fetchall.return_value = [intent, bracket]

    assert snapshot._read_snapshot(SYMBOL, ENTRY_ID, INTENT_ID) == [intent, bracket]

    read.assert_called_once_with()
    connection.cursor.assert_called_once_with()
    cursor.execute.assert_called_once()
    sql, params = cursor.execute.call_args.args
    normalized_sql = " ".join(sql.split())
    assert normalized_sql.startswith("SELECT * FROM bracket_recovery_tasks WHERE")
    assert "task_id = %s OR task_id = %s" in normalized_sql
    assert "(symbol = %s AND entry_order_id = %s)" in normalized_sql
    assert "status = ANY(%s)" in normalized_sql
    assert normalized_sql.endswith("LIMIT %s")
    assert params == (
        INTENT_ID, f"bracket:{SYMBOL}:{ENTRY_ID}", SYMBOL, ENTRY_ID,
        list(_ACTIVE_STATUSES), snapshot._MAX_SNAPSHOT_ROWS + 1,
    )
    cursor.fetchall.assert_called_once_with()


def test_snapshot_rejects_overflow_instead_of_assuming_absence(snapshot_cursor):
    _, _, cursor = snapshot_cursor
    cursor.fetchall.return_value = [{"task_id": "other"}] * (snapshot._MAX_SNAPSHOT_ROWS + 1)

    with pytest.raises(RuntimeError, match="snapshot exceeded row limit"):
        snapshot._read_snapshot(SYMBOL, ENTRY_ID, INTENT_ID)


def test_snapshot_propagates_store_failure(snapshot_cursor):
    _, _, cursor = snapshot_cursor
    cursor.execute = Mock(side_effect=OSError("unavailable"))

    with pytest.raises(OSError, match="unavailable"):
        snapshot._read_snapshot(SYMBOL, ENTRY_ID, INTENT_ID)
