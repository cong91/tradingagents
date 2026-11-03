"""Validation of read-only futures balance envelopes."""

import pytest

from server.futures_balance import sanitize_balance, validate_balance
from server.futures_snapshot import read_futures_snapshot

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("balance", [
    [], "bad", {}, {"info": []}, {"info": {}}, {"info": None},
    {"info": {"error": "private"}}, {"private": "error"}, {"total": []},
    {"equity": "NaN"}, {"equity": True},
    {"free": {}, "used": {}, "total": {}, "info": {}},
    {"free": {"USDT": None}, "used": {}, "total": {"USDT": "NaN"}},
    {"free": {"USDT": True}, "used": {}, "total": {}},
])
def test_malformed_balance_fails_without_misrepresenting_an_empty_account(balance):
    with pytest.raises(ValueError, match="invalid futures account response"):
        validate_balance(balance, "okx")

    calls = []

    class Exchange:
        def fetch_balance(self, params=None):
            calls.append("balance")
            return balance

        def fetch_positions(self, *args, **kwargs):
            pytest.fail("invalid balance must stop the positions read")

    class Bridge:
        exchange_id = "okx"
        config = {}

        def client(self):
            return Exchange()

    with pytest.raises(ValueError, match="invalid futures account response"):
        read_futures_snapshot(Bridge())
    assert calls == ["balance"]


@pytest.mark.parametrize(
    ("exchange_id", "balance"),
    [
        ("okx", {"info": {"data": ["private"]}}),
        ("okx", {"info": {"data": [{"totalEq": "100", "details": "private"}]}}),
        ("bybit", {"info": {"result": {"list": ["private"]}}}),
        ("bybit", {"info": {"result": {"list": [{"accountType": "CONTRACT", "coin": "private"}]}}}),
    ],
)
def test_malformed_vendor_balance_envelope_fails(exchange_id, balance):
    with pytest.raises(ValueError, match="invalid futures account response"):
        validate_balance(balance, exchange_id)

    calls = []

    class Exchange:
        def fetch_balance(self, params=None):
            calls.append("balance")
            return balance

        def fetch_positions(self, *args, **kwargs):
            pytest.fail("invalid balance must stop the positions read")

    class Bridge:
        config = {}

        def __init__(self):
            self.exchange_id = exchange_id

        def client(self):
            return Exchange()

    with pytest.raises(ValueError, match="invalid futures account response"):
        read_futures_snapshot(Bridge())
    assert calls == ["balance"]


@pytest.mark.parametrize(
    ("exchange_id", "balance"),
    [
        ("okx", {"info": {"data": [{"totalEq": "100", "details": []}]}}),
        ("bybit", {"info": {"result": {"list": [{"accountType": "CONTRACT", "coin": []}]}}}),
        ("bybit", {"info": {"result": {"list": [{"accountType": "CONTRACT", "coin": [
            {"coin": "BTC", "walletBalance": "1"}, {"coin": "USDT", "walletBalance": "2"},
        ]}]}}}),
    ],
)
def test_well_formed_account_without_safe_totals_is_preserved(exchange_id, balance):
    validate_balance(balance, exchange_id)
    assert isinstance(sanitize_balance(balance), dict)

    class Exchange:
        def fetch_balance(self, params=None):
            return balance

        def fetch_positions(self, symbols=None, params=None):
            return []

    class Bridge:
        config = {}

        def __init__(self):
            self.exchange_id = exchange_id

        def client(self):
            return Exchange()

    snapshot = read_futures_snapshot(Bridge())
    assert snapshot["status"] == "ok"
    assert snapshot["positions_read"]["status"] == "partial"


def test_balance_sanitization_preserves_unknown_instead_of_zero():
    balance = {"info": {"data": [{"totalEq": "100", "details": [
        {"ccy": "USDT", "cashBal": True, "availEq": "NaN"},
    ]}]}}
    validate_balance(balance, "okx")
    sanitized = sanitize_balance(balance)
    assert sanitized["info"]["data"][0]["totalEq"] == 100
    assert sanitized["info"]["data"][0]["details"][0] == {
        "ccy": "USDT", "cashBal": None, "availEq": None,
    }
