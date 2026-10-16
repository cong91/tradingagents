"""Full-flow Binance Demo verification — staged, fail-fast, no production.

Layer 1 (venue, spot): real ExchangeBridge with Binance Demo + live gates armed,
  a tiny long plan with an ATR bracket -> live fill -> exchange-side bracket ->
  verify both orders exist on Demo -> close the position.

Layer 2 (venue, futures): same through DerivativesExecutor (Binance Demo USDT-M),
  leverage 2, reduceOnly bracket, long -> verify -> close.

Safety: every stage asserts the client's API hostnames are Binance Demo BEFORE
any order is sent. A production hostname aborts the run. Orders are tiny and
closed at the end of each stage.
"""

import contextlib
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()  # .env: Binance Demo credentials by default

os.environ["TRADINGAGENTS_EXEC_BASE_URL"] = os.environ.get(
    "TRADINGAGENTS_EXEC_BASE_URL", "https://demo-api.binance.com"
)
os.environ["TRADINGAGENTS_EXEC_LIVE"] = "true"
os.environ["TRADINGAGENTS_EXEC_AUTO_CONFIRM"] = "true"
os.environ["TRADINGAGENTS_EXEC_DERIVATIVES"] = "true"

import ccxt  # noqa: E402

from tradingagents.dataflows.config import get_config, set_config  # noqa: E402

set_config({
    "exec_base_url": "https://demo-api.binance.com",
    "exec_live": True,
    "exec_auto_confirm": True,
    "exec_derivatives": True,
    "exec_log_path": str(__import__("pathlib").Path("server/data/demo-flow-audit.jsonl")),
    # Demo account already has most of its equity in BTC from an earlier run.
    # This flow raises caps deliberately: it verifies execution plumbing, not risk limits.
    "risk_max_position_pct_per_asset": 90.0,
    "risk_max_daily_loss_pct": 50.0,
    "risk_max_total_exposure_pct": 100.0,
    "risk_max_derivatives_leverage": 3.0,
    "risk_max_derivatives_exposure_pct": 50.0,
    "results_dir": str(__import__("pathlib").Path("server/data/demo-results")),
})

FAILURES: list[str] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        FAILURES.append(name)


def assert_demo(client: ccxt.Exchange, label: str) -> bool:
    """Only Binance Demo API hosts are allowed before any order is sent."""
    api = client.urls.get("api", {})
    watched = {
        key: api[key] for key in ("public", "private", "fapiPublic", "fapiPrivate")
        if key in api
    }
    demo_hosts = {"demo-api.binance.com", "demo-fapi.binance.com"}
    from urllib.parse import urlparse

    hosts = {urlparse(url).hostname for url in watched.values()}
    is_demo = bool(watched) and hosts.issubset(demo_hosts)
    step(f"{label}: venue is Binance Demo", is_demo,
         ", ".join(f"{k}={v}" for k, v in watched.items())[:160])
    return is_demo


def open_bracket_orders(exchange: ccxt.Exchange, symbol: str) -> list:
    return [o for o in exchange.fetch_open_orders(symbol)
            if o.get("info", {}).get("origStopPrice")
            or o.get("stopPrice") or o.get("triggerPrice")]


