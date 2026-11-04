-- Account identity is explicit evidence, not something inferred from legacy state.
ALTER TABLE position_states
    ADD COLUMN account_identity_exchange TEXT,
    ADD COLUMN account_identity_profile TEXT,
    ADD COLUMN account_identity_type TEXT,
    ADD COLUMN account_identity_hash TEXT,
    ADD CONSTRAINT position_states_account_identity_complete
        CHECK (
            (account_identity_exchange IS NULL
                AND account_identity_profile IS NULL
                AND account_identity_type IS NULL
                AND account_identity_hash IS NULL)
            OR
            (account_identity_exchange IS NOT NULL
                AND account_identity_profile IS NOT NULL
                AND account_identity_type IS NOT NULL
                AND account_identity_hash IS NOT NULL
                AND account_identity_hash ~ '^[0-9a-f]{64}$')
        );

ALTER TABLE bracket_recovery_tasks
    ADD COLUMN account_identity_exchange TEXT,
    ADD COLUMN account_identity_profile TEXT,
    ADD COLUMN account_identity_type TEXT,
    ADD COLUMN account_identity_hash TEXT,
    ADD CONSTRAINT bracket_recovery_tasks_account_identity_complete
        CHECK (
            (account_identity_exchange IS NULL
                AND account_identity_profile IS NULL
                AND account_identity_type IS NULL
                AND account_identity_hash IS NULL)
            OR
            (account_identity_exchange IS NOT NULL
                AND account_identity_profile IS NOT NULL
                AND account_identity_type IS NOT NULL
                AND account_identity_hash IS NOT NULL
                AND account_identity_hash ~ '^[0-9a-f]{64}$')
        );

CREATE INDEX idx_position_states_account_identity
    ON position_states(account_identity_hash)
    WHERE account_identity_hash IS NOT NULL;

CREATE INDEX idx_bracket_recovery_tasks_account_identity
    ON bracket_recovery_tasks(account_identity_hash)
    WHERE account_identity_hash IS NOT NULL;
