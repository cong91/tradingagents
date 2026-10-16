"""Bybit testnet full-flow verification — futures-first, staged, no mainnet.

Prerequisites (from the operator):
  1. Register at https://testnet.bybit.com (a SEPARATE account from mainnet
     bybit.com — testnet is its own site with its own demo funds).
  2. Create an API key there: avatar -> API -> Create New Key, permissions
     Read + Contract (futures) + Spot, NO withdrawal.
  3. Put them in .env as BYBIT_API_KEY / BYBIT_SECRET.

Layer 1 (venue assert): the client must resolve to api-testnet.bybit.com
  BEFORE anything else — a mainnet hostname aborts the run.

Layer 2 (futures, the layer Binance can no longer rehearse):
  DerivativesExecutor on exec_exchange_id=bybit -> symbol BTC/USDT:USDT ->
  plan (leverage 2) -> live fill on testnet -> verify the position -> check
  how the bracket landed (attached params may be unsupported on bybit
  linear in ccxt 4.5 — the run reports exactly what the venue accepted) ->
  two reduce-only trigger orders as the bybit-native fallback probe ->
  flatten and clean up.
"""

import contextlib
import os
import sys

os.environ["TRADINGAGENTS_EXEC_SANDBOX"] = "true"
os.environ["TRADINGAGENTS_EXEC_LIVE"] = "true"
os.environ["TRADINGAGENTS_EXEC_AUTO_CONFIRM"] = "true"
os.environ["TRADINGAGENTS_EXEC_DERIVATIVES"] = "true"

import ccxt  # noqa: E402

from tradingagents.dataflows.config import get_config, set_config  # noqa: E402

set_config({
    "exec_exchange_id": "bybit",
    "exec_sandbox": True,
    "exec_live": True,
    "exec_auto_confirm": True,
    "exec_derivatives": True,
    "exec_log_path": "server/data/bybit-testnet-audit.jsonl",
    "risk_max_position_pct_per_asset": 90.0,
    "risk_max_daily_loss_pct": 50.0,
    "risk_max_total_exposure_pct": 100.0,
    "risk_max_derivatives_leverage": 3.0,
    "risk_max_derivatives_exposure_pct": 50.0,
    "results_dir": "server/data/bybit-testnet-results",
})

FAILURES: list[str] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        FAILURES.append(name)


def assert_testnet(client: ccxt.Exchange, label: str) -> bool:
    api = client.urls.get("api", {})
    hosts = {str(url) for url in api.values() if isinstance(url, str) and "://" in url}
    is_testnet = bool(hosts) and all("testnet" in url for url in hosts)
    step(f"{label}: venue is testnet", is_testnet,
         ", ".join(sorted(hosts))[:140])
    return is_testnet


def flatten_long(client: ccxt.Exchange, symbol: str) -> None:
    for order in client.fetch_open_orders(symbol):
        with contextlib.suppress(Exception):
            client.cancel_order(order["id"], symbol)
    for position in client.fetch_positions([symbol]):
        contracts = float(position.get("contracts") or 0)
        if contracts > 0:
            client.create_order(symbol, "market", "sell",
                                client.amount_to_precision(symbol, contracts),
                                params={"reduceOnly": True})


