"""L2: currency buckets never invent a conversion, a zero, or a sum."""

from decimal import Decimal

import pytest

from tradingagents.outcomes.currency import (
    CurrencyAmount,
    MixedCurrencySumError,
    add_to_bucket,
    from_json,
    single_currency_total,
    sum_components,
    to_json,
    unknown,
)

pytestmark = pytest.mark.unit


def test_bucket_keeps_currencies_separate():
    bucket = add_to_bucket({}, CurrencyAmount("USDT", Decimal("10")))
    bucket = add_to_bucket(bucket, CurrencyAmount("USDT", Decimal("2.5")))
    bucket = add_to_bucket(bucket, CurrencyAmount("BNB", Decimal("1")))
    assert bucket == {"USDT": Decimal("12.5"), "BNB": Decimal("1")}


def test_bucket_addition_preserves_digits_beyond_default_decimal_precision():
    bucket = add_to_bucket({}, CurrencyAmount("USDT", Decimal("12345678901234567890123456789.12345678")))
    add_to_bucket(bucket, CurrencyAmount("USDT", Decimal("0.00000001")))
    assert bucket["USDT"] == Decimal("12345678901234567890123456789.12345679")


def test_missing_amount_poisons_its_bucket_not_others():
    bucket = add_to_bucket({}, CurrencyAmount("USDT", Decimal("10")))
    bucket = add_to_bucket(bucket, unknown())  # no currency, no amount
    assert bucket["USDT"] == Decimal("10")
    assert bucket[None] is None


def test_known_then_unknown_currency_total_becomes_unknown():
    bucket = add_to_bucket({}, CurrencyAmount("USDT", Decimal("10")))
    bucket = add_to_bucket(bucket, CurrencyAmount("USDT", None))
    assert bucket["USDT"] is None


def test_missing_currency_label_is_its_own_bucket_not_a_guess():
    bucket = add_to_bucket({}, CurrencyAmount(None, Decimal("3")))
    assert bucket[None] == Decimal("3")


def test_mixed_currency_sum_is_refused():
    bucket = add_to_bucket({}, CurrencyAmount("USDT", Decimal("10")))
    bucket = add_to_bucket(bucket, CurrencyAmount("BNB", Decimal("1")))
    with pytest.raises(MixedCurrencySumError):
        single_currency_total(bucket, "USDT")


def test_unknown_currency_bucket_blocks_a_single_currency_total():
    bucket = add_to_bucket({}, CurrencyAmount("USDT", Decimal("10")))
    bucket = add_to_bucket(bucket, CurrencyAmount(None, Decimal("3")))
    with pytest.raises(MixedCurrencySumError):
        single_currency_total(bucket, "USDT")


def test_zero_in_another_bucket_does_not_block_the_total():
    # A recorded zero is real evidence (a fee-free fill), unlike a missing one.
    bucket = add_to_bucket({}, CurrencyAmount("USDT", Decimal("10")))
    bucket = add_to_bucket(bucket, CurrencyAmount("BNB", Decimal("0")))
    assert single_currency_total(bucket, "USDT") == Decimal("10")


def test_single_currency_total_refuses_when_other_money_exists_and_currency_absent():
    # Asking for ETH while the bucket holds USDT money is a mixed claim: the
    # honest answer is a refusal, never a silent zero.
    bucket = add_to_bucket({}, CurrencyAmount("USDT", Decimal("1")))
    with pytest.raises(MixedCurrencySumError):
        single_currency_total(bucket, "ETH")


def test_single_currency_total_returns_none_for_an_empty_bucket():
    assert single_currency_total({}, "ETH") is None


def test_sum_components_folds_per_currency_and_poisons_unknowns():
    total = sum_components([
        {"USDT": Decimal("5"), "BNB": Decimal("1")},
        {"USDT": Decimal("-2")},
        {"USDT": None},
    ])
    assert total["USDT"] is None
    assert total["BNB"] == Decimal("1")


def test_json_roundtrip_keeps_exactness_and_nulls():
    bucket = {"USDT": Decimal("12.5"), "BNB": None, None: Decimal("0.001")}
    rows = to_json(bucket)
    assert rows[-1]["currency"] is None  # the unknown-currency bucket sorts last
    rebuilt = from_json(rows)
    assert rebuilt[None] == Decimal("0.001")
    assert rebuilt["USDT"] == Decimal("12.5")
    assert rebuilt["BNB"] is None


def test_from_json_skips_malformed_rows():
    rebuilt = from_json([
        {"currency": "USDT", "amount": "1.5"}, "junk", {}, {"currency": 5, "amount": "1"},
    ])
    assert rebuilt == {"USDT": Decimal("1.5")}
    assert from_json("not a list") == {}
