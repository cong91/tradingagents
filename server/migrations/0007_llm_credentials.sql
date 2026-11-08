-- Tier-specific encrypted LLM API credentials.
-- The Fernet ciphertext uses the separate control-panel vault key; no plaintext
-- provider key is stored in PostgreSQL, engine settings, or audit events.
CREATE TABLE IF NOT EXISTS llm_credentials (
    tier       text NOT NULL CHECK (tier IN ('quick', 'deep')),
    provider   text NOT NULL,
    secret     text NOT NULL,
    updated_at text NOT NULL,
    updated_by text,
    PRIMARY KEY (tier, provider)
);
