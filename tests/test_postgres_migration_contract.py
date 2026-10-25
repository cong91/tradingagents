"""Static contract checks for the explicit PostgreSQL migration artifact."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MIGRATION = Path(__file__).parents[1] / "server" / "migrations" / "0001_core_ledger.sql"


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8").lower()


def test_migration_artifact_does_not_own_the_runner_ledger_or_transaction():
    sql = _sql()

    assert sql.lstrip().startswith("-- initial postgresql ledger schema")
    assert "create table schema_migrations" not in sql
    assert "insert into schema_migrations" not in sql
    assert "begin;" not in sql
    assert "commit;" not in sql


def test_runner_uses_one_checksum_ledger_and_serialized_transaction():
    from server import postgres

    source = Path(postgres.__file__).read_text(encoding="utf-8")
    assert postgres._LEDGER == "schema_migrations"
    assert "CREATE TABLE IF NOT EXISTS {_LEDGER}" in source
    assert "pg_advisory_lock" in source
    assert "with conn.transaction()" in source
    assert "pg_advisory_unlock" in source
    assert "app_schema_migrations" not in source


def test_dry_run_loads_files_without_opening_database(monkeypatch):
    from server import postgres

    def fail_if_connected():
        pytest.fail("dry-run must not connect to PostgreSQL")

    monkeypatch.setattr(postgres, "connect", fail_if_connected)
    assert postgres.migrate(dry_run=True) == [
        "0001_core_ledger.sql",
        "0002_control_plane.sql",
        "0003_operator_settings.sql",
    ]


def test_workspace_entities_use_composite_ownership_foreign_keys():
    sql = _sql()

    assert (
        "foreign key (workspace_id, run_id, instrument_id)\n"
        "        references analysis_runs(workspace_id, run_id, instrument_id)"
    ) in sql
    assert "foreign key (workspace_id, decision_id)\n        references trading_decisions(workspace_id, decision_id)" in sql
    assert "foreign key (workspace_id, referenced_decision_id)\n        references trading_decisions(workspace_id, decision_id)" in sql
    assert "primary key (workspace_id, account_id, instrument_id, market_type, position_side)" in sql
    assert "foreign key (workspace_id, account_id)\n        references trading_accounts(workspace_id, account_id)" in sql


def test_job_attempt_result_and_approval_references_are_consistent():
    sql = _sql()

    assert "unique (workspace_id, job_id, instrument_id, attempt)" in sql
    assert (
        "foreign key (workspace_id, job_id, instrument_id, attempt, run_id)\n"
        "        references analysis_runs(workspace_id, job_id, instrument_id, attempt, run_id)"
    ) in sql
    assert (
        "foreign key (workspace_id, job_result_id, run_id)\n"
        "        references job_results(workspace_id, job_result_id, run_id) match full"
    ) in sql
    assert (
        "foreign key (workspace_id, run_id, instrument_id)\n"
        "        references analysis_runs(workspace_id, run_id, instrument_id)"
    ) in sql
    assert (
        "foreign key (workspace_id, run_id, decision_id, instrument_id)\n"
        "        references trading_decisions(workspace_id, run_id, decision_id, instrument_id) match full"
    ) in sql
    assert "check (job_result_id is null or run_id is not null)" in sql
    assert "check (decision_id is null or run_id is not null)" in sql


def test_order_claim_and_replay_keys_are_scoped_and_bounded():
    sql = _sql()

    assert "client_order_key ~ '^[0-9a-f]{64}$'" in sql
    assert "client_order_id = 'ta_' || substr(client_order_key, 1, 32)" in sql
    assert "unique (account_id, client_order_key)" in sql
    assert "where status in ('created', 'submitting', 'submission_unknown', 'open'," in sql
    assert "unique (account_id, idempotency_key)" in sql
    assert "unique (account_id, venue_trade_id)" in sql
    assert "create function claim_approval" in sql
    assert "and status = 'pending'" in sql
    assert "and expires_at > clock_timestamp()" in sql
    assert "returning *;" in sql


def test_unknown_submission_stays_reconcilable_before_any_retry():
    sql = _sql()
    order_ddl = sql.split("create table execution_orders (", 1)[1].split("\n);", 1)[0]

    assert "'submission_unknown'" in order_ddl
    assert "unique (account_id, client_order_key)" in order_ddl
    assert "uq_execution_orders_venue_id" in sql
    assert "uq_execution_orders_active_client_id" in sql
    assert "uq_execution_orders_approval" in sql
    assert "unique (account_id, idempotency_key)" in sql
    assert "unique (account_id, venue_trade_id)" in sql


def test_approval_claim_is_a_single_conditional_update():
    sql = _sql()
    claim_ddl = sql.split("create function claim_approval", 1)[1].split("$$;", 1)[0]

    assert claim_ddl.count("update approval_requests") == 1
    assert "where workspace_id = p_workspace_id" in claim_ddl
    assert "and approval_id = p_approval_id" in claim_ddl
    assert "and status = 'pending'" in claim_ddl
    assert "and expires_at > clock_timestamp()" in claim_ddl
    assert "returning *;" in claim_ddl


def test_historical_rows_are_not_deleted_by_parent_cascades():
    sql = _sql()

    assert "on delete cascade" not in sql
    assert "delete from" not in sql
    assert "truncate " not in sql
