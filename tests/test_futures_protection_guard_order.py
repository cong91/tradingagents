"""Non-mutating diagnostics remain available without verified account ownership."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server import job_store
from tradingagents.execution.futures_protection import manage_active_positions

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("allow_mutation", "mark", "opened_at", "expected"),
    [
        (False, 105.0, True, "mutation_gated"),
        (True, None, True, "invalid_position_snapshot"),
        (True, 105.0, False, "opened_at_unknown"),
        (True, 105.0, True, "account_identity_unknown"),
    ],
)
def test_protection_diagnostics_do_not_mutate_unverified_account(
    postgres_schema, caplog, allow_mutation, mark, opened_at, expected,
):
    symbol = "BTC/USDT:USDT"
    position_key = f"{symbol}:buy:entry-1"
    task_id = f"bracket:{symbol}:entry-1"
    assert job_store.persist_futures_entry_state(
        position_key, side="long", entry=100.0, quantity=1.0,
        stop=90.0, targets=[130.0], mark=100.0, order_id="entry-1",
    )
    state = job_store.load_position_state(position_key)
    state["bracket_task_id"] = task_id
    if not opened_at:
        state["snapshot"].pop("opened_at")
    assert job_store.save_position_state(position_key, state["revision"], state)
    assert job_store.create_bracket_recovery(
        task_id, symbol=symbol, entry_order_id="entry-1", close_side="sell", quantity=1.0,
        legs=[{"name": "stop-loss", "status": "placed", "order_id": "stop-1",
               "params": {"stopLossPrice": 90.0}}],
        now=datetime.now(UTC).isoformat(),
        venue_key="binance|https://demo-api.binance.com|True",
    )
    before_state = job_store.load_position_state(position_key)
    before_task = job_store.load_bracket_recovery(task_id)
    calls = []

    def raw_position_risk(_params=None):
        row = {
            "symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1.000000",
            "entryPrice": "100.0", "marginAsset": "USDT",
        }
        if mark is not None:
            row["markPrice"] = str(mark)
        return [row]

    exchange = SimpleNamespace(
        urls={"api": {
            "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
            "fapiPrivateV2": "https://demo-fapi.binance.com/fapi/v2",
            "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3",
        }},
        options={"enableDemoTrading": True, "sandboxMode": False},
        verbose=False,
        markets_by_id={"BTCUSDT": {
            "id": "BTCUSDT", "symbol": symbol, "contract": True, "linear": True,
            "inverse": False, "quote": "USDT", "settle": "USDT", "settleId": "USDT",
        }},
        fapiPrivateV3GetPositionRisk=raw_position_risk,
        fetch_position_mode=lambda **_kwargs: {"hedged": False},
        fetch_mark_price=lambda *_args, **_kwargs: {
            "markPrice": 105.0, "timestamp": int(datetime.now(UTC).timestamp() * 1000),
        },
        market=lambda _symbol: {"quote": "USDT", "settle": "USDT", "inverse": False},
        create_order=lambda *_args, **_kwargs: calls.append("create"),
        cancel_order=lambda *_args, **_kwargs: calls.append("cancel"),
    )
    bridge = SimpleNamespace(
        exchange_id="binance",
        config={"exec_base_url": "https://demo-api.binance.com", "exec_sandbox": True},
        client=lambda: exchange,
    )

    result = manage_active_positions(
        bridge, allow_mutation=allow_mutation,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{"position_key": position_key, "status": expected}]
    assert ("bracket account identity is unverified" in caplog.text) is allow_mutation
    assert calls == []
    assert job_store.load_position_state(position_key) == before_state
    assert job_store.load_bracket_recovery(task_id) == before_task
