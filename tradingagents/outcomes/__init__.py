"""Outcome ledger finance logic: attribution, currency buckets, netting, coverage.

Pure layer of the outcome ledger (plan §2.1-2.4): every rule here is a
function of its inputs. Venue I/O, PostgreSQL and the worker live in
``server/outcome_store.py`` and ``server/outcome_ledger.py``.
"""

from tradingagents.outcomes.attribution import (
    Attribution,
    AttributionDecision,
    CycleOrder,
    attribute_exit_fill,
    attribute_fill,
    attribute_recovered_fill,
    bracket_exit_order_ids,
    recovered_client_order_ids,
)
from tradingagents.outcomes.coverage import COVERAGE_STATUSES, build_coverage
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
from tradingagents.outcomes.netting import (
    VENUE_SEMANTICS_BINANCE,
    VENUE_SEMANTICS_FUNDING,
    classify_exit_leg,
    net_exit,
    partition_position_fills,
    r_multiple,
)

__all__ = [
    "COVERAGE_STATUSES",
    "Attribution",
    "AttributionDecision",
    "CurrencyAmount",
    "CycleOrder",
    "MixedCurrencySumError",
    "VENUE_SEMANTICS_BINANCE",
    "VENUE_SEMANTICS_FUNDING",
    "add_to_bucket",
    "attribute_exit_fill",
    "attribute_fill",
    "attribute_recovered_fill",
    "bracket_exit_order_ids",
    "build_coverage",
    "classify_exit_leg",
    "from_json",
    "net_exit",
    "partition_position_fills",
    "r_multiple",
    "recovered_client_order_ids",
    "single_currency_total",
    "sum_components",
    "to_json",
    "unknown",
]
