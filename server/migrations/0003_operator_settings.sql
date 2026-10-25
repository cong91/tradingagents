-- Operator overrides, durable in PostgreSQL.
--
-- Replaces server/data/operator_config.json. These are the values a human sets
-- deliberately and expects to survive a restart: the config halves of the
-- execution gates (which PUT /api/settings refuses) plus a few operational
-- knobs. The gate halves stay one switch of a two-switch design — the
-- environment halves are read fail-closed at execute time and never live here.
--
-- Written only through server/operator_config.py, which validates an explicit
-- allowlist and records every change in control_server_audit.

CREATE TABLE IF NOT EXISTS control_operator_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
