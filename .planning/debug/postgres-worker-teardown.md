# Debug: PostgreSQL missing control run tables

**Opened:** 2026-10-23
**Status:** resolved
**Reporter:** human partner

## Symptoms

- [x] PostgreSQL logs contain 5 UndefinedTable errors for control_run_events and 5 for control_runs.
- [x] Errors occur in pairs during test activity; runtime API logs contain no database stack traces.
- [x] One UndefinedColumn error for requested_ticker came from this session's diagnostic SQL, already corrected to payload->>'requested_ticker'.

## Eliminated Hypotheses

- [x] Runtime migrations absent: public schema contains control tables and all three migrations are recorded.
- [x] PostgreSQL corruption: database recovered successfully after host interruption and serves queries normally.
- [x] Public fallback: test search_path contains only the private schema.

## Evidence Log

| Timestamp | Data | Interpretation |
| --- | --- | --- |
| 2026-10-23 | All PostgreSQL history: 10 missing run/event table errors, latest 2026-10-22 00:58:28Z; one manual-query error 2026-10-22 15:50:04Z | Narrow error classes; no ongoing runtime error flood |
| 2026-10-23 | tests/conftest.py postgres_schema yields then immediately DROP SCHEMA without worker join | A worker can write after its schema is removed or env retargeted |
| 2026-10-23 | Independent fake-DB execution of actual fixture shows create/migrate/drop/worker-write with writer alive at drop | Confirmed fixture lifecycle defect |
| 2026-10-23 | Independent verifier confirmed root cause; blocked sign-off on unnamed test scheduler leak and stop()-raise blocking monitor shutdown | Both gaps then fixed and re-gated |

## Resolution

**Root Cause:** tests/conftest.py postgres_schema dropped its per-test PostgreSQL schema in teardown while background workers (server-run / server-daily / autoloop / monitors) could still persist — run_store writes control_runs/control_run_events with unqualified names, so any write after the drop raises UndefinedTable. The fixture also accepted any TRADINGAGENTS_TEST_DATABASE_URL, so a mispointed DSN could churn DDL against the runtime database.

**Fix:**
1. tests/conftest.py: reject a DSN whose dbname is not a dedicated test database (test, test_*, *_test, *_tests); join known persistence worker threads before DROP SCHEMA and retain the schema (test fails) if a worker does not exit within 10s.
2. server/autoloop.py: scheduler stop event + stop(); stop never raises (logs and lets a stuck daemon die with the process) and the loop re-checks the stop flag before spawning any cycle.
3. server/main.py lifespan: autoloop.stop() before monitor stops.
4. tests/test_server_autoloop.py: per-test scheduler stop event, teardown joins the scheduler, scheduler tests name their threads and assert exit, timeout tests deterministic (wait for worker to enter sync_portfolio / screen beyond the deadline).
5. tests/test_postgres_fixture_isolation.py: deterministic regressions — worker write strictly precedes schema drop; runtime-like db name rejected before connecting; join failure retains the schema.

**Gates:** ruff check . pass; full pytest on dedicated disposable PostgreSQL (migration_test): 1583 passed, 5 skipped; clean-import smoke pass; runtime PostgreSQL 0 ERROR/FATAL/PANIC in the final 45-minute window with control tables and 3 migrations intact. Independent verifier re-check of the two earlier blockers passes in the final state.

**Deliberate scope note:** runtime shutdown does not drain a running autoloop cycle (up to 60 min); only the test fixture joins cycle workers, because the bug class being fixed is test isolation, not live shutdown latency.
**Date:** 2026-10-23
