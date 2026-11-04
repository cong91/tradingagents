r"""Synthetic, read-only API for verifying the overview in a browser.

Run ``.venv\Scripts\python.exe tests\support\performance_preview.py`` from
this repository, and point the web frontend's API_INTERNAL_URL at
http://127.0.0.1:8101. No application modules or live services are used.
"""

import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

AUDIT_EVENTS = [
    {
        "timestamp": "2026-10-30T09:00:00Z",
        "ticker": "UNI",
        "action": "buy",
        "qty": 10,
        "price_est": 8.10,
        "ccxt_symbol": "UNI/USDT:USDT",
        "market": "derivatives",
        "leverage": 3,
        "cost": 27.0,
        "mode": "live",
        "order_id": "preview-uni-open",
        "phase": "execute",
    },
]

PORTFOLIO = {
    "status": "live",
    "currency": "USDT",
    "cash": 1000.0,
    "equity_est": 7750.0,
    "positions_source": "venue",
    "positions": [
        {
            "ticker": "BTC",
            "quantity": 0.1,
            "average_price": 65000.0,
            "basis_source": "fifo",
            "marked_price": 67500.0,
            "market_value": 6750.0,
            "unrealized_pnl": 250.0,
        },
    ],
    "realized_pnl": {
        "total": 0.0,
        "today": 0.0,
        "by_ticker": {},
        "closed_trades": 0,
        "unknown_basis_closes": 0,
        "consecutive_losses": 0,
    },
    "futures": {
        "equity": 5020.0,
        "wallet_balance": 5000.0,
        "available_balance": 4500.0,
        "unrealized_pnl": 20.0,
        "venue_profile": "demo",
        "equity_currency": "USDT",
        "wallet_balance_currency": "USDT",
        "available_balance_currency": "USDT",
        "unrealized_pnl_currency": "USDT",
        "account_scope": "derivatives",
        "positions": [
            {
                "symbol": "HYPE/USDT:USDT",
                "side": "long",
                "contracts": 10.0,
                "quantity": 10.0,
                "entry_price": 30.0,
                "mark_price": 31.0,
                "liquidation_price": 21.0,
                "unrealized_pnl": 10.0,
                "notional": 310.0,
                "margin": 103.33,
                "leverage": 3.0,
                "margin_mode": "isolated",
                "price_currency": "USDT",
                "notional_currency": "USDT",
                "margin_currency": "USDT",
                "unrealized_pnl_currency": "USDT",
                "opened_at": None,
                "held_hours": None,
                "funding": None,
                "funding_currency": None,
                "round_trip_fee": 0.31,
                "round_trip_fee_currency": "USDT",
            },
            {
                "symbol": "SOL/USDT:USDT",
                "side": "short",
                "contracts": 2.0,
                "quantity": -2.0,
                "entry_price": 180.0,
                "mark_price": 175.0,
                "liquidation_price": 230.0,
                "unrealized_pnl": 10.0,
                "notional": 350.0,
                "margin": 116.67,
                "leverage": 3.0,
                "margin_mode": "isolated",
                "price_currency": "USDT",
                "notional_currency": "USDT",
                "margin_currency": "USDT",
                "unrealized_pnl_currency": "USDT",
                "opened_at": None,
                "held_hours": None,
                "funding": None,
                "funding_currency": None,
                "round_trip_fee": 0.35,
                "round_trip_fee_currency": "USDT",
            },
            {
                "symbol": "XPL/USDT:USDT",
                "side": "long",
                "contracts": 100.0,
                "quantity": 100.0,
                "entry_price": 0.50,
                "mark_price": 0.50,
                "liquidation_price": 0.35,
                "unrealized_pnl": 0.0,
                "notional": 50.0,
                "margin": 25.0,
                "leverage": 2.0,
                "margin_mode": "isolated",
                "price_currency": "USDT",
                "notional_currency": "USDT",
                "margin_currency": "USDT",
                "unrealized_pnl_currency": "USDT",
                "opened_at": None,
                "held_hours": None,
                "funding": None,
                "funding_currency": None,
                "round_trip_fee": 0.05,
                "round_trip_fee_currency": "USDT",
            },
        ],
        "positions_read": {"status": "complete", "skipped": 0, "issues": []},
        "status": "ok",
        "error": None,
    },
    "halted_today": False,
    "warnings": [],
    "stale_after": 30,
}

