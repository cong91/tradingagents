"""Portfolio futures snapshot quality and spot isolation, without external services."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server import job_store, portfolio as portfolio_mod, position_cost as position_cost_mod
from tradingagents.dataflows.config import set_config
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit


@pytest.fixture
def portfolio_setup(monkeypatch):
    config = {"exec_live": True, "exec_quote_currency": "USDT"}
    events = [
        {"timestamp": "2020-01-01T00:00:00+00:00", "ticker": "BTC-USD",
         "action": "buy", "qty": 3, "price_est": 10, "market": "spot"},
        {"timestamp": "2020-01-01T01:00:00+00:00", "ticker": "BTC-USD",
         "action": "sell", "qty": 1, "price_est": 12, "market": "spot"},
        {"timestamp": "2020-01-01T02:00:00+00:00", "ticker": "BTC-USD",
         "action": "sell", "qty": 2, "price_est": 300, "market": "derivatives"},
        {"timestamp": "2020-01-01T03:00:00+00:00", "ticker": "BTC-USD",
         "action": "buy", "qty": 2, "price_est": 100, "market": "derivatives"},
    ]
    book = SimpleNamespace(
        cash=100.0,
        currency="USDT",
        positions=[SimpleNamespace(ticker="BTC-USD", quantity=2.0)],
    )
    bridge = SimpleNamespace(
        effective_mode=lambda: "live",
        sync_portfolio=lambda: book,
        fetch_price=lambda ticker: 15.0,
    )

    def unexpected_call(*args, **kwargs):
        pytest.fail("unexpected futures read or position cost access")

    monkeypatch.setattr(portfolio_mod, "get_config", lambda: config)
    monkeypatch.setattr(portfolio_mod.audit_log, "read_events", lambda: (events, 2))
    monkeypatch.setattr(portfolio_mod, "_build_bridge", lambda received: bridge)
    monkeypatch.setattr(portfolio_mod, "attach_position_costs", unexpected_call)
    return config, bridge


@pytest.fixture
def snapshot():
    return {
        "status": "ok",
        "error": None,
        "account_scope": "derivatives",
        "equity": 9000.0,
        "wallet_balance": 8000.0,
        "positions": [{"symbol": "ETH/USDT:USDT", "side": "long", "notional": 200.0}],
    }


def _assert_live_spot(body):
    assert body["positions_source"] == "venue"
    assert body["currency"] == "USDT"
    assert body["cash"] == 100.0
    assert body["equity_est"] == 130.0
    assert body["positions"] == [{
        "ticker": "BTC-USD", "quantity": 2.0, "average_price": 10.0,
        "basis_source": "fifo", "marked_price": 15.0, "market_value": 30.0,
        "unrealized_pnl": 10.0,
    }]
    assert body["realized_pnl"] == {
        "total": 2.0, "today": 0.0, "by_ticker": {"BTC-USD": 2.0},
        "closed_trades": 1, "unknown_basis_closes": 0, "consecutive_losses": 0,
    }


def _assert_unavailable_futures(futures, status, error):
    assert futures == {
        "equity": None,
        "wallet_balance": None,
        "available_balance": None,
        "unrealized_pnl": None,
        "venue_profile": None,
        "equity_currency": None,
        "wallet_balance_currency": None,
        "available_balance_currency": None,
        "unrealized_pnl_currency": None,
        "account_scope": "derivatives",
        "positions": [],
        "positions_read": {
            "status": "unavailable", "skipped": None, "issues": [], "observed_at": None,
        },
        "status": status,
        "error": error,
    }


@pytest.mark.parametrize("quality", [
    {"status": "complete", "skipped": 0, "issues": []},
    {"status": "partial", "skipped": 1,
     "issues": [{"symbol": None, "reason": "invalid_row"}]},
    {"status": "partial", "skipped": None,
     "issues": [{"symbol": None, "reason": "read_failed"}]},
])
def test_live_snapshot_quality_and_costs_preserve_spot(
    monkeypatch, portfolio_setup, snapshot, quality,
):
    config, bridge = portfolio_setup
    snapshot["positions_read"] = quality
    calls = []

    def read(received, **_):
        calls.append(("read", received))
        return snapshot

    def attach(received, cost_config):
        calls.append(("cost", received, cost_config))
        received["positions"][0]["held_hours"] = 4.0

    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", read)
    monkeypatch.setattr(portfolio_mod, "attach_position_costs", attach)

    body = portfolio_mod.get_portfolio()

    assert calls == [("read", bridge), ("cost", snapshot, config)]
    assert body["futures"] is snapshot
    assert body["futures"]["positions_read"] == quality
    assert body["futures"]["positions"][0]["held_hours"] == 4.0
    assert body["warnings"] == ["2 unparseable audit lines were skipped"]
    _assert_live_spot(body)


@pytest.mark.parametrize("config_live", [False, True])
def test_dry_mode_uses_audit_and_reports_futures_unavailable(
    monkeypatch, portfolio_setup, config_live,
):
    config, bridge = portfolio_setup
    config["exec_live"] = config_live
    bridge.effective_mode = lambda: "dry"

    def no_futures_read(bridge, **_):
        pytest.fail("dry mode must not read futures")

    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", no_futures_read)
    if not config_live:
        def no_bridge(config):
            pytest.fail("dry configuration must not construct a bridge")

        monkeypatch.setattr(portfolio_mod, "_build_bridge", no_bridge)

    body = portfolio_mod.get_portfolio()

    assert body["positions_source"] == "audit"
    assert body["cash"] is None
    assert body["equity_est"] == 0.0
    assert body["positions"] == [{
        "ticker": "BTC-USD", "quantity": 2.0, "average_price": 10.0,
        "basis_source": "fifo", "marked_price": None, "market_value": None,
        "unrealized_pnl": None,
    }]
    assert body["realized_pnl"]["total"] == 2.0
    assert body["warnings"][0] == "2 unparseable audit lines were skipped"
    assert "dry mode: positions are reconstructed" in body["warnings"][1]
    _assert_unavailable_futures(body["futures"], "unavailable", None)


@pytest.mark.parametrize("exception", [RuntimeError("private secret"), ValueError("private secret")])
def test_snapshot_failure_is_sanitized_and_keeps_spot(monkeypatch, portfolio_setup, exception):
    def fail_read(bridge, **_):
        raise exception

    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", fail_read)

    body = portfolio_mod.get_portfolio()

    error = f"futures account read failed ({type(exception).__name__})"
    _assert_unavailable_futures(body["futures"], "error", error)
    assert body["warnings"] == ["2 unparseable audit lines were skipped", error]
    assert "secret" not in str(body)
    _assert_live_spot(body)


def test_position_cost_failure_warns_without_losing_snapshot(monkeypatch, portfolio_setup, snapshot):
    snapshot["positions_read"] = {"status": "partial", "skipped": 1, "issues": [
        {"symbol": None, "reason": "missing_symbol"},
    ]}
    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", lambda bridge, **_: snapshot)

    def fail_costs(futures, config):
        raise ValueError("private secret")

    monkeypatch.setattr(portfolio_mod, "attach_position_costs", fail_costs)

    body = portfolio_mod.get_portfolio()

    assert body["futures"] is snapshot
    assert body["futures"]["positions_read"] == snapshot["positions_read"]
    assert body["warnings"] == [
        "2 unparseable audit lines were skipped", "futures position cost unavailable (ValueError)",
    ]
    assert "secret" not in str(body)
    _assert_live_spot(body)


def test_actual_cost_enrichment_attaches_fee_without_state_or_funding_reads(
    monkeypatch, portfolio_setup, snapshot,
):
    _, bridge = portfolio_setup
    snapshot["positions_read"] = {"status": "partial", "skipped": 0, "issues": [
        {"symbol": None, "reason": "scope_unverified"},
    ]}
    snapshot["positions"][0]["notional_currency"] = "USDT"
    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", lambda bridge, **_: snapshot)
    monkeypatch.setattr(portfolio_mod, "attach_position_costs", position_cost_mod.attach_position_costs)

    class UnusedClient:
        def fetch_funding_history(self, *args, **kwargs):
            pytest.fail("display cost must not read funding history")

    bridge.client = lambda: UnusedClient()

    body = portfolio_mod.get_portfolio()

    (position,) = body["futures"]["positions"]
    assert position["opened_at"] is None
    assert position["held_hours"] is None
    assert position["funding"] is None
    assert position["round_trip_fee"] == pytest.approx(0.2)
    assert position["round_trip_fee_currency"] == "USDT"
    assert body["futures"]["positions_read"] == snapshot["positions_read"]
    assert body["warnings"] == ["2 unparseable audit lines were skipped"]
    _assert_live_spot(body)


@pytest.mark.parametrize("read_state", ["complete", "partial", "positions_error", "balance_error"])
def test_real_snapshot_reader_wired_to_fake_exchange(
    monkeypatch, portfolio_setup, read_state,
):
    config, bridge = portfolio_setup
    bridge.exchange_id = "binance"
    bridge.config = config
    calls = []

    class FakeExchange:
        urls = {"api": {
            "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
            "fapiPrivateV2": "https://demo-fapi.binance.com/fapi/v2",
            "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3",
        }}
        options = {"enableDemoTrading": True, "sandboxMode": False}
        markets_by_id = {
            market_id: [{
                "id": market_id, "symbol": symbol, "quote": "USDT", "settle": "USDT",
                "contract": True, "linear": True, "inverse": False, "contractSize": 1,
            }]
            for market_id, symbol in (
                ("ETHUSDT", "ETH/USDT:USDT"), ("BTCUSDT", "BTC/USDT:USDT"),
            )
        }

        def fetch_balance(self, params=None):
            calls.append("balance")
            if read_state == "balance_error":
                raise RuntimeError("private secret")
            return {"info": {"totalMarginBalance": "1200", "positions": []}}

        def fapiPrivateV3GetPositionRisk(self, params):
            calls.append("positions")
            assert params == {}
            if read_state == "positions_error":
                raise RuntimeError("private secret")
            rows = [{"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "2"}]
            if read_state == "partial":
                rows.append({"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": None})
            return rows

    # The real bridge caches its client; the identity-stability check inside
    # read_futures_snapshot sees a fresh instance per call as a rebind.
    fake_exchange = FakeExchange()
    bridge.client = lambda: fake_exchange
    monkeypatch.setattr(portfolio_mod, "attach_position_costs", lambda *args: None)
    # The control store is not part of this unit fixture: pin an empty
    # monitored book so the coverage cross-check is deterministic here.
    monkeypatch.setattr(job_store, "active_position_states", lambda: [])

    before = datetime.now(UTC)
    body = portfolio_mod.get_portfolio()
    after = datetime.now(UTC)

    if read_state == "balance_error":
        assert calls == ["balance"]
        error = "futures account read failed (RuntimeError)"
        _assert_unavailable_futures(body["futures"], "error", error)
        assert body["warnings"] == ["2 unparseable audit lines were skipped", error]
    else:
        assert calls == ["balance", "positions"]
        assert body["futures"]["status"] == "ok"
        assert body["futures"]["equity"] == 1200.0
        quality = body["futures"]["positions_read"]
        if read_state == "positions_error":
            assert quality["status"] == "partial"
            assert quality["skipped"] is None
            assert quality["issues"] == [{"symbol": None, "reason": "read_failed"}]
            assert quality["observed_at"] is None
        else:
            # A clean Binance V3 read is certified; the partial case keeps the
            # scope marker because a skipped row de-certifies the response.
            assert quality["status"] == ("complete" if read_state == "complete" else "partial")
            assert quality["skipped"] == (1 if read_state == "partial" else 0)
            issues = []
            if read_state == "partial":
                issues.extend([
                    {"symbol": None, "reason": "scope_unverified"},
                    {"symbol": None, "reason": "missing_size"},
                ])
            assert quality["issues"] == issues
            observed_at = datetime.fromisoformat(quality["observed_at"])
            assert observed_at.utcoffset() == UTC.utcoffset(observed_at)
            assert before <= observed_at <= after
        assert [p["symbol"] for p in body["futures"]["positions"]] == (
            [] if read_state == "positions_error" else ["ETH/USDT:USDT"]
        )
        assert body["warnings"] == ["2 unparseable audit lines were skipped"]
    assert "secret" not in str(body)
    _assert_live_spot(body)


def test_sync_mode_wraps_the_route_with_snapshot_metadata(monkeypatch, tmp_path, snapshot):
    """Coordinator mode: metadata attached, reads coalesced, atomic always false."""
    from server import snapshot_coordinator as coordinator

    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", "true")
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS", "true")
    coordinator.reset_for_tests()
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "absent.jsonl")})

    class _Client:
        apiKey = "sync-key"
        secret = "sync-secret"

    venue_reads = []

    def read_snapshot(_bridge, **_):
        venue_reads.append(1)
        return snapshot

    bound_client = _Client()
    bridge = SimpleNamespace(
        effective_mode=lambda: "live",
        client=lambda: bound_client,
        exchange_id="binance",
        config={"exec_base_url": "https://demo-api.binance.com"},
        sync_portfolio=lambda: PortfolioContext(cash=0.0, currency="USDT", positions=[]),
    )
    monkeypatch.setattr(portfolio_mod, "_build_bridge", lambda config: bridge)
    monkeypatch.setattr(portfolio_mod.audit_log, "read_events", lambda: ([], 0))
    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", read_snapshot)
    monkeypatch.setattr(portfolio_mod, "attach_position_costs", lambda futures, config: None)

    first = portfolio_mod.get_portfolio()
    second = portfolio_mod.get_portfolio()

    assert len(venue_reads) == 1  # second call served from the coordinator cache
    for body in (first, second):
        assert body["snapshot"]["atomic"] is False
        assert body["snapshot"]["freshness"] == "unknown"
        assert body["snapshot"]["status"] == "partial"
        assert body["snapshot"]["context_id"] is not None
    assert second["snapshot"]["cache"] == "hit"
    assert first["futures"] is snapshot

    monkeypatch.delenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", raising=False)
    disabled = portfolio_mod.get_portfolio()
    assert "snapshot" not in disabled
    assert len(venue_reads) == 2


def test_sync_mode_without_account_identity_returns_unknown_without_venue_reads(
    monkeypatch, tmp_path, snapshot,
):
    from server import snapshot_coordinator as coordinator

    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", "true")
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS", "true")
    coordinator.reset_for_tests()
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "absent.jsonl")})

    class _AnonymousClient:
        apiKey = ""
        secret = ""

    bridge = SimpleNamespace(
        effective_mode=lambda: "live",
        client=lambda: _AnonymousClient(),
        exchange_id="binance",
        config={"exec_base_url": "https://demo-api.binance.com"},
    )
    monkeypatch.setattr(portfolio_mod, "_build_bridge", lambda config: bridge)
    monkeypatch.setattr(portfolio_mod.audit_log, "read_events", lambda: ([], 0))

    def unexpected_read(_bridge, **_):
        pytest.fail("no venue read without a bound account identity")

    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", unexpected_read)

    body = portfolio_mod.get_portfolio()

    assert body["snapshot"]["status"] == "unavailable"
    assert body["snapshot"]["error"] == "account_identity_unavailable"
    assert body["snapshot"]["freshness"] == "unknown"
    assert body["warnings"] == ["display snapshot unavailable: account_identity_unavailable"]
    assert body["futures"]["status"] == "unavailable"


def test_coordinator_binds_context_to_the_same_bridge_across_rotation(monkeypatch):
    from server import snapshot_coordinator as coordinator

    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", "true")
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS", "true")
    coordinator.reset_for_tests()
    config = {"exec_live": True, "exec_quote_currency": "USDT"}
    clients = [SimpleNamespace(apiKey="A", secret="a"), SimpleNamespace(apiKey="B", secret="b")]
    selected = [clients[0]]
    observed = []

    def build(_config):
        bound = selected[0]
        return SimpleNamespace(
            client=lambda: bound,
            effective_mode=lambda: "live",
            exchange_id="binance",
            config={"exec_base_url": "https://demo-api.binance.com"},
            sync_portfolio=lambda: PortfolioContext(cash=1, currency="USDT", positions=[]),
        )

    def read(bridge, **_):
        observed.append(bridge.client())
        return {"status": "ok", "positions": [], "account_scope": "derivatives"}

    monkeypatch.setattr(portfolio_mod, "get_config", lambda: config)
    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    monkeypatch.setattr(portfolio_mod.audit_log, "read_events", lambda: ([], 0))
    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", read)
    monkeypatch.setattr(portfolio_mod, "attach_position_costs", lambda *args: None)
    first = portfolio_mod.get_portfolio()
    selected[0] = clients[1]
    second = portfolio_mod.get_portfolio()
    clients[1].secret = "rotated"
    rotated = portfolio_mod.get_portfolio()
    assert observed == [clients[0], clients[1], clients[1]]
    assert len({first["snapshot"]["context_id"], second["snapshot"]["context_id"],
                rotated["snapshot"]["context_id"]}) == 3
    coordinator.reset_for_tests()


def test_spot_and_audit_failures_do_not_hide_futures(monkeypatch):
    config = {"exec_live": True, "exec_quote_currency": "USDT"}
    bridge = SimpleNamespace(
        effective_mode=lambda: "live",
        sync_portfolio=lambda: (_ for _ in ()).throw(RuntimeError("private")),
    )
    monkeypatch.setattr(portfolio_mod.audit_log, "read_events", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", lambda bridge, **_: {
        "status": "ok", "positions": [{"symbol": "BTC/USDT:USDT"}],
    })
    monkeypatch.setattr(portfolio_mod, "attach_position_costs", lambda *args: None)
    body = portfolio_mod._get_portfolio(config, bridge=bridge)
    assert body["positions_source"] == "unavailable"
    assert body["cash"] is None and body["equity_est"] is None
    assert body["realized_pnl"] is None and body["halted_today"] is None
    assert body["futures"]["positions"][0]["symbol"] == "BTC/USDT:USDT"
    assert "private" not in str(body)


def test_spot_rate_limit_defers_futures_and_starts_shared_cooldown(monkeypatch):
    from server import performance_history_cache as history_cache

    monkeypatch.setattr(history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    error = RuntimeError("HTTP 429")
    error.response = SimpleNamespace(status_code=429, headers={"Retry-After": "1200"})
    bridge = SimpleNamespace(
        effective_mode=lambda: "live",
        sync_portfolio=lambda: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(portfolio_mod.audit_log, "read_events", lambda: ([], 0))
    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", lambda bridge, **_: pytest.fail("rate limited"))
    body = portfolio_mod._get_portfolio({"exec_live": True}, bridge=bridge)
    assert history_cache.is_rate_limited()
    assert body["futures"]["status"] == "unavailable"
    assert body["positions_source"] == "unavailable"
