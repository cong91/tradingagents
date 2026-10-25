# PostgreSQL migrations

`0001_core_ledger.sql` is an explicit, additive baseline schema for an isolated
PostgreSQL environment. Run `python -m server.postgres --dry-run` to list the
migration plan without opening a connection, or `python -m server.postgres` to
apply pending files from `TRADINGAGENTS_DATABASE_URL`. The runner owns the
canonical `schema_migrations` ledger, records each file name and SHA-256
checksum, and serializes runs with a PostgreSQL advisory lock inside its
transaction. Migration files contain schema DDL only; do not add a separate
ledger row or explicit transaction wrapper.

The runner is not wired to application startup. The server continues to use its
existing SQLite/JSONL runtime stores; this artifact does not switch any read or
write path and does not dual-write. Applying this schema is not a production
cutover or permission to enable execution. Any future store cutover requires a
separately reviewed deployment/backfill plan and tested repository adapters.

## Ledger invariants

- Workspace ownership is carried through composite foreign keys on jobs, runs,
  decisions, approvals, orders, fills, context links, and position snapshots.
- Parent deletion does not cascade into analysis or financial history.
- One job result is permitted per `(workspace, job, instrument, attempt)`. A
  retry is a new attempt; this is a target-schema rule, not a statement about
  existing SQLite/JSONL history. Reconcile retry/update semantics before any
  import or backfill.
- Approval references, when present, must identify a result/run or a
  decision/run/instrument tuple consistently. `claim_approval` is a conditional
  update and yields a row to at most one concurrent worker.
- Before any venue request, the caller must claim the approval and insert the
  execution order in the same short database transaction. Commit before
  network I/O. Persist a timeout as `submission_unknown`; reconcile by account
  and client ID before any retry. No exactly-once guarantee is claimed across
  the database/venue boundary.
- `client_order_key` is the lowercase SHA-256 hex digest of a canonical intent
  scoped by workspace, account, approval, and intent version. The venue-facing
  ID is `ta_` plus the first 32 hex characters (35 ASCII characters total).
- Event idempotency keys and fill trade IDs are unique per account. Replayed
  duplicates therefore fail the uniqueness constraint instead of duplicating
  evidence.

## Integration verification

Run `.venv/Scripts/python.exe -m pytest -q tests/test_postgres_migrations_integration.py`.
The test starts a PostgreSQL 16 service using the isolated
`tests/postgres-compose.yml` project, then removes only its uniquely named Compose
project and volume. It skips when Docker Engine is unavailable. It does not
connect the API runtime to PostgreSQL.
