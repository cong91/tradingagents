"""L5: the outcome-ledger worker — independent of the autoloop, durable
cursor/batch pagination, restart catch-up, dedup, honest coverage.

Exit-fill fixtures mirror the production evidence chain: the exit fill
carries the BRACKET order id, and the bracket task (keyed by the entry venue
order id, same account identity) holds it in placed_ids — never an exit fill
borrowing the entry order id, a state production cannot produce.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from server import futures_cycle_store, job_store, outcome_ledger, outcome_report, outcome_store
from tradingagents.execution.futures_entry_journal import managed_live_provenance
from tradingagents.execution.sizing import PlannedOrder

pytestmark = pytest.mark.unit

ACCOUNT_HASH = "b" * 64
ACCOUNT_ID = f"binance:demo:{ACCOUNT_HASH}"
ORDER_ID = "9001"
CLIENT_ORDER_ID = "ta_worker123"
SYMBOL = "BTC/USDT:USDT"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _marker(client_order_id: str = CLIENT_ORDER_ID,
            account_type: str = "futures") -> dict:
    order = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol=SYMBOL, signal="Buy", side="buy",
        quantity=1.0, estimated_price=100.0, cost=100.0,
    )
    identity = SimpleNamespace(
        exchange="binance", profile="demo", account_type=account_type,
        identity_hash=ACCOUNT_HASH,
    )
    return managed_live_provenance(order, client_order_id, identity=identity, venue="binance:demo")


_CYCLE_SEQ = count()


def _record_executed_cycle(cycle_id: str, *, order_id: str | None = ORDER_ID,
                           executed: bool = True) -> None:
    # Candle-open pairs must be distinct per cycle: futures_cycles carries a
    # UNIQUE (symbol, four_hour_open, one_hour_open, strategy_version), so a
    # repeated pair is a real-world duplicate, not a retry.
    seq = next(_CYCLE_SEQ)
    now = datetime.now(UTC)
    four_hour_open = (now - timedelta(hours=4 + seq)).isoformat()
    one_hour_open = (now - timedelta(hours=1 + seq)).isoformat()
    payload = {
        "execution_status": "executed" if executed else "risk_rejected",
        "executed": executed,
        "order_id": order_id,
        "agent_proposal": {
            "entry_price": 100.0, "stop_loss": 98.0, "take_profit": 104.0,
            "leverage": 1.0, "risk_fraction": 0.01,
        },
        "signal": "Buy",
    }
    assert futures_cycle_store.claim_futures_cycle(
        cycle_id, symbol=SYMBOL, four_hour_open=four_hour_open,
        one_hour_open=one_hour_open, strategy_version="1",
        payload=payload,
    )
    futures_cycle_store.update_futures_cycle(
        cycle_id, status="completed" if executed else "rejected",
        phase="entry" if executed else "execution",
        payload=payload, terminal=True,
    )


def _record_journal(*, placed_ids: list[str], task_id: str | None = None,
                    client_order_id: str = CLIENT_ORDER_ID, bind_cycle: bool = True,
                    account_type: str = "futures") -> None:
    marker = _marker(client_order_id, account_type=account_type)
    task_id = task_id or marker["intent_id"]
    if bind_cycle:
        with psycopg.connect(os.environ["TRADINGAGENTS_DATABASE_URL"]) as conn:
            conn.execute(
                """UPDATE futures_cycles SET payload = payload || %s::jsonb
                   WHERE payload->>'order_id' = ANY(%s)""",
                (json.dumps({"entry_provenance": marker, "entry_intent_id": marker["intent_id"]}),
                 placed_ids),
            )
    leg = {
        "name": "entry-intent", "client_order_id": client_order_id,
        "symbol": SYMBOL, "side": "buy",
        "entry_provenance": marker,
    }
    assert job_store.create_bracket_recovery(
        task_id, symbol=SYMBOL, entry_order_id=client_order_id, close_side="sell",
        quantity=1.0, legs=[leg], now=_now_iso(),
        venue_key="binance:demo",
        identity=SimpleNamespace(
            exchange="binance", profile="demo", account_type=account_type,
            identity_hash=ACCOUNT_HASH,
        ),
    )
    job_store.update_bracket_recovery(
        task_id, status="completed", placed_ids=placed_ids, attempts=0,
        last_error=None, now=_now_iso(),
    )


def _record_bracket_task(entry_order_id: str, placed_ids: list[str],
                         task_id: str | None = None,
                         account_type: str = "futures") -> None:
    """The production exit evidence: a bracket task keyed by the ENTRY venue
    order id, whose placed_ids carry the stop/target/scale-out order ids
    (brackets._save / scale_out_apply), bound to the same account identity."""
    legs = [
        {"name": "stop-loss", "status": "placed", "order_id": placed_ids[0]},
    ]
    assert job_store.create_bracket_recovery(
        task_id or f"bracket:{SYMBOL}:{entry_order_id}",
        symbol=SYMBOL, entry_order_id=entry_order_id, close_side="sell",
        quantity=1.0, legs=legs, now=_now_iso(),
        venue_key="binance:demo",
        identity=SimpleNamespace(
            exchange="binance", profile="demo", account_type=account_type,
            identity_hash=ACCOUNT_HASH,
        ),
    )
    job_store.update_bracket_recovery(
        task_id or f"bracket:{SYMBOL}:{entry_order_id}",
        status="monitoring", placed_ids=placed_ids, attempts=0,
        last_error=None, now=_now_iso(),
    )


def _venue_fill(trade_id: str, order_id: str, *, realized: str | None = None,
                side: str = "buy", price: str = "100") -> dict:
    return {
        "trade_id": trade_id,
        "order_id": order_id,
        "symbol": SYMBOL,
        "side": side,
        "timestamp": _now_iso(),
        "contracts": "1.0",
        "price": price,
        "fee": "0.1",
        "fee_currency": "USDT",
        "realized_pnl": realized,
        "realized_pnl_currency": "USDT" if realized is not None else None,
    }


def _fill_source(fills: list[dict], *, status: str = "ok", funding: list | None = None):
    def source(account_id: str, symbol: str, since_ms: int, until_ms: int) -> dict:
        return {"status": status, "fills": fills, "warnings": [], "funding": funding,
                "start_at": datetime.fromtimestamp(since_ms / 1000, UTC).isoformat(),
                "end_at": datetime.fromtimestamp((until_ms + 1) / 1000, UTC).isoformat()}
    return source


def _raising_source(message: str = "venue down"):
    def source(account_id: str, symbol: str, since_ms: int, until_ms: int) -> dict:
        raise RuntimeError(message)
    return source


def test_worker_module_is_independent_of_the_autoloop():
    """The outcome worker remains isolated from the autoloop."""
    import ast

    def imported_modules(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        return names

    worker_imports = imported_modules(Path(outcome_ledger.__file__))
    assert not any(name.startswith("server.autoloop") for name in worker_imports)
    autoloop_imports = imported_modules(
        Path(__file__).parents[1] / "server" / "autoloop.py"
    )
    assert not any(
        name.startswith(("server.outcome_ledger", "server.outcome_store", "tradingagents.outcomes"))
        for name in autoloop_imports
    )


def test_terminal_vocabulary_matches_futures_cycle_store():
    assert outcome_store.TERMINAL_CYCLE_STATUSES == futures_cycle_store._FUTURES_TERMINAL_STATUSES


def test_disabled_ingest_does_nothing(postgres_schema):
    _record_executed_cycle("futures:BTC/USDT:USDT:t0")
    result = outcome_ledger.poll_once(_fill_source([]))
    assert result["status"] == "disabled"
    assert outcome_report.summary()["status"] == "unavailable"


def test_first_poll_initializes_cursor_without_backfill(postgres_schema, ingest_enabled):
    _record_executed_cycle("futures:BTC/USDT:USDT:t1")
    result = outcome_ledger.poll_once(_fill_source([]))
    assert result["status"] == "initialized"
    assert "no backfill" in result["note"]
    # History before enable stays uncovered: nothing was attributed.
    assert outcome_report.summary()["status"] == "unavailable"


@pytest.fixture
def ingest_enabled():
    """Env overrides apply at config build, so tests flip the worker flag
    through the process config and restore it afterwards."""
    from tradingagents.dataflows.config import set_config

    set_config({"outcome_ledger_ingest_enabled": True})
    yield
    set_config({"outcome_ledger_ingest_enabled": False})


def _enable_and_seed() -> None:
    """The cursor sits one hour behind the clock so the cycles the tests
    claim right now (futures_cycle_store stamps them with _now) fall after it."""
    seeded = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    outcome_store.set_cursor(f"{seeded}|seed")


def test_executed_cycle_with_managed_fills_is_recorded(postgres_schema, ingest_enabled):
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:t2")
    _record_journal(placed_ids=[ORDER_ID])
    # Production evidence: the exit fill carries the BRACKET order id, not the
    # entry order id — the bracket task holds it (blocker 2 fix).
    _record_bracket_task(ORDER_ID, placed_ids=["9500"])
    result = outcome_ledger.poll_once(_fill_source([
        _venue_fill("700", ORDER_ID),
        _venue_fill("701", "9500", realized="9.9", side="sell", price="104"),
    ]))
    assert result["status"] == "polled"
    assert result["processed"] == 1
    outcome = result["outcomes"][0]
    assert outcome["result"] == "recorded"
    assert outcome["fills_attributed"] == 2
    assert outcome["unattributed"] == 0
    summary = outcome_report.summary()
    assert summary["status"] == "partial"
    assert summary["positions"]["total"] == 1
    assert summary["positions"]["closed"] == 1
    assert summary["totals"]["realized_by_currency"]["USDT"] == "9.9"
    assert summary["totals"]["fee_by_currency"]["USDT"] == "0.2"
    # The net claim is allowed: funding rows exist? No — but the fee semantics
    # are recorded per fill, so fees subtract from the net honestly.
    net_rows = {row["currency"]: row for row in summary["totals"]["net_by_currency"]}
    assert net_rows["USDT"]["fee_known"] is True
    assert net_rows["USDT"]["funding_known"] is False


def test_exit_fill_without_bracket_evidence_stays_unattributed(
    postgres_schema, ingest_enabled,
):
    """A sell fill with no bracket evidence is NOT claimed as a bot exit:
    the realized PnL stays out of the ledger and the fill shows up as
    unattributed in coverage (fail-closed, never fabricated)."""
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:t2b")
    _record_journal(placed_ids=[ORDER_ID])
    result = outcome_ledger.poll_once(_fill_source([
        _venue_fill("710", ORDER_ID),
        _venue_fill("711", "77777", realized="50", side="sell", price="110"),
    ]))
    outcome = result["outcomes"][0]
    assert outcome["fills_attributed"] == 1
    assert outcome["unattributed"] == 1
    summary = outcome_report.summary()
    assert summary["totals"]["realized_by_currency"] == {}
    coverage = summary["coverage"][0]
    assert coverage["unattributed_manual_detected"] == 1


def test_cursor_advances_and_dedup_is_durable(postgres_schema, ingest_enabled):
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:t3")
    _record_journal(placed_ids=[ORDER_ID])
    _record_bracket_task(ORDER_ID, placed_ids=["9501"])
    fills = [
        _venue_fill("800", ORDER_ID),
        _venue_fill("801", "9501", side="sell", price="104", realized="4.0"),
    ]
    outcome_ledger.poll_once(_fill_source(fills))
    again = outcome_ledger.poll_once(_fill_source(fills))
    # Discovery has advanced; reconciliation revisits without duplicating fills.
    assert again["processed"] == 0
    assert len(again["reconciled"]) == 1
    summary = outcome_report.summary()
    assert summary["positions"]["total"] == 1
    assert summary["fills"] == 2


def test_restart_catch_up_covers_cycles_completed_while_worker_was_down(
    postgres_schema, ingest_enabled,
):
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:t4a")
    _record_journal(placed_ids=[ORDER_ID], client_order_id="ta_worker_a")
    _record_bracket_task(ORDER_ID, placed_ids=["8500"], task_id=f"bracket:{SYMBOL}:{ORDER_ID}")
    result = outcome_ledger.poll_once(_fill_source([
        _venue_fill("850", ORDER_ID),
        _venue_fill("852", "8500", side="sell", price="104", realized="3.0"),
    ]))
    assert result["processed"] == 1
    # The worker was "down": a second cycle landed afterwards.
    second_order = "9100"
    _record_executed_cycle("futures:BTC/USDT:USDT:t4b", order_id=second_order)
    _record_journal(placed_ids=[second_order], client_order_id="ta_worker_b")
    _record_bracket_task(second_order, placed_ids=["9500"], task_id=f"bracket:{SYMBOL}:{second_order}")
    caught_up = outcome_ledger.poll_once(_fill_source([
        _venue_fill("851", second_order),
        _venue_fill("853", "9500", side="sell", price="106", realized="5.0"),
    ]))
    assert caught_up["processed"] == 1
    assert caught_up["outcomes"][0]["cycle_id"] == "futures:BTC/USDT:USDT:t4b"
    summary = outcome_report.summary()
    assert summary["positions"]["total"] == 2
    assert summary["totals"]["realized_by_currency"]["USDT"] == "8.0"


def test_batch_is_bounded_and_ordered(postgres_schema, ingest_enabled):
    _enable_and_seed()
    for index in range(5):
        cycle_order_id = str(9000 + index)
        _record_executed_cycle(f"futures:BTC/USDT:USDT:b{index}", order_id=cycle_order_id)
        _record_journal(placed_ids=[cycle_order_id], client_order_id=f"ta_worker_{index}")
        _record_bracket_task(cycle_order_id, placed_ids=[str(9500 + index)],
                             task_id=f"bracket:{SYMBOL}:{cycle_order_id}")
    fills = [
        _venue_fill(str(9700 + index), str(9000 + index)) for index in range(5)
    ] + [
        _venue_fill(str(9800 + index), str(9500 + index), side="sell", price="104",
                    realized="2.0")
        for index in range(5)
    ]
    result = outcome_ledger.poll_once(_fill_source(fills), batch_size=2)
    assert result["processed"] == 2
    first_ids = [outcome["cycle_id"] for outcome in result["outcomes"]]
    assert first_ids == sorted(first_ids)
    # Next poll continues exactly where the cursor stopped.
    second = outcome_ledger.poll_once(_fill_source([]), batch_size=2)
    assert second["processed"] == 2
    third = outcome_ledger.poll_once(_fill_source([]), batch_size=2)
    assert third["processed"] == 1


def test_manual_fills_stay_unattributed_and_reach_coverage(postgres_schema, ingest_enabled):
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:t5")
    _record_journal(placed_ids=[ORDER_ID])
    _record_bracket_task(ORDER_ID, placed_ids=["9505"])
    manual_fill = _venue_fill("990", "88888", realized="50", side="sell", price="110")
    result = outcome_ledger.poll_once(_fill_source([manual_fill]))
    outcome = result["outcomes"][0]
    assert outcome["result"] == "no_fills_in_window"
    assert outcome["unattributed"] == 1
    summary = outcome_report.summary()
    # Nothing was attributed to the bot: zero positions, and the manual fill
    # lives only in the coverage row — never in the ledger's PnL.
    assert summary["positions"]["total"] == 0
    assert summary["fills"] == 0
    coverage = summary["coverage"][0]
    assert coverage["unattributed_manual_detected"] == 1
    assert coverage["status"] == "partial"


def test_unsupported_venue_status_is_preserved_in_coverage(postgres_schema, ingest_enabled):
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:t6")
    _record_journal(placed_ids=[ORDER_ID])
    result = outcome_ledger.poll_once(_fill_source([], status="unsupported"))
    assert result["outcomes"][0]["result"] == "no_fills_in_window"
    coverage = outcome_report.summary()["coverage"][0]
    assert coverage["status"] == "unsupported"


def test_recovery_map_merges_a_retry_fill_into_the_same_position(
    postgres_schema, ingest_enabled,
):
    """Defense-in-depth contract: a retry order id found in a journal task's
    placed_ids is the SAME intent — one position, never a second one. (The
    venue order id normally survives a clientOrderId retry unchanged, so the
    primary path is attribute_fill; this map only ever widens, never splits.)"""
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:t7")
    _record_journal(placed_ids=[ORDER_ID, "7777"])  # task records both ids
    result = outcome_ledger.poll_once(_fill_source([
        _venue_fill("860", ORDER_ID),
        _venue_fill("861", "7777", side="buy", price="104"),
    ]))
    outcome = result["outcomes"][0]
    assert outcome["fills_attributed"] == 2
    assert outcome["counts"]["positions_new"] == 1
    summary = outcome_report.summary()
    assert summary["positions"]["total"] == 1
    assert summary["fills"] == 2


def test_funding_without_event_identity_remains_unattributed(postgres_schema, ingest_enabled):
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:t8")
    _record_journal(placed_ids=[ORDER_ID])
    _record_bracket_task(ORDER_ID, placed_ids=["9508"])
    funding = [{
        "timestamp": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        # one settlement after the entry fill, inside the position's window
        "amount": "-0.25",  # venue sign: negative = paid
        "currency": "USDT",
        "venue_semantics": "binance: income positive = received",
    }]
    outcome_ledger.poll_once(_fill_source([
        _venue_fill("870", ORDER_ID),
        _venue_fill("871", "9508", side="sell", price="104", realized="1.0"),
    ], funding=funding))
    summary = outcome_report.summary()
    totals = summary["totals"]
    assert totals["funding_by_currency"] == {}
    assert any("funding_position_attribution_unknown" in row["warnings"]
               for row in summary["coverage"])


def test_funding_without_venue_semantics_is_skipped_not_guessed(
    postgres_schema, ingest_enabled,
):
    """A funding row without an explicit semantics string is dropped, never
    defaulted to Binance's convention (finding 8: fill-in is fabrication)."""
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:t8b")
    _record_journal(placed_ids=[ORDER_ID])
    funding = [{
        "timestamp": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "amount": "-0.25",
        "currency": "USDT",
        # venue_semantics missing on purpose
    }]
    result = outcome_ledger.poll_once(
        _fill_source([_venue_fill("872", ORDER_ID)], funding=funding),
    )
    outcome = result["outcomes"][0]
    assert outcome["result"] == "recorded"
    assert outcome["counts"]["funding_new"] == 0
    assert outcome_report.summary()["totals"]["funding_by_currency"] == {}


