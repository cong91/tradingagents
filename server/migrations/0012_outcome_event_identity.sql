-- Preserve existing rows while giving new venue events their real identities.
ALTER TABLE outcome_fills
    DROP CONSTRAINT outcome_fills_account_id_venue_trade_id_key;
ALTER TABLE outcome_fills
    ADD CONSTRAINT outcome_fills_account_symbol_trade_key
    UNIQUE (account_id, symbol, venue_trade_id);

ALTER TABLE outcome_exits
    DROP CONSTRAINT outcome_exits_account_id_outcome_id_leg_price_quantity_clos_key;
ALTER TABLE outcome_exits
    ADD COLUMN fill_id UUID REFERENCES outcome_fills(fill_id);
ALTER TABLE outcome_exits
    ADD CONSTRAINT outcome_exits_fill_id_key UNIQUE (fill_id);
CREATE UNIQUE INDEX outcome_exits_legacy_identity
    ON outcome_exits(account_id, outcome_id, leg, price, quantity, closed_at)
    WHERE fill_id IS NULL;

-- Funding income is account/symbol scoped, and is not proof of position ownership.
CREATE TABLE outcome_funding_observations (
    account_id       TEXT NOT NULL,
    venue_key        TEXT NOT NULL,
    account_type     TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    venue_income_id  TEXT NOT NULL,
    funding_at       TIMESTAMPTZ NOT NULL,
    amount           NUMERIC NOT NULL,
    currency         TEXT NOT NULL,
    venue_semantics  TEXT NOT NULL,
    observed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, venue_key, account_type, symbol, venue_income_id)
);
