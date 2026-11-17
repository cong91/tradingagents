"""Rejected Binance rows must not leave unified position identities actionable."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from server import binance_futures_positions
from tests.test_futures_scope_evidence import _Bridge, _client, _market, _row
from tradingagents.execution import futures_identity, futures_protection
from tradingagents.execution.brackets import BracketManager
from tradingagents.execution.exchange_client import venue_key
from tradingagents.execution.futures_account import observe_futures_positions

pytestmark = pytest.mark.unit

BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
CONFIG = {"risk_derivatives_profit_protection_enabled": True}


def _state(symbol):
    return {
        "position_key": f"{symbol}:buy:entry-1",
        "snapshot": {"side": "long"},
        "bracket_task_id": "bracket-1",
    }


def _manager_setup(monkeypatch, client, states):
    monkeypatch.setattr(futures_protection.job_store, "active_position_states", lambda: states)
    monkeypatch.setattr(client, "fetch_position_mode", lambda **kwargs: {"hedged": False})
    monkeypatch.setattr(futures_protection, "_fresh_mark", lambda *args: (100.0, None))


@pytest.mark.parametrize(("valid_side", "invalid"), [
    ("BOTH", {"positionSide": "INVALID"}),
    ("BOTH", {"positionSide": None}),
    ("BOTH", {"positionSide": "long"}),
    ("LONG", {"positionSide": "SHORT", "positionAmt": "NaN"}),
    ("LONG", {"positionSide": "SHORT", "marginAsset": "BTC"}),
    ("LONG", {"positionSide": "SHORT", "positionAmt": "0", "notional": "NaN"}),
])
def test_rejected_row_marks_same_unified_market_unknown(monkeypatch, valid_side, invalid):
    client, _ = _client(monkeypatch, [_row(positionSide=valid_side), _row(**invalid)])

    observation = observe_futures_positions(_Bridge(client))

    assert [position.symbol for position in observation.positions] == [BTC]
    assert observation.scope_complete is False
    assert observation.unknown_scope is False
    assert observation.unknown_symbols == {BTC}


def test_rejected_same_market_never_reaches_protection_mutation(monkeypatch):
    client, _ = _client(monkeypatch, [_row(), _row(positionSide="INVALID")])
    state = _state(BTC)
    _manager_setup(monkeypatch, client, [state])
    manage = Mock(return_value={"position_key": state["position_key"], "status": "mutated"})
    monkeypatch.setattr(futures_protection, "_manage_one", manage)

    result = futures_protection.manage_active_positions(
        _Bridge(client), allow_mutation=True, config=CONFIG,
    )

    assert result == [{"position_key": state["position_key"], "status": "position_unknown"}]
    manage.assert_not_called()


def test_refresh_rechecks_rejected_same_market_before_mutation(monkeypatch):
    client, _ = _client(monkeypatch, [])
    reads = iter([[_row()], [_row(), _row(positionSide="INVALID")]])
    monkeypatch.setattr(client, "fapiPrivateV3GetPositionRisk", lambda params: next(reads))
    state = _state(BTC)
    _manager_setup(monkeypatch, client, [state])
    guard = SimpleNamespace(check=Mock())
    monkeypatch.setattr(futures_protection, "operation_lock", lambda symbol: nullcontext(guard))
    monkeypatch.setattr(futures_protection.job_store, "acquire_position_monitor_lease", lambda key: "lease")
    monkeypatch.setattr(futures_protection.job_store, "load_position_state", lambda key: state)
    release = Mock()
    monkeypatch.setattr(futures_protection.job_store, "release_position_monitor_lease", release)
    advance = Mock(return_value={"position_key": state["position_key"], "status": "mutated"})
    monkeypatch.setattr(futures_protection, "_manage_one_with_lease", advance)
    monkeypatch.setattr(futures_protection.job_store, "active_bracket_recoveries", lambda: [])

    result = futures_protection.manage_active_positions(
        _Bridge(client), allow_mutation=True, config=CONFIG,
    )

    assert result == [{"position_key": state["position_key"], "status": "position_unknown"}]
    advance.assert_not_called()
    release.assert_called_once_with(state["position_key"], "lease")


def test_unrelated_market_only_proceeds_if_rejected_identity_is_resolved(monkeypatch):
    client, _ = _client(
        monkeypatch,
        [_row(), _row(positionSide="INVALID"), _row(symbol="ETHUSDT")],
        markets=[_market(), _market("ETHUSDT", ETH)],
    )
    bridge = _Bridge(client)
    observation = observe_futures_positions(bridge)
    states = [_state(BTC), _state(ETH)]
    _manager_setup(monkeypatch, client, states)
    manage = Mock(side_effect=lambda bridge, state, *args: {
        "position_key": state["position_key"], "status": "managed",
    })
    monkeypatch.setattr(futures_protection, "_manage_one", manage)

    results = futures_protection.manage_active_positions(bridge, allow_mutation=True, config=CONFIG)

    assert observation.unknown_scope is False
    assert observation.unknown_symbols == {BTC}
    assert observation.scope_complete is False
    assert results[0] == {"position_key": states[0]["position_key"], "status": "position_unknown"}
    assert results[1] == {"position_key": states[1]["position_key"], "status": "managed"}
    assert [call.args[1]["position_key"] for call in manage.call_args_list] == [states[1]["position_key"]]


@pytest.mark.parametrize("mapping", ["ambiguous", "missing", "whitespace"])
def test_unresolvable_rejected_identity_blocks_unrelated_management(monkeypatch, mapping):
    raw_symbol = {"ambiguous": "BTCUSDT", "missing": "XYZUSDT", "whitespace": " BTCUSDT "}[mapping]
    client, _ = _client(
        monkeypatch,
        [_row(symbol=raw_symbol, positionSide="INVALID"), _row(symbol="ETHUSDT")],
        markets=[_market(), _market("ETHUSDT", ETH)],
    )
    if mapping == "ambiguous":
        client.markets_by_id["BTCUSDT"].append({**_market(), "symbol": ETH})
    bridge = _Bridge(client)
    observation = observe_futures_positions(bridge)
    state = _state(ETH)
    _manager_setup(monkeypatch, client, [state])
    manage = Mock(return_value={"position_key": state["position_key"], "status": "mutated"})
    monkeypatch.setattr(futures_protection, "_manage_one", manage)

    result = futures_protection.manage_active_positions(bridge, allow_mutation=True, config=CONFIG)

    assert observation.scope_complete is False
    assert observation.unknown_scope is True
    assert result == [{"position_key": state["position_key"], "status": "position_unknown"}]
    manage.assert_not_called()


@pytest.mark.parametrize("fault", [
    "empty_symbol", "symbol_whitespace", "quote_whitespace", "settle_whitespace",
    "settle_id_whitespace", "symbol_internal_whitespace", "quote_internal_whitespace",
    "nonunified_symbol", "missing_settlement_prefix", "reverse_mismatch", "duplicate_unified",
])
def test_unverifiable_market_metadata_blocks_unrelated_management(monkeypatch, fault):
    client, _ = _client(
        monkeypatch, [_row(positionSide="INVALID"), _row(symbol="ETHUSDT")],
        markets=[_market(), _market("ETHUSDT", ETH)],
    )
    market = client.markets_by_id["BTCUSDT"][0]
    if fault == "empty_symbol":
        market["symbol"] = ""
    elif fault == "symbol_whitespace":
        market["symbol"] = f" {BTC} "
    elif fault == "quote_whitespace":
        market["quote"] = " USDT "
    elif fault == "settle_whitespace":
        market["settle"] = " USDT "
    elif fault == "settle_id_whitespace":
        market["settleId"] = " USDT "
    elif fault == "symbol_internal_whitespace":
        market["symbol"] = "BTC/USDT :USDT"
    elif fault == "quote_internal_whitespace":
        market["quote"] = "US DT"
    elif fault == "nonunified_symbol":
        market["symbol"] = "BTCUSDT"
    elif fault == "missing_settlement_prefix":
        market["symbol"] = "BTC/USDT:-250101"
        market["future"] = True
    elif fault == "reverse_mismatch":
        client.markets[BTC]["id"] = "OTHERID"
    else:
        client.markets_by_id["OTHERID"] = [{**_market(), "id": "OTHERID"}]
    if fault in {"symbol_internal_whitespace", "nonunified_symbol", "missing_settlement_prefix"}:
        reverse = client.markets.pop(BTC)
        reverse["symbol"] = market["symbol"]
        client.markets[market["symbol"]] = reverse
    elif fault == "quote_internal_whitespace":
        client.markets[BTC]["quote"] = market["quote"]

    bridge = _Bridge(client)
    observation = observe_futures_positions(bridge)
    state = _state(ETH)
    _manager_setup(monkeypatch, client, [state])
    manage = Mock()
    monkeypatch.setattr(futures_protection, "_manage_one", manage)

    assert observation.unknown_scope is True
    assert observation.scope_complete is False
    assert futures_protection.manage_active_positions(
        bridge, allow_mutation=True, config=CONFIG,
    ) == [{"position_key": state["position_key"], "status": "position_unknown"}]
    manage.assert_not_called()


@pytest.mark.parametrize("cursor", ["nextPageCursor", "nextPageToken", "nextCursor"])
def test_pagination_cursor_blocks_unrelated_management(monkeypatch, cursor):
    client, _ = _client(
        monkeypatch, [_row(positionSide="INVALID", **{cursor: "token"}), _row(symbol="ETHUSDT")],
        markets=[_market(), _market("ETHUSDT", ETH)],
    )
    bridge = _Bridge(client)
    observation = observe_futures_positions(bridge)
    state = _state(ETH)
    _manager_setup(monkeypatch, client, [state])
    manage = Mock()
    monkeypatch.setattr(futures_protection, "_manage_one", manage)

    assert observation.unknown_scope is True
    assert observation.scope_complete is False
    assert futures_protection.manage_active_positions(
        bridge, allow_mutation=True, config=CONFIG,
    ) == [{"position_key": state["position_key"], "status": "position_unknown"}]
    manage.assert_not_called()


@pytest.mark.parametrize("change", ["apiKey", "secret", "base_url", "client", "route"])
def test_client_or_credentials_changed_during_read_blocks_management(monkeypatch, change):
    client, _ = _client(
        monkeypatch, [_row(positionSide="INVALID"), _row(symbol="ETHUSDT")],
        markets=[_market(), _market("ETHUSDT", ETH)],
    )
    client.apiKey, client.secret = "initial-key", "initial-secret"
    bridge = _Bridge(client)
    original_read = client.fapiPrivateV3GetPositionRisk

    def change_during_read(params):
        rows = original_read(params)
        if change in {"apiKey", "secret"}:
            setattr(client, change, "changed")
        elif change == "base_url":
            bridge.config["exec_base_url"] = "https://api.binance.com"
        elif change == "client":
            bridge.exchange = object()
        else:
            client.urls["api"]["fapiPrivateV3"] = "https://fapi.binance.com/fapi/v3"
        return rows

    monkeypatch.setattr(client, "fapiPrivateV3GetPositionRisk", change_during_read)

    observation = observe_futures_positions(bridge)

    assert observation.positions == ()
    assert observation.unknown_scope is True
    assert observation.scope_complete is False


def test_market_index_without_reverse_metadata_can_resolve_rejected_symbol(monkeypatch):
    client, _ = _client(monkeypatch, [_row(positionSide="INVALID")])
    del client.markets[BTC]

    observation = observe_futures_positions(_Bridge(client))

    assert observation.unknown_scope is False
    assert observation.unknown_symbols == {BTC}
    assert observation.scope_complete is False


def test_rejected_mapping_uses_isolated_reader_metadata(monkeypatch):
    client, _ = _client(monkeypatch, [_row(positionSide="INVALID")])
    reader, _ = _client(monkeypatch, [_row(positionSide="INVALID")])
    reader.markets_by_id = {}
    monkeypatch.setattr(reader, "load_markets", lambda: reader.set_markets([_market()]))
    monkeypatch.setattr(binance_futures_positions, "isolated_client", lambda _: reader)

    observation = observe_futures_positions(_Bridge(client))

    assert observation.unknown_scope is False
    assert observation.unknown_symbols == {BTC}
    assert observation.scope_complete is False


def test_fresh_reader_metadata_normalizes_position_quantity(monkeypatch):
    client, _ = _client(monkeypatch, [])
    reader, _ = _client(monkeypatch, [_row()], markets=[_market(contract_size=0.01)])
    reader.markets_by_id = {}
    monkeypatch.setattr(reader, "load_markets", lambda: reader.set_markets([_market(contract_size=0.01)]))
    monkeypatch.setattr(binance_futures_positions, "isolated_client", lambda _: reader)

    observation = observe_futures_positions(_Bridge(client))

    assert observation.scope_complete is True
    assert observation.positions[0].quantity == pytest.approx(0.01)


def test_refresh_allows_unrelated_known_market_to_proceed(monkeypatch):
    client, _ = _client(
        monkeypatch, [], markets=[_market(), _market("ETHUSDT", ETH)],
    )
    reads = iter([
        [_row(symbol="ETHUSDT")],
        [_row(positionSide="INVALID"), _row(symbol="ETHUSDT")],
    ])
    monkeypatch.setattr(client, "fapiPrivateV3GetPositionRisk", lambda params: next(reads))
    state = _state(ETH)
    _manager_setup(monkeypatch, client, [state])
    monkeypatch.setattr(futures_protection, "operation_lock", lambda symbol: nullcontext(SimpleNamespace(check=Mock())))
    monkeypatch.setattr(futures_protection.job_store, "acquire_position_monitor_lease", lambda key: "lease")
    monkeypatch.setattr(futures_protection.job_store, "load_position_state", lambda key: state)
    release = Mock()
    monkeypatch.setattr(futures_protection.job_store, "release_position_monitor_lease", release)
    monkeypatch.setattr(futures_protection.job_store, "active_bracket_recoveries", lambda: [])
    advance = Mock(return_value={"position_key": state["position_key"], "status": "managed"})
    monkeypatch.setattr(futures_protection, "_manage_one_with_lease", advance)

    assert futures_protection.manage_active_positions(
        _Bridge(client), allow_mutation=True, config=CONFIG,
    ) == [{"position_key": state["position_key"], "status": "managed"}]
    assert list(advance.call_args.args[2]) == [(ETH, "long")]
    release.assert_called_once_with(state["position_key"], "lease")


@pytest.mark.parametrize(("fault", "expected"), [
    ("identity_unknown", "account_identity_unknown"),
    ("identity_mismatch", "account_identity_mismatch"),
    ("venue_mismatch", "venue_mismatch"),
    ("bracket_mismatch", "bracket_identity_mismatch"),
    ("valid", "managed"),
])
def test_unrelated_management_preserves_protection_gates(monkeypatch, fault, expected):
    client, _ = _client(
        monkeypatch, [_row(positionSide="INVALID"), _row(symbol="ETHUSDT", entryPrice="90")],
        markets=[_market(), _market("ETHUSDT", ETH)],
    )
    bridge = _Bridge(client)
    state = _state(ETH)
    state["snapshot"]["opened_at"] = "2026-01-01T00:00:00+00:00"
    if fault != "identity_unknown":
        state.update({
            "account_identity_exchange": "binance", "account_identity_profile": "demo",
            "account_identity_type": "usds_futures", "account_identity_hash": "test-identity",
        })
    _manager_setup(monkeypatch, client, [state])
    monkeypatch.setattr(futures_protection, "operation_lock", lambda symbol: nullcontext(SimpleNamespace(check=Mock())))
    monkeypatch.setattr(futures_protection.job_store, "acquire_position_monitor_lease", lambda key: "lease")
    monkeypatch.setattr(futures_protection.job_store, "load_position_state", lambda key: state)
    monkeypatch.setattr(futures_protection.job_store, "release_position_monitor_lease", lambda *args: None)
    monkeypatch.setattr(futures_protection.job_store, "active_bracket_recoveries", lambda: [])
    task = {
        "task_id": state["bracket_task_id"], "symbol": ETH, "close_side": "sell",
        "status": "monitoring", "venue_key": venue_key(bridge), "legs": [],
    }
    if fault == "venue_mismatch":
        task["venue_key"] = "another-venue"
    elif fault == "bracket_mismatch":
        task["symbol"] = BTC
    monkeypatch.setattr(futures_protection.job_store, "load_bracket_recovery", lambda key: task)
    comparison = (
        futures_identity.IdentityComparison.MISMATCH
        if fault == "identity_mismatch" else futures_identity.IdentityComparison.MATCH
    )
    monkeypatch.setattr(futures_identity, "compare_futures_account_identity", lambda *args: comparison)
    advance = Mock(return_value={"position_key": state["position_key"], "status": "managed"})
    monkeypatch.setattr(futures_protection, "_advance_for_verified_account", advance)

    assert futures_protection.manage_active_positions(
        bridge, allow_mutation=True, config=CONFIG,
    ) == [{"position_key": state["position_key"], "status": expected}]
    assert advance.call_count == (1 if fault == "valid" else 0)


@pytest.mark.parametrize("fault", ["same_market", "unresolved", "cursor", "none"])
def test_bracket_remaining_contracts_independently_respects_scope(monkeypatch, fault):
    rejected = _row(positionSide="INVALID")
    if fault == "unresolved":
        rejected["symbol"] = "UNKNOWN"
    elif fault == "cursor":
        rejected["nextPageCursor"] = "token"
    symbol = BTC if fault == "same_market" else ETH
    client, _ = _client(
        monkeypatch, [rejected, _row(symbol="ETHUSDT")],
        markets=[_market(), _market("ETHUSDT", ETH)],
    )
    bridge = _Bridge(client)
    task = {"symbol": symbol, "close_side": "sell", "venue_key": venue_key(bridge)}

    remaining = BracketManager(bridge)._remaining_contracts(task, allow_flat=True)

    assert remaining == (1.0 if fault == "none" else None)


def test_bracket_cleanup_never_infers_flat_from_partial_scope(monkeypatch):
    client, _ = _client(monkeypatch, [_row(positionSide="INVALID")])
    bridge = _Bridge(client)
    manager = BracketManager(bridge)
    save = Mock(return_value="saved")
    monkeypatch.setattr(manager, "_save", save)
    cleanup = Mock()
    monkeypatch.setattr(manager, "_cleanup_retired", cleanup)
    winner = {"name": "stop-loss", "status": "filled", "retired_ids": ["old-stop"]}
    task = {
        "symbol": ETH, "close_side": "sell", "venue_key": venue_key(bridge),
        "legs": [winner, {"name": "take-profit", "status": "placed", "order_id": "target"}],
    }

    assert manager._cleanup(client, task, winner, allow_cancel=True) == "reconcile_incomplete"
    cleanup.assert_not_called()
    assert save.call_args.args[2] == "replacement filled; flat position is not verified"