def test_cycle_without_executed_order_is_skipped(postgres_schema, ingest_enabled):
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:t9", executed=False)
    result = outcome_ledger.poll_once(_fill_source([]))
    assert result["outcomes"][0]["result"] == "skipped_not_executed"
    assert outcome_report.summary()["status"] == "unavailable"


def test_failing_cycle_is_retried_then_published_as_error_coverage(
    postgres_schema, ingest_enabled,
):
    """Finding 4: a cycle that raises is never silently dropped — the cursor
    holds back while attempts remain, and past the bound the failure lands as
    a durable coverage row with status ``error``.

    The deterministic poison: an exit fill with a negative quantity violates
    the outcome_fills CHECK, so record_outcome (and the whole cycle) raises
    on every poll until the retry bound publishes the failure.
    """
    _enable_and_seed()
    _record_executed_cycle("futures:BTC/USDT:USDT:e1")
    _record_executed_cycle("futures:BTC/USDT:USDT:e2")
    # The journal is bound AFTER both cycles so each carries the marker:
    # a legacy marker-less cycle is skipped cleanly, which would move the
    # cursor past it instead of exercising the retry bound below.
    _record_journal(placed_ids=[ORDER_ID])
    _record_bracket_task(ORDER_ID, placed_ids=["9510"])

    poisoned_fills = [
        _venue_fill("900", ORDER_ID),
        _venue_fill("901", "9510", side="sell", price="104", realized="9.9"),
    ]
    poisoned_fills[1]["contracts"] = "-5"  # violates CHECK (quantity > 0)

    first = outcome_ledger.poll_once(_fill_source(poisoned_fills))
    assert first["outcomes"][0]["result"] == "error:CheckViolation" or (
        first["outcomes"][0]["result"].startswith("error:")
    )
    # The cursor did not advance past the failing cycle: poll 2 retries it.
    second = outcome_ledger.poll_once(_fill_source(poisoned_fills))
    assert second["outcomes"][0]["result"].startswith("error:")
    third = outcome_ledger.poll_once(_fill_source(poisoned_fills))
    assert third["outcomes"][0]["result"].startswith("error:")
    # Past MAX_CYCLE_ATTEMPTS the failure is published and the pipeline moves.
    coverage = outcome_report.summary()["coverage"]
    error_rows = [row for row in coverage if row["status"] == "error"]
    assert len(error_rows) == 1
    assert error_rows[0]["account_id"].startswith("cycle-error:")
    assert "venue read" in json.dumps(error_rows[0]["warnings"]) or (
        "attempts" in json.dumps(error_rows[0]["warnings"])
    )
    fourth = outcome_ledger.poll_once(_fill_source([]))
    assert fourth["processed"] == 1  # the cursor moved past the poisoned cycle to e2


def test_start_without_ingest_never_starts_a_thread():
    from tradingagents.dataflows.config import set_config

    set_config({"outcome_ledger_ingest_enabled": False})
    outcome_ledger.start()
    assert outcome_ledger._worker_thread is None
    outcome_ledger.stop()


def test_interval_seconds_coercion():
    assert outcome_ledger.interval_seconds({"outcome_ledger_interval_seconds": 12}) == 12.0
    assert outcome_ledger.interval_seconds(
        {"outcome_ledger_interval_seconds": "bogus"},
    ) == outcome_ledger.DEFAULT_INTERVAL_SECONDS
    assert outcome_ledger.ingest_enabled({"outcome_ledger_ingest_enabled": True}) is True
    assert outcome_ledger.ingest_enabled({"outcome_ledger_ingest_enabled": False}) is False
