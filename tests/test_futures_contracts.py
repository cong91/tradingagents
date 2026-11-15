import pytest

from tradingagents.execution.futures_contracts import (
    FuturesContractError,
    FuturesContractSpec,
)


@pytest.mark.unit
def test_linear_contract_spec_converts_base_and_contract_quantities():
    spec = FuturesContractSpec.from_market(
        "BTC/USDT:USDT",
        {"contractSize": 0.01, "linear": True, "inverse": False},
    )

    assert spec.contracts_for_base(0.002) == pytest.approx(0.2)
    assert spec.base_for_contracts(0.2) == pytest.approx(0.002)


@pytest.mark.unit
@pytest.mark.parametrize("market", [
    None,
    {},
    {"contractSize": None, "linear": True, "inverse": False},
    {"contractSize": 0, "linear": True, "inverse": False},
    {"contractSize": float("nan"), "linear": True, "inverse": False},
    {"contractSize": True, "linear": True, "inverse": False},
    {"contractSize": 1, "linear": False, "inverse": False},
    {"contractSize": 1, "linear": True, "inverse": True},
])
def test_contract_spec_rejects_missing_or_unsupported_market_metadata(market):
    with pytest.raises(FuturesContractError, match="contract metadata"):
        FuturesContractSpec.from_market("BTC/USDT:USDT", market)


@pytest.mark.unit
@pytest.mark.parametrize("quantity", [0, -1, float("nan"), True, None])
def test_contract_conversion_rejects_nonpositive_or_invalid_quantities(quantity):
    spec = FuturesContractSpec.from_market(
        "BTC/USDT:USDT",
        {"contractSize": 1, "linear": True, "inverse": False},
    )

    with pytest.raises(FuturesContractError):
        spec.contracts_for_base(quantity)
