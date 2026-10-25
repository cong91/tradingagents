"""ExchangeBridge: ccxt lifecycle, planning, risk-gated execution (multi-exchange).

Multi-exchange (L2): ``exec_exchange_id`` (or the ``exchange_id`` argument)
selects the ccxt class; credentials come from ``{ID}_API_KEY`` / ``{ID}_SECRET``
env vars only -- never from the config dict, never logged, never written to
the audit trail (safety #5). ``exec_symbol_overrides`` maps a pipeline ticker
to a full ccxt symbol and wins over the default ``base/quote`` mapping.

Gate model (FR5): the effective mode is computed AT EXECUTE TIME and is
authoritative -- live only when BOTH independent gates are open: the config
``exec_live`` key must be a literal True (armed in code; deliberately NOT
env-overridable) AND the ``TRADINGAGENTS_EXEC_LIVE`` env flag must read true
at that moment, fail-closed (anything but true/1/yes/on means dry). The env
flag's only consumer is that read, so one variable can never arm both halves.
``PlannedOrder.mode`` is informational and never grants execution rights.
Dry-run never touches the exchange (simulated fill), so it needs no confirm;
live execution requires an explicit ``confirm=True`` and never runs without
credentials. Safety #4: every exception on the execution path is audited
before it raises, and a live order that WAS placed but whose success-audit
could not be written raises a distinctive "order was placed" error so a
retry can never become a duplicate.

Risk guard (FR-K3/K4, L2): every plan and every execution is checked by a
``RiskGuard`` built from the config ``risk_*`` keys (injectable via
``risk_guard`` for tests). A halted day refuses everything with a
``PermissionError``; a plan that breaches a percentage limit comes back as a
no-order plan (the "always returns a plan" contract holds) audited as
``rejected_by_risk``; ``execute_order`` re-checks immediately before
anything is sent, so a halt raised by any process on the shared audit log
blocks both the dry and live paths.

Audit lines carry a ``phase`` field ("plan" or "execute"): the risk guard
replays only executed lines, so a planned order is never double-counted as
a filled one. Lines without ``phase`` (legacy, control events) are treated
as executed history.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from tradingagents.agents.rating import RATINGS_5_TIER
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbols import crypto_base, futures_base
from tradingagents.default_config import _BOOL_TRUE
from tradingagents.execution.audit import append_event
from tradingagents.execution.risk import RiskGuard, RiskLimits
from tradingagents.execution.sizing import PlannedOrder, SizingRules, compute
from tradingagents.portfolio import PortfolioContext, Position

logger = logging.getLogger(__name__)

_UNIFIED_FUTURES_SYMBOL = re.compile(r"^[A-Z0-9]+/[A-Z0-9]+:[A-Z0-9]+$")

_BINANCE_DEMO_HOSTS = frozenset({"demo-api.binance.com", "demo-fapi.binance.com"})
_BINANCE_PRODUCTION_HOSTS = frozenset({"api.binance.com", "fapi.binance.com"})


def _binance_profile(base_url: str | None) -> str:
    """Validate the configured Binance base URL and return its profile."""
    raw = str(base_url or "").strip()
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.path not in ("", "/"):
        raise ValueError("exec_base_url must be an HTTPS Binance API host without a path")
    if host in _BINANCE_DEMO_HOSTS:
        return "demo"
    if host in _BINANCE_PRODUCTION_HOSTS:
        return "production"
    raise ValueError(
        "exec_base_url must be https://demo-api.binance.com or https://api.binance.com"
    )


def credential_env_names(exchange_id: str, base_url: str | None = None) -> tuple[str, str]:
    """Return credential env vars coupled to the selected venue profile."""
    upper = exchange_id.strip().upper()
    if upper == "BINANCE" and _binance_profile(base_url or "https://api.binance.com") == "demo":
        return "BINANCE_DEMO_API_KEY", "BINANCE_DEMO_SECRET"
    return f"{upper}_API_KEY", f"{upper}_SECRET"


def _assert_binance_urls(client: Any, profile: str) -> None:
    expected = _BINANCE_DEMO_HOSTS if profile == "demo" else _BINANCE_PRODUCTION_HOSTS
    api = client.urls.get("api", {}) if isinstance(getattr(client, "urls", None), dict) else {}
    if isinstance(api, str):
        watched = [api]
    elif isinstance(api, dict):
        watched = [
            value for key, value in api.items()
            if key in {"public", "private", "fapiPublic", "fapiPrivate"}
        ]
    else:
        watched = []
    hosts = {urlparse(str(url)).hostname for url in watched}
    if not watched or not hosts.issubset(expected):
        raise RuntimeError(
            f"Binance {profile} profile resolved unexpected API hosts: {sorted(hosts)}"
        )


def _default_exchange_factory(
    exchange_id: str, sandbox: bool = False, *, base_url: str | None = None
) -> Any:
    """Build the configured ccxt client lazily; credentials from env only.

    ``exchange_id`` must name a ``ccxt.Exchange`` subclass (e.g. "binance",
    "okx"); anything else raises before any network call. ccxt is imported
    here, not at module level, so importing tradingagents.execution stays
    cheap and tests can inject fakes. For Binance, the safe mode is its Demo
    Trading API (``demo-api.binance.com``), not Spot Testnet. Other exchanges
    use ccxt's sandbox mode when available.
    """
    import ccxt

    klass = getattr(ccxt, exchange_id, None)
    if not (isinstance(klass, type) and issubclass(klass, ccxt.Exchange)):
        raise ValueError(
            f"exec_exchange_id {exchange_id!r} does not name a ccxt exchange class"
        )
    profile = None
    if exchange_id == "binance":
        profile = _binance_profile(base_url or "https://demo-api.binance.com")
    credential_url = base_url or (
        "https://demo-api.binance.com" if profile == "demo" else "https://api.binance.com"
    ) if exchange_id == "binance" else None
    key_env, secret_env = credential_env_names(exchange_id, credential_url)
    api_key = os.environ.get(key_env, "")
    secret = os.environ.get(secret_env, "")
    if exchange_id == "binance" and (not api_key or not secret):
        raise RuntimeError(f"missing exchange credentials: {key_env} and {secret_env}")
    client = klass({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
    })
    if exchange_id == "binance":
        if profile == "demo":
            enable_demo_trading = getattr(client, "enable_demo_trading", None)
            if not callable(enable_demo_trading):
                raise RuntimeError(
                    "ccxt Binance client does not support Demo Trading mode; "
                    "refusing to fall back to Spot Testnet"
                )
            enable_demo_trading(True)
        _assert_binance_urls(client, profile)
    elif sandbox:
        client.set_sandbox_mode(True)
    return client


class PreTradeRejection(RuntimeError):
    """A live order was refused before reaching the exchange; already audited.

    Subclasses RuntimeError so callers catching RuntimeError keep working,
    while the pass-through handler in _execute_live can tell these audited
    rejections apart from any other RuntimeError an injected exchange client
    might raise -- the latter must fall through to the generic audited
    handler instead of riding this one (safety #4).
    """


class ExchangeBridge:
    """Signal -> plan -> risk check -> approval-gated execution on one venue.

    ``config`` is shallow-merged OVER a fresh deepcopy of the process config
    (``get_config()`` already returns a copy), so a partial dict like
    ``{"exec_live": True}`` never raises KeyError and every unspecified key
    keeps its process value. The merge happens once at construction: a later
    ``set_config()`` is NOT seen by an existing bridge -- build a new one.
    ``rules`` overrides the default FR2 sizing mapping when given.
    ``exchange_id`` overrides ``exec_exchange_id``; the default factory
    resolves it to the ccxt class of the same (lowercase) name.
    ``risk_guard`` overrides the default guard built from the ``risk_*``
    config keys (tests inject a permissive or scripted guard here).
    """

    def __init__(
        self,
        config: dict | None = None,
        exchange_factory: Callable[[], Any] | None = None,
        rules: SizingRules | None = None,
        exchange_id: str | None = None,
        risk_guard: RiskGuard | None = None,
    ) -> None:
        base = get_config()
        self.config: dict = {**base, **config} if config is not None else base
        self.exchange_id = str(
            exchange_id if exchange_id is not None else self.config.get("exec_exchange_id")
        ).strip().lower()
        if not self.exchange_id:
            raise ValueError("exec_exchange_id is empty; cannot select a ccxt exchange class")
        if exchange_factory is not None:
            self._exchange_factory = exchange_factory
        else:
            def build_exchange() -> Any:
                kwargs = {"sandbox": self.config.get("exec_sandbox") is True}
                if self.exchange_id == "binance":
                    kwargs["base_url"] = self.config.get("exec_base_url")
                return _default_exchange_factory(self.exchange_id, **kwargs)

            self._exchange_factory = build_exchange
        self.rules = rules if rules is not None else SizingRules()
        self._exchange: Any | None = None
        # Futures contracts chosen by the screen for this bridge's lifetime
        # (ticker -> unified symbol, e.g. "SOL-USD" -> "SOL/USDC:USDC"). A pin
        # wins over exec_symbol_overrides and the exec_quote_currency default.
        self._pinned_futures: dict[str, str] = {}
        self.risk_guard = risk_guard if risk_guard is not None else self._build_risk_guard()

    def _build_risk_guard(self) -> RiskGuard:
        limits = RiskLimits(
            max_daily_loss_pct=float(self.config["risk_max_daily_loss_pct"]),
            max_position_pct_per_asset=float(self.config["risk_max_position_pct_per_asset"]),
            max_total_exposure_pct=float(self.config["risk_max_total_exposure_pct"]),
            max_consecutive_loss_count=int(self.config["risk_max_consecutive_loss_count"]),
            max_derivatives_leverage=float(self.config["risk_max_derivatives_leverage"]),
            max_derivatives_exposure_pct=float(self.config["risk_max_derivatives_exposure_pct"]),
        )
        return RiskGuard(limits=limits, price_of=self._price_of_ticker)

    def _price_of_ticker(self, ticker: str) -> float | None:
        """Mark a pipeline-form ticker via the exchange; None when unpriceable."""
        try:
            symbol = self.symbol_for(ticker)
        except ValueError:
            return None
        return self._fetch_price(symbol)

    def futures_settle_currencies(self) -> frozenset[str]:
        """Settle assets whose linear contracts this bridge may trade."""
        raw = self.config.get("exec_futures_settle_currencies") or ()
        if isinstance(raw, str):
            raw = raw.split(",")
        allowed = {str(item).strip().upper() for item in raw if str(item).strip()}
        quote = str(self.config.get("exec_quote_currency") or "USDT").strip().upper()
        return frozenset(allowed | {quote})

    def pin_futures_contract(self, ticker: str, symbol: str) -> None:
        """Bind ``ticker`` to one unified futures contract for later resolution."""
        symbol = str(symbol).strip().upper()
        if not _UNIFIED_FUTURES_SYMBOL.fullmatch(symbol):
            raise ValueError(f"{symbol!r} is not a unified futures symbol (BASE/QUOTE:SETTLE)")
        settle = symbol.rsplit(":", 1)[1]
        if settle not in self.futures_settle_currencies():
            raise ValueError(
                f"{symbol!r} settles in {settle}, outside exec_futures_settle_currencies"
            )
        self._pinned_futures[ticker] = symbol

    def pinned_futures_symbol(self, ticker: str) -> str | None:
        return self._pinned_futures.get(ticker)

    def futures_symbol_for(self, ticker: str, *, markets: dict[str, Any] | None = None) -> str:
        """Resolve a pipeline futures ticker against active contract metadata."""
        pinned = self._pinned_futures.get(ticker)
        overrides = self.config.get("exec_symbol_overrides")
        requested = pinned or (overrides.get(ticker) if isinstance(overrides, dict) else None)
        base = futures_base(ticker)
        if not base and not requested:
            raise ValueError(f"{ticker!r} is not a valid futures ticker")
        if requested is not None:
            requested = str(requested).strip().upper()
            if not _UNIFIED_FUTURES_SYMBOL.fullmatch(requested):
                raise ValueError(
                    f"exec_symbol_overrides[{ticker!r}] = {requested!r} is not a valid unified futures symbol"
                )
        if markets is None:
            markets = self._exchange_or_raise().load_markets()
        if not isinstance(markets, dict):
            raise ValueError("exchange returned invalid futures market metadata")
        candidates = [str(requested)] if requested else [symbol for symbol in markets if isinstance(symbol, str)]
        quote = str(self.config.get("exec_quote_currency") or "USDT").strip().upper()
        if pinned:
            # A pinned contract carries its own settle asset (validated against
            # exec_futures_settle_currencies when it was pinned).
            quote = pinned.rsplit(":", 1)[1]
        for symbol in candidates:
            if not isinstance(symbol, str) or not _UNIFIED_FUTURES_SYMBOL.fullmatch(symbol):
                continue
            market = markets.get(symbol)
            if not isinstance(market, dict):
                continue
            if requested and symbol != requested:
                continue
            if not requested and (
                str(market.get("base") or "").upper() != base
                or str(market.get("quote") or "").upper() != quote
            ):
                continue
            if requested and (
                str(market.get("quote") or "").upper() != quote
                or str(market.get("settle") or "").upper() != quote
            ):
                continue
            market_type = str(market.get("type") or "").lower()
            if market.get("active") is False or market.get("contract") is not True:
                continue
            if market_type not in {"swap", "future"}:
                continue
            settle = str(market.get("settle") or "").upper()
            if settle != quote:
                continue
            return symbol
        raise ValueError(f"no active {quote}-settled futures contract for {ticker!r}")

    def symbol_for(self, ticker: str) -> str:
        """Map a pipeline (Yahoo-form) ticker to the ccxt symbol.

        ``exec_symbol_overrides`` (config-only, keyed by pipeline ticker)
        wins first -- it is the escape hatch for pairs outside the closed
        crypto set. Otherwise the base must come from the pipeline's closed
        crypto set via ``crypto_base``, the quote from ``exec_quote_currency``.
        Anything else raises.
        """
        overrides = self.config.get("exec_symbol_overrides")
        if isinstance(overrides, dict):
            override = overrides.get(ticker)
            if override:
                return str(override)
        base = crypto_base(ticker)
        if not base:
            raise ValueError(
                f"{ticker!r} is not an executable crypto symbol; the bridge "
                "only trades the pipeline's known crypto bases (or provide "
                "an exec_symbol_overrides entry)"
            )
        quote = str(self.config.get("exec_quote_currency") or "").strip().upper()
        if not quote:
            raise ValueError("exec_quote_currency is empty; cannot build a ccxt symbol")
        return f"{base}/{quote}"

    def plan_order(
        self,
        ticker: str,
        signal: str,
        portfolio: PortfolioContext | None = None,
        *,
        price: float | None = None,
    ) -> PlannedOrder:
        """Size ``signal`` for ``ticker`` and write one audit line (FR2-4, FR7).

        Without ``price`` the plan is a network read (FR3): fetch_ticker AND
        load_markets, so the exchange's own minimum cost decides the plan.
        With ``price`` given the plan stays fully offline and uses the rules
        floor -- the live execute re-check with fresh market limits is then
        authoritative. ``mode`` on the returned plan records the gate value
        at plan time only; the audit line records confirmed=False because
        nothing has been confirmed yet.
        """
        ccxt_symbol = self.symbol_for(ticker)
        min_cost = self.rules.min_order_cost
        if price is None:
            exchange = self._exchange_or_raise()
            price = self._fetch_price(ccxt_symbol)
            market_min = self._market_min_cost(exchange.load_markets(), ccxt_symbol)
            if market_min is not None:
                min_cost = market_min
        mode = self._effective_mode()
        order = compute(
            self.rules,
            signal=signal,
            ticker=ticker,
            ccxt_symbol=ccxt_symbol,
            portfolio=portfolio,
            price=price,
            min_cost=min_cost,
        )
        order.mode = mode  # informational; the execute-time gate decides for real
        decision = self.risk_guard.check(order, portfolio, self.config["exec_log_path"])
        if decision.halted:
            self._audit(
                action="rejected_by_risk",
                order=order,
                mode=mode,
                confirmed=False,
                reason=decision.reason,
                phase="plan",
            )
            raise PermissionError(f"risk guard halted trading: {decision.reason}")
        if not decision.allowed:
            # The always-returns-a-plan contract: a limit breach comes back as
            # a no-order plan carrying the risk reason, never an exception.
            rejected = order.model_copy(
                update={"side": None, "quantity": None, "cost": None, "reason": decision.reason}
            )
            self._audit(
                action="rejected_by_risk",
                order=rejected,
                mode=mode,
                confirmed=False,
                reason=decision.reason,
                phase="plan",
            )
            return rejected
        self._audit(
            action=order.side or "no_order",
            order=order,
            mode=mode,
            confirmed=False,
            reason=order.reason,
            phase="plan",
        )
        return order

    def execute_order(
        self,
        order: PlannedOrder,
        confirm: bool = False,
        portfolio: PortfolioContext | None = None,
    ) -> str | None:
        """Execute ``order``; live requires both gates open AND confirm=True.

        Dry mode simulates the fill without calling the exchange at all and
        returns None. Live mode returns the exchange order id (None if the
        response carries none). Every refusal or failure on the live path is
        audited before it raises; nothing is ever sent on a refusal (#4).

        The risk guard re-checks immediately before anything is sent (FR-K3):
        a halt on the shared audit log -- including one appended by another
        process after the plan was made -- or a limit breach raises
        PermissionError on both the dry and live paths. Pass ``portfolio``
        to re-check the percentage limits too; without it only the
        denominator-free checks (halt, consecutive losses) run, and the dry
        path stays fully offline.

        Three refusals raise without an audit line because nothing could ever
        be sent: an order whose signal is not a tradeable 5-tier rating or
        that is flagged needs_review (safety #3 must hold on the execution
        surface too, not only at plan time), one without side/quantity, and
        a derivatives plan -- the spot surface must never route one, because
        the FR-D gates and the derivatives risk caps live only on
        ``DerivativesExecutor`` (a spot venue selected via
        ``exec_exchange_id`` is not a derivatives venue either).
        """
        if getattr(order, "market", "spot") == "derivatives":
            raise ValueError(
                f"derivatives plan for {order.ticker!r} cannot execute through "
                "the spot ExchangeBridge; use DerivativesExecutor (FR-D) -- "
                "nothing was sent"
            )
        if order.needs_review or order.signal not in RATINGS_5_TIER:
            raise ValueError(
                f"order signal {order.signal!r} is not a tradeable rating; nothing was sent"
            )
        if order.side is None or order.quantity is None:
            raise ValueError(
                f"not an executable order: side={order.side!r}, quantity={order.quantity!r}"
            )
        mode = self._effective_mode()
        # fail_closed=True: a send is being decided, so an unreadable audit
        # log (locked by a backup/AV scan, permissions) must refuse -- the
        # halt and loss-streak state it carries cannot be verified.
        decision = self.risk_guard.check(
            order, portfolio, self.config["exec_log_path"], fail_closed=True
        )
        if decision.halted or not decision.allowed:
            self._audit(
                action="rejected_by_risk",
                order=order,
                mode=mode,
                confirmed=confirm,
                reason=decision.reason,
                phase="execute",
            )
            raise PermissionError(f"risk guard refused execution: {decision.reason}")
        if mode == "dry":
            return self._execute_dry(order, confirm)
        return self._execute_live(order, confirm)

    def sync_portfolio(self) -> PortfolioContext:
        """Read the exchange balance into a broker-neutral PortfolioContext (FR6).

        Position tickers use the pipeline's Yahoo form (``BTC-USD``) so
        ``PortfolioContext.position_in`` matches the same strings the rest of
        the framework uses. Balances outside the known crypto bases are
        skipped with a log line; the quote currency becomes the cash.
        """
        exchange = self._exchange_or_raise()
        if self.exchange_id == "bybit":
            balance = exchange.fetch_balance(params={"type": "spot"})
            info = balance.get("info") if isinstance(balance, dict) else None
            info = info if isinstance(info, dict) else {}
            result = info.get("result") if isinstance(info.get("result"), dict) else {}
            records = result.get("list") if isinstance(result.get("list"), list) else []
            account_types = {
                str(row["accountType"]).strip().upper()
                for row in records
                if isinstance(row, dict) and row.get("accountType")
            }
            if result.get("accountType"):
                account_types.add(str(result["accountType"]).strip().upper())
            account_type = next(iter(account_types)) if len(account_types) == 1 else None
            if account_type != "SPOT":
                scope = account_type or ("mixed" if account_types else "unknown")
                raise RuntimeError(
                    "Bybit spot-only snapshot unavailable: "
                    f"wallet response accountType is {scope}"
                )
        else:
            balance = exchange.fetch_balance()
        quote = str(self.config.get("exec_quote_currency") or "USDT").strip().upper()
        positions: list[Position] = []
        for currency, amount in (balance.get("total") or {}).items():
            if currency == quote:
                continue
            base = crypto_base(f"{currency}-USD")
            if base is None:
                logger.info("sync_portfolio: skipping unmapped balance %r", currency)
                continue
            quantity = float(amount or 0.0)
            if quantity == 0:
                continue
            positions.append(Position(ticker=f"{base}-USD", quantity=quantity))
        cash = (balance.get("free") or {}).get(quote)
        if cash is None:
            cash = (balance.get(quote) or {}).get("free")
        return PortfolioContext(cash=cash, currency=quote, positions=positions)

    def _execute_dry(self, order: PlannedOrder, confirm: bool) -> None:
        # Simulated fill: no exchange call of any kind, so no write endpoint
        # can be reached and no confirm is needed (FR4). Planned brackets
        # ride the audit line via stop_loss/take_profit — the dry fill is the
        # rehearsal for the live bracket placement.
        self._audit(
            action=order.side or "no_order",
            order=order,
            mode="dry",
            confirmed=confirm,
            order_id=None,
            reason="dry-run: order not sent to exchange",
            phase="execute",
        )

    def _execute_live(self, order: PlannedOrder, confirm: bool) -> str | None:
        if confirm is not True:
            self._audit(
                action="execute_denied",
                order=order,
                mode="live",
                confirmed=confirm,
                reason="live execution requires confirm=True",
                phase="execute",
            )
            raise PermissionError("live execution requires confirm=True; nothing was sent")
        exchange = self._exchange_or_raise()
        api_key = getattr(exchange, "apiKey", "") or ""
        secret = getattr(exchange, "secret", "") or ""
        if not api_key or not secret:
            self._audit(
                action="execute_denied",
                order=order,
                mode="live",
                confirmed=confirm,
                reason="missing exchange API credentials",
                phase="execute",
            )
            key_env, secret_env = credential_env_names(
                self.exchange_id, self.config.get("exec_base_url")
            )
            raise RuntimeError(
                f"live execution requires {key_env} and {secret_env}; nothing was sent"
            )
        # Lazy ccxt import: only this live path needs the exception classes.
        import ccxt

        try:
            # load_markets first: ccxt's amount_to_precision requires the
            # markets loaded and raises ExchangeError("markets not loaded")
            # otherwise (verified on ccxt 4.5.84).
            markets = exchange.load_markets()
            amount = exchange.amount_to_precision(order.ccxt_symbol, order.quantity)
            fresh_price = self._fetch_price(order.ccxt_symbol)
            min_cost = self._market_min_cost(markets, order.ccxt_symbol)
            if min_cost is None:
                min_cost = self.rules.min_order_cost
            if fresh_price is None:
                self._audit(
                    action="rejected_at_execute",
                    order=order,
                    mode="live",
                    confirmed=confirm,
                    qty_precision=amount,
                    reason="fresh price unavailable at execute",
                    phase="execute",
                )
                raise PreTradeRejection(
                    f"cannot verify {order.ccxt_symbol} price at execute time; nothing was sent"
                )
            # Decide on the amount that will actually hit the wire: ccxt
            # TRUNCATES the planned quantity to the precision step, so a plan
            # that passed the minimum on the full float can truncate below it.
            sent_cost = float(amount) * fresh_price
            if sent_cost < min_cost:
                self._audit(
                    action="rejected_at_execute",
                    order=order,
                    mode="live",
                    confirmed=confirm,
                    qty_precision=amount,
                    reason="cost below exchange minimum at execute",
                    phase="execute",
                )
                raise PreTradeRejection(
                    f"{order.ccxt_symbol} cost {sent_cost:.8g} is below the exchange "
                    f"minimum {min_cost:.8g} after precision truncation; nothing was sent"
                )
            response = exchange.create_order(order.ccxt_symbol, "market", order.side, amount)
            order_id = response.get("id") if isinstance(response, dict) else None
        except PreTradeRejection:
            raise  # business rejections above are already audited
        except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
            self._audit_rejected(order, confirm, f"exchange error: {exc}")
            raise RuntimeError(f"live order on {order.ccxt_symbol} failed: {exc}") from exc
        except Exception as exc:  # safety #4: nothing escapes the path unaudited
            self._audit_rejected(order, confirm, f"unexpected error: {exc!r}")
            raise RuntimeError(f"live order on {order.ccxt_symbol} failed: {exc!r}") from exc

        try:
            self._audit(
                action=order.side or "no_order",
                order=order,
                mode="live",
                confirmed=confirm,
                order_id=order_id,
                qty_precision=amount,
                phase="execute",
            )
        except OSError as exc:
            # The order already exists on the exchange: never let this look
            # like a failed order, or a caller retry would duplicate it.
            raise RuntimeError(
                f"order {order_id!r} WAS placed on {order.ccxt_symbol} but writing "
                f"the audit log failed: {exc}"
            ) from exc
        if order.side == "buy" and (order.stop_loss is not None or order.take_profit is not None):
            self._place_bracket(order, confirm)
        return order_id

    def _place_bracket(self, order: PlannedOrder, confirm: bool) -> None:
        """Attach exchange-side protective exits after an entry fill.

        Binance spot: one OCO order list (``POST /api/v3/orderList/oco``) —
        the above leg takes profit at the target, the below leg stops out,
        and they share the same locked balance (two separate orders would
        double-lock it and refuse). The bracket lives on the venue, so the
        stop survives this process dying — the exchange enforces it, not us.
        Verified live on testnet: both legs appear as open orders with the
        entry amount. A bracket that fails to place AFTER the entry filled
        must never discard the fill or look like a failed order (safety #4):
        it is audited loudly as ``bracket_failed`` and the position stays,
        protected only by the next daily loop's exit signals.
        """
        exchange = self._exchange_or_raise()
        market_id = exchange.market_id(order.ccxt_symbol)
        # A market buy's taker fee is charged in the received asset, so the
        # filled base amount is slightly BELOW the planned quantity — the OCO
        # must sell what is actually held (verified live: selling the planned
        # quantity after a fill refuses with InsufficientFunds).
        base = order.ccxt_symbol.split("/", 1)[0]
        balance = exchange.fetch_balance()
        held = float((balance.get("free") or {}).get(base, 0) or 0)
        bracket_qty = exchange.amount_to_precision(
            order.ccxt_symbol, min(float(order.quantity), held)
        )
        if not float(bracket_qty) > 0:
            self._audit(
                action="bracket_failed",
                order=order,
                mode="live",
                confirmed=confirm,
                reason=f"no sellable {base} after the entry fill (free={held}); "
                       "bracket not placed",
                phase="execute",
            )
            return
        request: dict[str, Any] = {
            "symbol": market_id,
            "side": "SELL",
            "quantity": bracket_qty,
        }
        if order.take_profit is not None:
            request["aboveType"] = "LIMIT_MAKER"
            request["abovePrice"] = exchange.price_to_precision(
                order.ccxt_symbol, order.take_profit
            )
        if order.stop_loss is not None:
            request["belowType"] = "STOP_LOSS"
            request["belowStopPrice"] = exchange.price_to_precision(
                order.ccxt_symbol, order.stop_loss
            )
        import ccxt

        try:
            # The camelCase implicit method (ccxt generates no snake_case
            # alias for this endpoint in 4.5.x) — verified on testnet.
            place = getattr(exchange, "privatePostOrderListOco", None)
            if place is None:
                raise RuntimeError("exchange client does not expose orderList/oco")
            response = place(request)
        except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
            self._audit(
                action="bracket_failed",
                order=order,
                mode="live",
                confirmed=confirm,
                reason=f"exchange error placing protective exits: {exc}",
                phase="execute",
            )
            return
        except Exception as exc:  # noqa: BLE001 — the entry already filled
            self._audit(
                action="bracket_failed",
                order=order,
                mode="live",
                confirmed=confirm,
                reason=f"unexpected error placing protective exits: {exc!r}",
                phase="execute",
            )
            return
        bracket_id = response.get("orderListId") if isinstance(response, dict) else None
        try:
            self._audit(
                action="bracket_placed",
                order=order,
                mode="live",
                confirmed=confirm,
                order_id=str(bracket_id) if bracket_id is not None else None,
                reason="exchange-side OCO stopLoss/takeProfit attached after entry fill",
                phase="execute",
            )
        except OSError as exc:
            raise RuntimeError(
                f"bracket {bracket_id!r} WAS placed on {order.ccxt_symbol} but writing "
                f"the audit log failed: {exc}"
            ) from exc

    def _effective_mode(self) -> str:
        # ``is True`` on purpose: the config gate only opens on a real boolean
        # (what _coerce produces, or an explicit True injection). Strings like
        # "false" or "0" in the config dict must fail closed, not ride Python
        # truthiness.
        live = self.config.get("exec_live") is True and self._env_live_flag()
        return "live" if live else "dry"

    def effective_mode(self) -> str:
        """The gate-decided mode right now: ``"live"`` or ``"dry"`` (FR5).

        Public because orchestrators (the daily runner) need the same value
        the bridge will use, without reaching into private state.
        """
        return self._effective_mode()

    def audit_plan(
        self,
        order: PlannedOrder,
        *,
        action: str,
        reason: str | None = None,
        confirmed: bool = False,
        order_id: str | None = None,
        phase: str = "plan",
    ) -> None:
        """Public audit writer for orchestrators (e.g. the daily runner and
        the derivatives executor).

        Keeps their lines in the bridge's exact shape and redaction rules
        instead of hand-building dicts in another module.
        """
        self._audit(
            action=action,
            order=order,
            mode=self._effective_mode(),
            confirmed=confirmed,
            order_id=order_id,
            reason=reason,
            phase=phase,
        )

    def fetch_price(self, ticker: str) -> float | None:
        """Last price for a pipeline-form ticker via the venue; None if unavailable."""
        return self._price_of_ticker(ticker)

    def client(self) -> Any:
        """The venue client this bridge trades through (built lazily).

        Composed executors (derivatives) reuse the same client so call
        recording, rate limiting and credential redaction stay in one place.
        """
        return self._exchange_or_raise()

    @staticmethod
    def _env_live_flag() -> bool:
        """Read TRADINGAGENTS_EXEC_LIVE at call time, fail-closed.

        This read is the ONLY consumer of the flag: it is deliberately absent
        from _ENV_OVERRIDES, so the same variable can never fold into the
        config half of the gate and collapse FR5's two conditions into one.
        None/"" or anything outside true/1/yes/on keeps the bridge dry.
        """
        raw = os.environ.get("TRADINGAGENTS_EXEC_LIVE")
        if not raw:
            return False
        return raw.strip().lower() in _BOOL_TRUE

    def _exchange_or_raise(self) -> Any:
        if self._exchange is None:
            self._exchange = self._exchange_factory()
        return self._exchange

    def _fetch_price(self, symbol: str) -> float | None:
        exchange = self._exchange_or_raise()
        ticker = exchange.fetch_ticker(symbol)
        if not isinstance(ticker, dict):
            return None
        for key in ("last", "close"):
            value = ticker.get(key)
            if value:
                return float(value)
        return None

    @staticmethod
    def _market_min_cost(markets: Any, symbol: str) -> float | None:
        if not isinstance(markets, dict):
            return None
        market = markets.get(symbol) or {}
        cost = (market.get("limits") or {}).get("cost") or {}
        minimum = cost.get("min")
        return float(minimum) if minimum is not None else None

    def _audit(
        self,
        *,
        action: str,
        order: PlannedOrder,
        mode: str,
        confirmed: bool,
        order_id: str | None = None,
        qty_precision: str | None = None,
        reason: str | None = None,
        phase: str = "execute",
    ) -> None:
        """Write one audit line (FR7). The mode recorded is the effective one.

        ``phase`` separates planned from executed lines so the risk guard's
        FIFO replay never double-counts a plan that later executed.
        """
        event: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ticker": order.ticker,
            "ccxt_symbol": order.ccxt_symbol,
            "signal": order.signal,
            "action": action,
            "qty": order.quantity,
            "price_est": order.estimated_price,
            "mode": mode,
            "confirmed": confirmed,
            "order_id": order_id,
            "phase": phase,
            # The risk guard's FIFO replay needs the market to recognize a
            # derivatives sell as a short opener; leverage documents the
            # notional multiplier alongside price_est.
            "market": getattr(order, "market", "spot"),
            "leverage": getattr(order, "leverage", 1.0),
        }
        if event["market"] == "derivatives" and getattr(order, "cost", None) is not None:
            event["cost"] = order.cost
        if getattr(order, "margin_mode", None) is not None:
            event["margin_mode"] = order.margin_mode
        if getattr(order, "stop_loss", None) is not None:
            event["stop_loss"] = order.stop_loss
        if getattr(order, "take_profit", None) is not None:
            event["take_profit"] = order.take_profit
        if qty_precision is not None:
            event["qty_precision"] = qty_precision
        if reason is not None:
            # Safety #5: strip any credential that leaked into interpolated
            # exception text before it reaches the audit trail.
            event["reason"] = self._redact(reason)
        append_event(self.config["exec_log_path"], event)

    def _audit_rejected(self, order: PlannedOrder, confirm: bool, reason: str) -> None:
        """Record an exchange-side rejection of a live order (safety #4)."""
        self._audit(
            action="rejected_at_execute",
            order=order,
            mode="live",
            confirmed=confirm,
            reason=reason,
            phase="execute",
        )

    def _redact(self, text: str) -> str:
        """Replace the client's credentials with a marker, wherever they appear."""
        for secret in (getattr(self._exchange, "apiKey", ""), getattr(self._exchange, "secret", "")):
            if secret:
                text = text.replace(str(secret), "[redacted]")
        return text
