"""L3: the net formula, its no-double-counting guards, and fill partitioning."""

import inspect
from decimal import Decimal

import pytest

from tradingagents.outcomes.currency import CurrencyAmount
from tradingagents.outcomes.netting import (
    VENUE_SEMANTICS_BINANCE,
    VENUE_SEMANTICS_FUNDING,
    classify_exit_leg,
    net_exit,
    partition_position_fills,
    r_multiple,
)

pytestmark = pytest.mark.unit

_USDT = CurrencyAmount("USDT", Decimal("10"))
_ZERO = CurrencyAmount("USDT", Decimal("0"))


def test_net_is_realized_minus_fee_plus_signed_funding():
    net = net_exit(
        realized_pnl=_USDT,
        fee=CurrencyAmount("USDT", Decimal("0.4")),
        funding_events=[CurrencyAmount("USDT", Decimal("1.5")), CurrencyAmount("USDT", Decimal("-0.5"))],
    )
    assert net["USDT"] == Decimal("10") - Decimal("0.4") + Decimal("1.5") - Decimal("0.5")


def test_fee_in_a_different_currency_keeps_two_components():
    net = net_exit(
        realized_pnl=_USDT,
        fee=CurrencyAmount("BNB", Decimal("0.01")),
    )
    assert net["USDT"] == Decimal("10")
    assert net["BNB"] == Decimal("-0.01")


def test_unknown_fee_stays_unknown_and_poisons_only_its_bucket():
    net = net_exit(realized_pnl=_USDT, fee=CurrencyAmount("USDT", None))
    assert net["USDT"] is None


def test_venue_semantics_are_recorded_not_assumed():
    assert VENUE_SEMANTICS_BINANCE == "binance: income positive = received"
    assert {VENUE_SEMANTICS_BINANCE} == VENUE_SEMANTICS_FUNDING


def test_fee_included_refuses_a_second_fee_subtraction():
    # A venue whose realized_pnl already contains the fee must not have the
    # fee subtracted again; passing both is a double count and raises.
    with pytest.raises(ValueError, match="double count"):
        net_exit(realized_pnl=_USDT, fee=_ZERO, fee_included=True)


def test_fee_included_formula_is_realized_plus_funding():
    net = net_exit(
        realized_pnl=_USDT, fee=CurrencyAmount("USDT", None), fee_included=True,
        funding_events=[CurrencyAmount("USDT", Decimal("1"))],
    )
    assert net["USDT"] == Decimal("11")


def test_slippage_is_not_a_netting_input():
    # Slippage already moved the fill price; the net formula takes no
    # slippage parameter at all, so it can never be subtracted twice.
    parameters = inspect.signature(net_exit).parameters
    assert "slippage" not in parameters
    assert "slippage_rate" not in parameters


def test_r_multiple_uses_the_position_initial_risk():
    net = {"USDT": Decimal("20")}
    assert r_multiple(
        net,
        initial_risk=CurrencyAmount("USDT", Decimal("2")),
        quantity=Decimal("5"),
    ) == Decimal("2")


def test_r_multiple_is_none_when_risk_or_quantity_unknown():
    net = {"USDT": Decimal("20")}
    assert r_multiple(net, initial_risk=CurrencyAmount("USDT", None), quantity=Decimal("5")) is None
    assert r_multiple(net, initial_risk=_USDT, quantity=None) is None


def test_r_multiple_is_none_for_other_currency_money():
    assert r_multiple(
        {"BNB": Decimal("1")},
        initial_risk=_USDT, quantity=Decimal("1"),
    ) is None


def test_partition_fills_from_the_position_side():
    long_fills = [{"side": "buy", "q": 1}, {"side": "sell", "q": 0.5}, {"side": "sell", "q": 0.5}]
    entries, exits = partition_position_fills("long", long_fills)
    assert len(entries) == 1 and len(exits) == 2
    short_fills = [{"side": "sell", "q": 1}, {"side": "buy", "q": 1}]
    entries, exits = partition_position_fills("short", short_fills)
    assert len(entries) == 1 and len(exits) == 1


def test_partition_refuses_fill_with_unknown_side():
    entries, exits = partition_position_fills("long", [{"side": None}])
    assert entries == [] and exits == []


def test_partition_rejects_bad_side():
    with pytest.raises(ValueError):
        partition_position_fills("sideways", [{"side": "buy"}])


def test_classify_stop_target_and_scale_out_for_a_long():
    entry = Decimal("100")
    stop, target = Decimal("95"), Decimal("110")
    assert classify_exit_leg(
        exit_price=Decimal("95"), entry_price=entry, stop_price=stop,
        target_price=target, exit_quantity=Decimal("1"), remaining_quantity=Decimal("1"),
    ) == "stop"
    assert classify_exit_leg(
        exit_price=Decimal("110"), entry_price=entry, stop_price=stop,
        target_price=target, exit_quantity=Decimal("1"), remaining_quantity=Decimal("1"),
    ) == "target"
    assert classify_exit_leg(
        exit_price=Decimal("103"), entry_price=entry, stop_price=stop,
        target_price=target, exit_quantity=Decimal("0.3"), remaining_quantity=Decimal("1"),
    ) == "scale_out"


def test_unclassifiable_full_close_is_bracket_not_a_guess():
    assert classify_exit_leg(
        exit_price=Decimal("100"), entry_price=Decimal("100"), stop_price=None,
        target_price=None, exit_quantity=Decimal("1"), remaining_quantity=Decimal("1"),
    ) == "bracket"


def test_short_legs_mirror_the_long():
    entry, stop, target = Decimal("100"), Decimal("105"), Decimal("90")
    assert classify_exit_leg(
        exit_price=Decimal("105"), entry_price=entry, stop_price=stop,
        target_price=target, exit_quantity=Decimal("1"), remaining_quantity=Decimal("1"),
    ) == "stop"
    assert classify_exit_leg(
        exit_price=Decimal("90"), entry_price=entry, stop_price=stop,
        target_price=target, exit_quantity=Decimal("1"), remaining_quantity=Decimal("1"),
    ) == "target"
