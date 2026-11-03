"""Deterministic exchange used by the server API endpoint tests."""


class _FakeExchange:
    """Deterministic venue for the approve pipeline; no network, no ccxt."""

    apiKey = "test-key"
    secret = "test-secret"
    urls = {"api": {
        "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
        "fapiPrivateV2": "https://demo-fapi.binance.com/fapi/v2",
        "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3",
    }}
    options = {"enableDemoTrading": True, "sandboxMode": False}

    def __init__(self, *, fail_create: bool = False):
        self.fail_create = fail_create
        self.orders: list[tuple[str, str, str, str]] = []

    def fetch_ticker(self, symbol):
        return {"last": 83078.0, "close": 83078.0}

    def load_markets(self):
        return {"BTC/USDT": {"limits": {"cost": {"min": 10.0}}}}

    def fetch_balance(self, params=None):
        if params:
            return {"info": {"totalWalletBalance": "9500.0", "totalMarginBalance": "9500.0", "availableBalance": "9500.0", "totalUnrealizedProfit": "0.0", "positions": []}}
        return {"total": {"BTC": 0.001, "USDT": 9500.0}, "free": {"USDT": 9500.0}}

    def fetch_positions(self, symbols=None, params=None):
        return []

    def fapiPrivateV3GetPositionRisk(self, params):
        assert params == {}
        return []

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.8f}"

    def create_order(self, symbol, type_, side, amount):
        if self.fail_create:
            raise RuntimeError("simulated exchange outage")
        self.orders.append((symbol, type_, side, amount))
        return {"id": "7570510"}