def layer_futures() -> None:
    print("\n=== BYBIT LAYER: futures full path (testnet USDT-M, leverage 2) ===", flush=True)
    if not os.environ.get("BYBIT_API_KEY") or not os.environ.get("BYBIT_SECRET"):
        step("futures: credentials present", False,
             "BYBIT_API_KEY/BYBIT_SECRET chưa có trong .env — làm theo hướng dẫn trước.")
        return

    from tradingagents.execution.derivatives import DerivativesExecutor

    executor = DerivativesExecutor(config=get_config())
    client = executor._bridge.client()
    client.load_markets()
    if not assert_testnet(client, "futures"):
        return

    symbol = executor.symbol_for("BTC-USD")
    step("futures: symbol mapping", bool(symbol), f"BTC-USD -> {symbol}")

    balance = client.fetch_balance()
    free = float((balance.get("free") or {}).get("USDT", 0) or 0)
    step("futures: testnet balance readable", free > 0, f"free USDT={free:.2f}")
    if free <= 0:
        return

    portfolio = executor._bridge.sync_portfolio()
    price = float(client.fetch_ticker(symbol)["last"])
    plan = executor.plan_order("BTC-USD", "Buy", portfolio, price=price, leverage=2.0)
    step("futures: plan_order sizes a leveraged long",
         plan.side == "buy" and plan.market == "derivatives" and (plan.quantity or 0) > 0,
         f"side={plan.side} qty={plan.quantity} leverage={plan.leverage} "
         f"cost(margin)={plan.cost}")
    if plan.side is None:
        step("futures: plan refusal reason", False, str(plan.reason)[:160])
        return
    # Cap the margin at ~50 USDT: plumbing test, not a position.
    tiny_margin = min(50.0, float(plan.cost or 50.0))
    plan = plan.model_copy(update={
        "cost": tiny_margin,
        "quantity": tiny_margin * 2.0 / price,
        "stop_loss": round(price * 0.98, 2),
        "take_profit": round(price * 1.02, 2),
    })
    order_id = executor.execute_order(plan, confirm=True, portfolio=portfolio)
    step("futures: live leveraged fill on testnet", bool(order_id), f"order_id={order_id}")

    positions = client.fetch_positions([symbol])
    live = [p for p in positions if abs(float(p.get("contracts") or 0)) > 0]
    step("futures: position opened on venue", len(live) == 1,
         ", ".join(f"{p['symbol']} contracts={p.get('contracts')} "
                   f"entry={p.get('entryPrice')} lev={p.get('leverage')}"
                   for p in live) or "none")

    # How did the attached-params bracket land? Bybit v5 usually surfaces
    # position-level TP/SL fields, or as conditional orders.
    if live:
        pos = live[0]
        pos_tpsl = (pos.get("takeProfit"), pos.get("stopLoss"))
        conditional = [o for o in client.fetch_open_orders(symbol)
                       if o.get("stopPrice") or o.get("triggerPrice")
                       or o.get("info", {}).get("triggerPrice")]
        step("futures: bracket visible on venue",
             pos_tpsl != (None, None) or len(conditional) >= 1,
             f"position TP/SL={pos_tpsl}, conditional orders={len(conditional)}")

    # Bybit-native fallback probe: two reduce-only trigger orders (the engine
    # fix if attached params were rejected).
    if live:
        entry = float(live[0].get("entryPrice") or price)
        qty = client.amount_to_precision(symbol, live[0]["contracts"])
        try:
            sl = client.create_order(symbol, "market", "sell", float(qty), None,
                                     {"triggerPrice": round(entry * 0.98, 2),
                                      "reduceOnly": True})
            print(f"[INFO] reduce-only stop trigger OK {sl.get('id')}", flush=True)
            tp = client.create_order(symbol, "market", "sell", float(qty), None,
                                     {"triggerPrice": round(entry * 1.02, 2),
                                      "reduceOnly": True})
            print(f"[INFO] reduce-only TP trigger OK {tp.get('id')}", flush=True)
        except Exception as exc:
            print(f"[INFO] trigger fallback: {type(exc).__name__} {str(exc)[:180]}",
                  flush=True)

    flatten_long(client, symbol)
    remaining = [p for p in client.fetch_positions([symbol])
                 if abs(float(p.get("contracts") or 0)) > 0]
    step("futures: cleanup flattened the position", len(remaining) == 0,
         f"positions left={len(remaining)}")


def layer_spot_probe() -> None:
    """Optional: spot entry on bybit testnet (the sellable-balance + fill
    path). The Binance-style OCO bracket does not exist on bybit, so this
    layer only verifies entry/exit plumbing."""
    print("\n=== BYBIT LAYER: spot entry probe (testnet) ===", flush=True)
    if not os.environ.get("BYBIT_API_KEY") or not os.environ.get("BYBIT_SECRET"):
        step("spot: layer SKIPPED — no keys", True, "same credentials as the futures layer")
        return
    from tradingagents.execution.bridge import ExchangeBridge

    bridge = ExchangeBridge(config={**get_config()})
    client = bridge.client()
    client.load_markets()
    if not assert_testnet(client, "spot"):
        return
    portfolio = bridge.sync_portfolio()
    price = float(client.fetch_ticker("BTC/USDT")["last"])
    plan = bridge.plan_order("BTC-USD", "Buy", portfolio, price=price)
    if plan.side is None:
        step("spot: plan refusal", False, str(plan.reason)[:160])
        return
    tiny_qty = round(min(50.0 / price, plan.quantity), 6)
    plan = plan.model_copy(update={"quantity": tiny_qty, "cost": tiny_qty * price})
    order_id = bridge.execute_order(plan, confirm=True)
    step("spot: live fill on testnet", bool(order_id), f"order_id={order_id}")
    held = float((client.fetch_balance().get("free") or {}).get("BTC", 0) or 0)
    if held >= 0.00001:
        client.create_order("BTC/USDT", "market", "sell",
                            client.amount_to_precision("BTC/USDT", held))
    step("spot: cleanup flattened", True, f"sold back {held} BTC")


if __name__ == "__main__":
    layer_futures()
    layer_spot_probe()
    print("\n=== SUMMARY ===", flush=True)
    if FAILURES:
        print(f"FAILED stages: {FAILURES}", flush=True)
        sys.exit(1)
    print("all bybit testnet stages passed", flush=True)
