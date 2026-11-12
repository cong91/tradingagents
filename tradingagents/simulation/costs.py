"""Simulation cost model: fees, funding series, and a single slippage source.

One rule this module exists to enforce: slippage changes the fill price only.
Every fee is computed on the slipped price, and no component of the PnL
pipeline may subtract slippage again — the engine never receives a slippage
value to deduct, only prices already carrying it. Funding is applied between
candles that span a settlement boundary, with the venue's sign convention
recorded per series (positive amount = the position receives funding).
"""

from __future__ import annotations

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
    """A funding rate series with its venue sign semantics recorded.

    ``amounts`` maps settlement timestamps to signed quote-currency amounts for
    the position's original opened size: a positive value pays the position
    (short receiving funding on Binance), a negative charges it. After a
    scale-out, the engine prorates each amount to the remaining quantity.
    ``notional_scale`` converts feeds expressed in smaller quote units. The
    semantics string travels with the series so a report can state which
    convention produced the numbers.

    ``known_from_ms``/``known_to_ms`` optionally mark the span the venue
    history actually covers. A window with NO settlement inside that span is
    a KNOWN zero (the series is the authoritative history), while a window
    outside the span is UNKNOWN — the two must never be conflated: missing
    funding is unknown, never zero.
    """

    amounts: dict[int, float]
    venue_semantics: str
    notional_scale: float = 1.0
    known_from_ms: int | None = None
    known_to_ms: int | None = None

    def received_between(self, start_ms: int, end_ms: int) -> float | None:
        """Signed funding the position earns for settlements in [start, end).

        Returns None only when the window lies outside the recorded known
        span; a window inside the span with no settlement is a known zero.
        """
        if self.known_from_ms is not None and start_ms < self.known_from_ms:
            return None
        if self.known_to_ms is not None and end_ms > self.known_to_ms:
            return None
        events = [ts for ts in self.amounts if start_ms <= ts < end_ms]
        return sum(self.amounts[ts] for ts in events) * self.notional_scale

    def received_in_known_span(self, start_ms: int, end_ms: int) -> float:
        """Sum settlement rates covered by this series, clipping unknown edges."""
        known_start = max(
            start_ms, self.known_from_ms if self.known_from_ms is not None else start_ms,
        )
        known_end = min(
            end_ms, self.known_to_ms if self.known_to_ms is not None else end_ms,
        )
        if known_start >= known_end:
            return 0.0
        events = [ts for ts in self.amounts if known_start <= ts < known_end]
        return sum(self.amounts[ts] for ts in events) * self.notional_scale

    def covers(self, start_ms: int, end_ms: int) -> bool:
        """Whether the full half-open window lies within the known span."""
        return not (
            self.known_from_ms is not None and start_ms < self.known_from_ms
            or self.known_to_ms is not None and end_ms > self.known_to_ms
        )


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

    def apply_funding(self, series: FundingSeries, start_ms: int, end_ms: int) -> None:
        """Fold a known unit-notional funding rate into the ledger."""
        self.mark_funding_available(series)
        amount = series.received_between(start_ms, end_ms)
        if amount is None:
            self.mark_funding_uncertain(series)
            return
        self.record_funding(amount)

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
