"""Simulation cost model: fees, funding series, and a single slippage source.

One rule this module exists to enforce: slippage changes the fill price only.
Every fee is computed on the slipped price, and no component of the PnL
pipeline may subtract slippage again. Funding is settled from explicit per-event
rates and mark prices, never inferred from candle prices or entry prices.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tradingagents.dataflows.market_data import Candle


@dataclass(frozen=True)
class CostModel:
    """Fee and slippage rates as fractions of notional.

    ``taker_rate`` prices the market-entry and stop fills; ``maker_rate``
    prices resting exit levels (targets) when the caller opts in. Defaults
    mirror the production default round-trip rate budget in
    ``default_config`` (0.1% round trip) split as 0.05% per leg.
    """

    taker_rate: float = 0.0005
    maker_rate: float = 0.0005
    slippage_rate: float = 0.0

    def entry_price(self, side: str, price: float) -> float:
        """The price a market entry actually fills at after slippage.

        A buy slips up, a sell slips down: slippage is always against the
        trader. The value is the single slipped price everything else reads.
        """
        direction = 1.0 if side == "buy" else -1.0
        return price * (1.0 + direction * self.slippage_rate)

    def exit_price(self, side: str, price: float, *, maker: bool = False) -> float:
        """The price an exit fills at after slippage (against the trader).

        Closing a long (a sell) slips down; closing a short (a buy) slips up.
        ``side`` is the position side, not the order side, matching the
        engine's vocabulary.
        """
        direction = -1.0 if side == "long" else 1.0
        return price * (1.0 + direction * self.slippage_rate)

    def fee(self, notional: float, *, maker: bool = False) -> float:
        """Fee charged on the (already slipped) notional."""
        rate = self.maker_rate if maker else self.taker_rate
        return abs(notional) * rate

    def shifted(self, *, fee_delta: float = 0.0, slippage_delta: float = 0.0) -> CostModel:
        """A copy with rates shifted for the sensitivity report."""
        return CostModel(
            taker_rate=self.taker_rate + fee_delta,
            maker_rate=self.maker_rate + fee_delta,
            slippage_rate=max(0.0, self.slippage_rate + slippage_delta),
        )


@dataclass(frozen=True)
class FundingSeries:
    """Per-settlement funding evidence for a linear contract.

    Rates are fractions of the settlement mark notional. A positive rate is
    paid by longs and received by shorts. Missing rate or mark evidence at a
    listed settlement is unknown, not zero. The explicit bounds state the
    half-open interval for which the source is complete; inside that interval,
    a covered window with no settlement is a known zero.
    """

    symbol: str | None = None
    rates: dict[int, float | None] = field(default_factory=dict)
    mark_prices: dict[int, float | None] = field(default_factory=dict)
    known_from_ms: int | None = None
    known_to_ms: int | None = None
    quote_currency: str | None = None
    contract_type: str | None = None
    quantity_unit: str | None = None
    venue_semantics: str = "positive rate paid by longs, received by shorts"

    def settlement_times(self, start_ms: int, end_ms: int) -> list[int]:
        """Covered evidence timestamps in the half-open window [start, end)."""
        if not self._valid_bounds():
            return []
        return sorted(
            timestamp for timestamp in self.rates.keys() | self.mark_prices.keys()
            if isinstance(timestamp, int) and not isinstance(timestamp, bool)
            and start_ms <= timestamp < end_ms
            and self.known_from_ms <= timestamp < self.known_to_ms
        )

    def settlement_received(
        self, timestamp_ms: int, *, quantity: float, side: str,
    ) -> float | None:
        """Signed quote-currency funding at one settlement, or None if invalid."""
        rate = self.rates.get(timestamp_ms)
        mark = self.mark_prices.get(timestamp_ms)
        if not _finite_number(rate) or not _finite_number(mark) or mark <= 0:
            return None
        if not _finite_number(quantity) or quantity < 0 or side not in ("long", "short"):
            return None
        amount = (-1.0 if side == "long" else 1.0) * quantity * mark * rate
        return amount if math.isfinite(amount) else None

    def matches_contract(self, *, symbol: str, quote_currency: str) -> bool:
        """Whether metadata identifies this exact linear base-quantity market."""
        return (
            bool(symbol)
            and bool(quote_currency)
            and self.symbol == symbol
            and self.quote_currency == quote_currency
            and self.contract_type == "linear"
            and self.quantity_unit == "base"
        )

    def _valid_bounds(self) -> bool:
        return (
            isinstance(self.known_from_ms, int)
            and not isinstance(self.known_from_ms, bool)
            and isinstance(self.known_to_ms, int)
            and not isinstance(self.known_to_ms, bool)
            and self.known_from_ms < self.known_to_ms
        )

    def _valid_timestamps(self) -> bool:
        return all(
            isinstance(timestamp, int) and not isinstance(timestamp, bool)
            for timestamp in self.rates.keys() | self.mark_prices.keys()
        )

    def covers(self, start_ms: int, end_ms: int) -> bool:
        """Whether valid explicit bounds cover the entire [start, end) window."""
        if start_ms >= end_ms:
            return True
        return (
            self._valid_bounds()
            and self._valid_timestamps()
            and start_ms >= self.known_from_ms
            and end_ms <= self.known_to_ms
        )


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


@dataclass
class CostLedger:
    """Accumulated costs of one simulated run, per quote currency label.

    ``funding_available`` stays False until the caller actually hands in a
    series: a run without funding data must report funding as unknown, never
    as a zero.
    """

    currency: str = "USDT"
    fee_paid: float = 0.0
    funding_received: float = 0.0
    funding_events: int = 0
    funding_unknown_windows: int = 0
    funding_uncertain: bool = False
    funding_available: bool = False
    funding_semantics: str | None = None
    _pending_semantics: set[str] = field(default_factory=set)

    def charge_fee(self, fee: float) -> None:
        self.fee_paid += fee

    def mark_funding_available(self, series: FundingSeries) -> None:
        self.funding_available = True
        self._pending_semantics.add(series.venue_semantics)

    def mark_funding_uncertain(self, series: FundingSeries) -> None:
        self.mark_funding_available(series)
        self.funding_unknown_windows += 1
        self.funding_uncertain = True

    def record_funding(self, amount: float) -> None:
        self.funding_received += amount
        self.funding_events += 1

    def settlement_semantics(self) -> str | None:
        """The recorded venue semantics once funding was actually applied."""
        return ", ".join(sorted(self._pending_semantics)) or None


def candle_windows(candles: list[Candle]) -> list[tuple[int, int]]:
    """The [open, next_open) time windows each candle spans, in epoch ms."""
    windows: list[tuple[int, int]] = []
    for index, candle in enumerate(candles):
        next_open = candles[index + 1].open_time if index + 1 < len(candles) else candle.close_time
        windows.append((candle.open_time, next_open))
    return windows


def funding_window_ms(interval_hours: float = 8.0) -> int:
    """Default settlement cadence in epoch ms (Binance USD-M: 8h)."""
    return int(interval_hours * 3600 * 1000)


def funding_settlements_between(
    start_ms: int,
    end_ms: int,
    *,
    interval_ms: int,
    anchor_ms: int,
) -> list[int]:
    """Settlement timestamps inside [start, end) for an anchored schedule."""
    if interval_ms <= 0:
        return []
    first = anchor_ms + ((start_ms - anchor_ms) // interval_ms + 1) * interval_ms
    return list(range(first, end_ms, interval_ms))


def utc_ms(moment: datetime) -> int:
    """Epoch ms of a tz-aware moment; naive datetimes are refused."""
    if moment.tzinfo is None:
        raise ValueError("funding timestamps must be timezone-aware")
    return int(moment.astimezone(UTC).timestamp() * 1000)
