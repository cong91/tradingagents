-- Engine settings the operator saves through PUT /api/settings, durable in
-- PostgreSQL.
--
-- Replaces the runtime .env rewrite that used to persist these values: .env
-- goes back to being deployment-only configuration (credentials, venue URL,
-- the environment halves of the gates) that changes only with a restart,
-- while values saved from the control panel live here. Applied at boot by
-- server/main.py after the env layer and BEFORE the operator overrides, so a
-- deliberate operator override still outranks a settings save.
--
-- Written only through server/settings.py, which validates the
-- _ENV_OVERRIDES surface and refuses the read-only keys (exec_base_url) and
-- every gate key.

CREATE TABLE IF NOT EXISTS control_engine_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