# ---------------------------------------------------------------- layer 1: spot
def layer_spot() -> None:
    print("\n=== LAYER 1: spot full path (live Binance Demo, bracket) ===", flush=True)
    from tradingagents.dataflows.screening import atr_targets
    from tradingagents.execution.bridge import ExchangeBridge

    bridge = ExchangeBridge(config=get_config())
    client = bridge.client()
    client.load_markets()
    if not assert_demo(client, "spot"):
        return

    existing_orders = client.fetch_open_orders("BTC/USDT")
    pre_balance = client.fetch_balance()
    pre_held = float((pre_balance.get("free") or {}).get("BTC", 0) or 0)
    step(
        "spot: Demo account is clear before test",
        not existing_orders and pre_held < 0.00001,
        f"open_orders={len(existing_orders)} BTC={pre_held:.8f}; refusing to touch pre-existing exposure",
    )
    if existing_orders or pre_held >= 0.00001:
        return

    # plan_order's percentage caps need a real portfolio: build it from the
    # venue itself (same shape the autoloop passes to _handle_plan).
    portfolio = bridge.sync_portfolio()
    balance = client.fetch_balance()
    free = float((balance.get("free") or {}).get("USDT", 0) or 0)
    step("spot: Demo balance readable", free > 0, f"free USDT={free:.2f}")
    if free <= 0:
        return

    price = float(client.fetch_ticker("BTC/USDT")["last"])
    plan = bridge.plan_order("BTC-USD", "Buy", portfolio, price=price)
    step("spot: plan_order sizes a long", plan.side == "buy" and plan.quantity and plan.quantity > 0,
         f"side={plan.side} qty={plan.quantity} cost={plan.cost}")
    if plan.side is None:
        step("spot: plan refusal reason", False, str(plan.reason)[:160])
        return
    # Cap the entry at ~50 USDT: this is a plumbing test, not a position.
    tiny_qty = round(min(50.0 / price, plan.quantity), 6)
    plan = plan.model_copy(update={"quantity": tiny_qty, "cost": tiny_qty * price})
    today = datetime.now(timezone.utc).date().isoformat()
    stop, take = atr_targets("BTC-USD", today, "crypto")
    if stop and take:
        plan = plan.model_copy(update={"stop_loss": stop, "take_profit": take})
    step("spot: ATR bracket attached", stop is not None and take is not None,
         f"SL={stop} TP={take}" if stop and take else "ATR unavailable")

    order_id = bridge.execute_order(plan, confirm=True)
    step("spot: live fill on Binance Demo", bool(order_id), f"order_id={order_id}")

    # The preflight guarantees these are the flow's own OCO legs.
    orders = client.fetch_open_orders("BTC/USDT")
    step("spot: OCO bracket live on venue (both legs)", len(orders) >= 2,
         f"{len(orders)} open order(s): "
         + ", ".join(f"{o.get('type')}@{o.get('price') or o.get('stopPrice')}"
                     for o in orders[:4]))

    # cleanup: CANCEL the OCO first (its TP leg locks the bought BTC), then
    # flatten the freed balance.
    for order in orders:
        with contextlib.suppress(ccxt.OrderNotFound):
            client.cancel_order(order["id"], "BTC/USDT")
    held = float((client.fetch_balance().get("free") or {}).get("BTC", 0) or 0)
    if held >= 0.00001:
        client.create_order("BTC/USDT", "market", "sell",
                            client.amount_to_precision("BTC/USDT", held))
    after = float((client.fetch_balance().get("free") or {}).get("BTC", 0) or 0)
    step("spot: cleanup flattened the position",
         len(client.fetch_open_orders("BTC/USDT")) == 0,
         f"cancelled {len(orders)} OCO leg(s), residual BTC={after:.8f}")


