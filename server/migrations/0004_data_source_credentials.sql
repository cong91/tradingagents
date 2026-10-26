-- Third-party session cookies for the sentiment data sources (Reddit, X).
--
-- ``secret`` holds Fernet ciphertext, never the cookie values: these are live
-- bearer sessions, and this table sits in the same database as the rest of the
-- control plane. The encryption key lives in the engine state directory, so a
-- database dump alone cannot decrypt anything.
--
-- Timestamps are text, matching futures_cycles and position_states.
CREATE TABLE IF NOT EXISTS data_source_credentials (
    provider   text PRIMARY KEY,
    secret     text NOT NULL,
    updated_at text NOT NULL,
    updated_by text
);
