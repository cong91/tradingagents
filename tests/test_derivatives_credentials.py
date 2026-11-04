import pytest

from tradingagents.execution.derivatives import DerivativesExecutor
from tradingagents.execution.sizing import PlannedOrder


@pytest.mark.parametrize(
    ("base_url", "expected_env"),
    [
        ("https://demo-api.binance.com", "BINANCE_DEMO_API_KEY and BINANCE_DEMO_SECRET"),
        ("https://api.binance.com", "BINANCE_API_KEY and BINANCE_SECRET"),
    ],
)
def test_derivatives_missing_credentials_refuse_and_audit(tmp_path, base_url, expected_env):
    class EmptyCredentials:
        apiKey = ""
        secret = ""

    executor = DerivativesExecutor(
        config={"exec_base_url": base_url, "exec_log_path": str(tmp_path / "audit.jsonl")},
        exchange_factory=EmptyCredentials,
    )
    events = []
    executor._bridge.audit_plan = lambda order, **kwargs: events.append(kwargs)
    order = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Buy",
        side="buy", quantity=0.001, market="derivatives",
    )

    with pytest.raises(RuntimeError, match=expected_env):
        executor._execute_live(order, confirm=True)

    assert len(events) == 1
    assert events[0]["action"] == "derivatives_denied"
    assert events[0]["reason"] == "missing exchange API credentials"
    assert events[0]["confirmed"] is True
    assert events[0]["phase"] == "execute"