# ------------------------------------------------------------- layer 2: futures
def layer_futures() -> None:
    """Run futures through the same URL-selected Demo credentials as spot."""
    print("\n=== LAYER 2: futures full path (Binance Demo, reduceOnly) ===", flush=True)
    from tradingagents.execution.derivatives import DerivativesExecutor

    config = {**get_config(), "exec_base_url": "https://demo-api.binance.com"}
    executor = DerivativesExecutor(config=config)
    client = executor._bridge.client()
    client.options["defaultType"] = "swap"
    try:
        client.load_markets()
    except Exception as exc:
        step("futures: demo markets load", False, f"{type(exc).__name__}: {exc}")
        return
    api = client.urls.get("api", {})
    watched = {k: api[k] for k in ("fapiPublic", "fapiPrivate") if k in api}
    demo_hosts = {"demo-api.binance.com", "demo-fapi.binance.com"}
    from urllib.parse import urlparse

    hosts = {urlparse(url).hostname for url in watched.values()}
    is_demo = bool(watched) and hosts.issubset(demo_hosts)
    step("futures: venue is Binance Demo", is_demo,
         ", ".join(f"{k}={v}" for k, v in watched.items())[:140])
    if not is_demo:
        return

    symbol = executor.symbol_for("BTC-USD")
    market_id = client.market_id(symbol)
    existing_positions = client.fetch_positions([symbol])
    open_positions = [p for p in existing_positions if abs(float(p.get("contracts") or 0)) > 0]
    existing_orders = client.fetch_open_orders(symbol)
    existing_algo = client.fapiPrivateGetOpenAlgoOrders({"symbol": market_id})
    existing_algo = existing_algo if isinstance(existing_algo, list) else existing_algo.get("orders", [])
    clean = not open_positions and not existing_orders and not existing_algo
    step(
        "futures: Demo account is clear before test",
        clean,
        f"positions={len(open_positions)} orders={len(existing_orders)} algo={len(existing_algo)}; refusing to touch pre-existing exposure",
    )
    if not clean:
        return

    balance = client.fetch_balance()
    free = float((balance.get("free") or {}).get("USDT", 0) or 0)
    step("futures: Demo balance readable", free > 0, f"free USDT={free:.2f} symbol={symbol}")
    if free <= 0:
        return

    price = float(client.fetch_ticker(symbol)["last"])
    portfolio = executor._bridge.sync_portfolio()
    stop_loss = round(price * 0.97, 2)
    take_profit = round(price * 1.03, 2)
    plan = executor.plan_order(
        "BTC-USD", "Buy", portfolio, price=price, leverage=2.0,
        stop_loss=stop_loss, take_profit=take_profit,
    )
    step("futures: plan_order sizes a leveraged long",
         plan.side == "buy" and plan.market == "derivatives" and (plan.quantity or 0) > 0,
         f"side={plan.side} qty={plan.quantity} leverage={plan.leverage}")
    if plan.side is None:
        step("futures: plan refusal reason", False, str(plan.reason)[:160])
        return
    plan = plan.model_copy(update={
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    })
    order_id = executor.execute_order(plan, confirm=True, portfolio=portfolio)
    step("futures: live leveraged fill on demo venue", bool(order_id), f"order_id={order_id}")

    positions = client.fetch_positions([symbol])
    live = [p for p in positions if abs(float(p.get("contracts") or 0)) > 0]
    step("futures: position opened on venue", len(live) == 1,
         ", ".join(f"{p['symbol']} contracts={p.get('contracts')} leverage={p.get('leverage')}"
                   for p in live) or "none")

    # Conditional (algo) legs do NOT appear in classic fetch_open_orders —
    # they rest in the Algo Order API surface (fapiPrivateGetOpenAlgoOrders,
    # a bare list). The bracket is 2 legs: STOP_MARKET below + TAKE_PROFIT
    # above, both reduceOnly.
    market_id = client.market_id(symbol)
    algo = client.fapiPrivateGetOpenAlgoOrders({"symbol": market_id})
    algo_orders = algo if isinstance(algo, list) else algo.get("orders", [])
    step("futures: reduceOnly bracket live on venue", len(algo_orders) >= 2,
         f"{len(algo_orders)} algo leg(s): "
         + ", ".join(f"{o.get('orderType')}@{o.get('triggerPrice') or o.get('stopPrice')}"
                     for o in algo_orders[:4]))

    if live:
        client.create_order(symbol, "market", "sell",
                            client.amount_to_precision(symbol, live[0]["contracts"]),
                            params={"reduceOnly": True})
    # These are the only Algo orders on the symbol because the preflight
    # refuses to run with existing positions/orders.
    for order in algo_orders:
        with contextlib.suppress(Exception):
            client.fapiPrivateDeleteAlgoOrder({"symbol": market_id,
                                               "algoId": order.get("algoId")})
    for order in client.fetch_open_orders(symbol):
        with contextlib.suppress(Exception):
            client.cancel_order(order["id"], symbol)
    positions = client.fetch_positions([symbol])
    remaining = [p for p in positions if abs(float(p.get("contracts") or 0)) > 0]
    leftover_algo = client.fapiPrivateGetOpenAlgoOrders({"symbol": market_id})
    leftover = leftover_algo if isinstance(leftover_algo, list) else leftover_algo.get("orders", [])
    flattened = len(remaining) == 0 and len(leftover) == 0
    step("futures: cleanup flattened the position", flattened,
         f"positions left={len(remaining)}, algo orders left={len(leftover)}")
    if flattened:
        from server import job_store

        state_key = f"{symbol}:buy:{order_id}"
        state_cleared = job_store.delete_position_state(state_key)
        step("futures: durable position state cleared", state_cleared, state_key)


if __name__ == "__main__":
    layer_spot()
    layer_futures()
    print("\n=== SUMMARY ===", flush=True)
    if FAILURES:
        print(f"FAILED stages: {FAILURES}", flush=True)
        sys.exit(1)
    print("all venue stages passed", flush=True)