FILL_HISTORY = {
    "status": "partial",
    "source": "venue_fills",
    "venue_profile": "demo",
    "start_at": "2026-10-27T00:00:00Z",
    "end_at": "2026-11-02T00:00:00Z",
    "observed_at": "2026-11-02T00:00:00Z",
    "coverage": "Synthetic preview: selected symbols and time window only; not full history.",
    "warnings": ["Synthetic preview data; some historical fills may be outside this scope."],
    "fills": [
        {
            "trade_id": "preview-xpl-reopen",
            "order_id": "preview-xpl-order",
            "symbol": "XPL/USDT:USDT",
            "side": "buy",
            "timestamp": "2026-11-01T11:00:00Z",
            "contracts": "100",
            "price": "0.50",
            "fee": "0.025",
            "fee_currency": "USDT",
            "realized_pnl": "0",
            "realized_pnl_currency": "USDT",
        },
        {
            "trade_id": "preview-xpl-close",
            "order_id": "preview-xpl-close-order",
            "symbol": "XPL/USDT:USDT",
            "side": "sell",
            "timestamp": "2026-11-01T10:00:00Z",
            "contracts": "100",
            "price": "0.48",
            "fee": "0.024",
            "fee_currency": "USDT",
            "realized_pnl": "-2.00",
            "realized_pnl_currency": "USDT",
        },
        {
            "trade_id": "preview-uni-close",
            "order_id": "preview-uni-close-order",
            "symbol": "UNI/USDT:USDT",
            "side": "sell",
            "timestamp": "2026-10-31T10:00:00Z",
            "contracts": "10",
            "price": "8.50",
            "fee": "0.0425",
            "fee_currency": "USDT",
            "realized_pnl": "4.00",
            "realized_pnl_currency": "USDT",
        },
    ],
}


class PreviewHandler(BaseHTTPRequestHandler):
    """Serve only fixed fixtures; unsupported methods never change state."""

    def do_GET(self):
        path = urlsplit(self.path).path
        generated_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        fixtures = {
            "/api/health": {
                "status": "ok",
                "exec_mode": "live",
                "halted_today": False,
                "active_run_id": None,
                "pending_approvals": 0,
            },
            "/api/history": {"items": [], "total": 0, "by_ticker": {}},
            "/api/audit": {
                "items": AUDIT_EVENTS,
                "page": 1,
                "page_size": 200,
                "total_matching": len(AUDIT_EVENTS),
                "total_lines": len(AUDIT_EVENTS),
                "halted_today": False,
                "halt_reason": None,
                "last_halt": None,
                "last_halt_reset": None,
                "skipped_lines": 0,
            },
            "/api/portfolio": PORTFOLIO,
            "/api/performance": {
                "summary": None,
                "open_positions": None,
                "brackets": {"placed": 0, "failed": 0},
                "recent_orders": AUDIT_EVENTS,
                "skipped_lines": 0,
            },
            "/api/performance/fills": FILL_HISTORY,
            "/api/autoloop": {
                "enabled": False,
                "interval_minutes": 1440,
                "running_cycle": False,
                "gates": {
                    "auto_confirm_config": False,
                    "auto_confirm_env": False,
                    "exec_live_config": False,
                    "exec_live_env": False,
                    "exec_derivatives_config": False,
                    "exec_derivatives_env": False,
                    "all_open": False,
                },
                "last_cycle": None,
            },
            "/api/daily": {
                "items": [],
                "page": 1,
                "page_size": 1,
                "total": 0,
                "active_job_id": None,
            },
            "/api/settings": {
                "settings": [],
                "credentials": [],
                "gates_readonly": {},
                "effective_exec_mode": "live",
            },
        }
        if path not in fixtures:
            self._send_json(404, {"detail": "Not Found"})
            return
        payload = fixtures[path]
        if path == "/api/portfolio":
            futures = payload["futures"]
            payload = {
                **payload,
                "futures": {
                    **futures,
                    "positions_read": {**futures["positions_read"], "observed_at": generated_at},
                },
            }
        self._send_json(200, {"generated_at": generated_at, **payload})

    def _method_not_allowed(self):
        self._send_json(405, {"detail": "Synthetic preview is read-only"})

    do_POST = do_PUT = do_PATCH = do_DELETE = _method_not_allowed

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    with ThreadingHTTPServer(("127.0.0.1", 8101), PreviewHandler) as server:
        print("Synthetic read-only preview listening on http://127.0.0.1:8101", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
