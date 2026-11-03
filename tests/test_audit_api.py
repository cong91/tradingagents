"""Control-panel audit API tests."""

import json

import pytest

from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    _today,
    client as client,
)
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit


# --- audit ----------------------------------------------------------------------


def test_audit_reads_engine_log_with_filters_and_halt(client, tmp_path):
    audit_file = tmp_path / "audit.jsonl"
    today = _today()
    audit_file.write_text("\n".join([
        json.dumps({"timestamp": f"{today}T04:02:00.509077+00:00", "ticker": "BTC-USD",
                    "ccxt_symbol": "BTC/USDT", "signal": "Buy", "action": "buy",
                    "qty": 0.0012, "price_est": 83078.0, "mode": "dry", "confirmed": True,
                    "order_id": None, "phase": "execute", "market": "spot", "leverage": 1.0,
                    "reason": "dry fill"}),
        json.dumps({"event": "halt", "timestamp": f"{today}T05:00:00+00:00",
                    "reason": "daily loss cap breached"}),
        "{oops: not json",
    ]) + "\n", encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})

    body = client.get("/api/audit").json()
    assert body["total_lines"] == 2
    assert body["skipped_lines"] == 1
    assert body["halted_today"] is True
    assert body["halt_reason"] == "daily loss cap breached"
    assert body["last_halt"].startswith(today)
    assert body["last_halt_reset"] is None
    assert [item["action"] for item in body["items"] if "action" in item] == ["buy"]  # newest first
    assert body["items"][0]["_line_no"] == 2  # the halt line is the newest event

    filtered = client.get("/api/audit", params={"action": "buy", "phase": "execute"}).json()
    assert filtered["total_matching"] == 1
    assert filtered["items"][0]["ticker"] == "BTC-USD"


def test_audit_missing_log_is_empty_not_error(client, tmp_path):
    set_config({"exec_log_path": str(tmp_path / "absent.jsonl")})
    body = client.get("/api/audit").json()
    assert body["items"] == []
    assert body["total_lines"] == 0
    assert body["halted_today"] is False
