"""Net-result formula for the outcome ledger, without double counting.

Contract (research report §5A / plan §2.2):

    net_exit = realized_pnl - fee + funding_received - funding_paid

per currency bucket. Slippage is NOT an input here at all: it already moved
the fill price, so subtracting it again would count it twice — the module
takes no slippage parameter and the tests pin that. Fee is only subtracted
when the venue's ``realized_pnl`` excludes fees; when ingestion records
``fee_included=True`` (a venue whose realized PnL already contains the fee),
the fee component must be absent and the formula reduces to
realized + funding. Funding amounts carry the venue's own sign, recorded in
``venue_semantics`` at ingest time; this module does not guess a sign.

Missing values stay ``None``: an unknown fee or unknown funding never reads
as zero.
"""

from __future__ import annotations

from decimal import Decimal

from tradingagents.outcomes.currency import CurrencyAmount, add_to_bucket

# Recorded sign contracts. Adding a venue means adding its documented
# semantics here and pinning it with a test — never assuming Binance's.
VENUE_SEMANTICS_BINANCE = "binance: income positive = received"
VENUE_SEMANTICS_FUNDING = {VENUE_SEMANTICS_BINANCE}

# The realized-PnL fee contract recorded by ingestion: the Binance USD-M
# normalizer reports realizedPnl and commission as separate fields, so the
# venue's realized value EXCLUDES the fee and the net formula may subtract
# it once. Any other recorded semantics must make consumers refuse the fee
# subtraction — the decision lives in ingestion (plan §2.2), pinned by test.
REALIZED_PNL_EXCLUDES_FEE = "binance: realized_pnl excludes commission"


def net_exit(
    *,
    realized_pnl: CurrencyAmount,
    fee: CurrencyAmount,
    funding_events: list[CurrencyAmount] | None = None,
    fee_included: bool = False,
) -> dict[str | None, Decimal | None]:
    """Per-currency net of one exit: realized - fee + signed funding.

    When ``realized_pnl.currency`` and ``fee.currency`` match, the fee is
    subtracted in that bucket; different currencies keep two separate
    components instead of inventing a conversion. Funding events carry their
    venue sign already (positive = received), so they are added as-is.
    """
    if fee_included and fee.amount is not None:
        raise ValueError(
            "fee_included=True means the venue's realized_pnl already contains "
            "the fee; subtracting fee again would double count it"
        )
    total: dict[str | None, Decimal | None] = {}
    add_to_bucket(total, realized_pnl)
    if not fee_included:
        add_to_bucket(total, CurrencyAmount(fee.currency, -fee.amount if fee.amount is not None else None))
    for event in funding_events or []:
        add_to_bucket(total, event)
    return total


def r_multiple(
    net: dict[str | None, Decimal | None],
    *,
    initial_risk: CurrencyAmount,
    quantity: Decimal | None,
) -> Decimal | None:
    """Net result in initial-risk units, or None when any input is unknown.

    ``initial_risk`` is per-unit (entry - initial stop distance) and
    ``quantity`` the position size, so the denominator is the position's full
    initial risk. A mixed-currency net or an unknown risk/quantity returns
    None instead of a made-up multiple.
    """
    if initial_risk.amount is None or initial_risk.currency is None or quantity is None:
        return None
    if quantity <= 0:
        return None
    denominator = initial_risk.amount * quantity
    if denominator == 0:
        return None
    amount = net.get(initial_risk.currency)
    if amount is None:
        # Other-currency money cannot be expressed in this R unit either.
        return None
    return amount / denominator


def partition_position_fills(
    side: str,
    fills: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Split a position's trades into entry and exit fills from its own side.

    A long opens with buys and closes with sells; a short is mirrored. The
    classification comes from the position's side, never from the venue's
    field spelling, so the role of a trade is deterministic given the side.
    Fills with a missing side stay out of both lists rather than being guessed.
    """
    if side == "long":
        entry_side, exit_side = "buy", "sell"
    elif side == "short":
        entry_side, exit_side = "sell", "buy"
    else:
        raise ValueError("side must be long or short")
    entries: list[dict] = []
    exits: list[dict] = []
    for fill in fills:
        fill_side = fill.get("side") if isinstance(fill, dict) else None
        if fill_side == entry_side:
            entries.append(fill)
        elif fill_side == exit_side:
            exits.append(fill)
    return entries, exits


def classify_exit_leg(
    *,
    exit_price: Decimal,
    entry_price: Decimal | None,
    stop_price: Decimal | None,
    target_price: Decimal | None,
    exit_quantity: Decimal,
    remaining_quantity: Decimal,
) -> str:
    """Classify one exit fill into a leg, or fall back to 'bracket'.

    Deterministic, price-based classification against the plan's own stop and
    target: at or beyond the stop -> 'stop'; at or beyond the target ->
    'target'; a partial reduction that is neither -> 'scale_out'; anything
    that cannot be placed against the plan (unknown entry, unknown stop and
    target, or a full close at an unclassifiable price) stays 'bracket', the
    honest "we know it closed, we cannot prove which leg" label.
    """
    if entry_price is None or entry_price <= 0:
        return "bracket"
    stop_leg = "stop" if stop_price is not None and stop_price > 0 else ""
    target_leg = "target" if target_price is not None and target_price > 0 else ""
    if stop_leg:
        at_or_past_stop = (
            exit_price <= stop_price if entry_price >= stop_price else exit_price >= stop_price
        )
        if at_or_past_stop:
            return stop_leg
    if target_leg:
        at_or_past_target = (
            exit_price >= target_price if target_price > entry_price else exit_price <= target_price
        )
        if at_or_past_target:
            return target_leg
    if remaining_quantity > 0 and exit_quantity < remaining_quantity:
        return "scale_out"
    return "bracket"
