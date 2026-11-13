-- Outcome ledger: net-result attribution chain cycle -> order -> fill -> exit.
-- Additive only: empty tables, no backfill, no rewrite of existing rows.
-- Ingestion starts at enable time; anything before that stays uncovered and is
-- reported through outcome_coverage, never attributed retroactively.
--
-- account_id is a logical venue-account identity string (e.g.
-- "binance:demo:<identity hash prefix>"), not a UUID foreign key: the runtime
-- writer has no trading_accounts rows to join to, and a guessed key would
-- attach one account's history to another. There is no FK here on purpose.
--
-- Currency discipline: every amount keeps its own currency field. Mixed
-- currencies are never summed in SQL or code; missing amounts stay NULL and
-- read as "unknown", never as zero.

CREATE TABLE IF NOT EXISTS outcome_positions (
    outcome_id       UUID PRIMARY KEY,
    account_id       TEXT NOT NULL,
    cycle_id         TEXT,
    client_order_id  TEXT,
    venue_order_id   TEXT,
    symbol           TEXT NOT NULL,
    market_type      TEXT NOT NULL CHECK (market_type IN ('spot', 'perpetual', 'future')),
    side             TEXT NOT NULL CHECK (side IN ('long', 'short')),
    planned_quantity NUMERIC,
    filled_quantity  NUMERIC NOT NULL CHECK (filled_quantity >= 0),
    opened_at        TIMESTAMPTZ,
    closed_at        TIMESTAMPTZ,
    -- managed  = provenance marker + order link both held at ingest time;
    -- recovered = the same intent found through the recovery path, merged into
    --             the existing client_order_id (never a second position);
    -- unattributed = no evidence: ingestion must never write such a row, the
    --             vocabulary stays only to keep the contract explicit.
    attribution      TEXT NOT NULL CHECK (attribution IN ('managed', 'recovered', 'unattributed')),
    strategy_version TEXT,
    provenance       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, client_order_id),
    CHECK (attribution = 'unattributed' OR client_order_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_outcome_positions_cycle
    ON outcome_positions(cycle_id) WHERE cycle_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_outcome_positions_account_closed
    ON outcome_positions(account_id, closed_at DESC);

CREATE TABLE IF NOT EXISTS outcome_fills (
    fill_id                UUID PRIMARY KEY,
    account_id             TEXT NOT NULL,
    outcome_id             UUID NOT NULL REFERENCES outcome_positions(outcome_id),
    venue_trade_id         TEXT NOT NULL,
    venue_order_id         TEXT,
    symbol                 TEXT NOT NULL,
    side                   TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    -- role pins whether this trade opened or reduced the position, decided
    -- from the position's own side, not from the venue's field spelling.
    role                   TEXT NOT NULL CHECK (role IN ('entry', 'exit')),
    quantity               NUMERIC NOT NULL CHECK (quantity > 0),
    price                  NUMERIC NOT NULL CHECK (price > 0),
    fee                    NUMERIC,
    fee_currency           TEXT,
    realized_pnl           NUMERIC,
    realized_pnl_currency  TEXT,
    -- Whether the venue's realized_pnl already contains the fee; the net
    -- formula reads this instead of assuming (plan §2.2: the decision lives
    -- in ingestion, pinned by test).
    realized_pnl_semantics TEXT,
    occurred_at            TIMESTAMPTZ NOT NULL,
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, venue_trade_id)
);
CREATE INDEX IF NOT EXISTS idx_outcome_fills_outcome
    ON outcome_fills(outcome_id, occurred_at);

-- One row per exit event (a fill that reduced the position), classified into
-- a leg. The uniqueness tuple includes closed_at, which the store always
-- fills from the underlying exit fill's occurred_at (never NULL), so a
-- replayed ingest collides instead of duplicating the exit.
CREATE TABLE IF NOT EXISTS outcome_exits (
    exit_id         UUID PRIMARY KEY,
    account_id      TEXT NOT NULL,
    outcome_id      UUID NOT NULL REFERENCES outcome_positions(outcome_id),
    leg             TEXT NOT NULL CHECK (leg IN ('bracket', 'stop', 'target', 'scale_out', 'time_stop')),
    exit_reason     TEXT,
    quantity        NUMERIC NOT NULL CHECK (quantity > 0),
    price           NUMERIC NOT NULL CHECK (price > 0),
    -- JSONB list of {"currency": str|null, "amount": str|null}: Decimal exactness
    -- as text; amount null = unknown, never 0; mixed currencies stay separate.
    net_per_currency JSONB NOT NULL DEFAULT '[]'::jsonb,
    r_multiple      NUMERIC,
    closed_at       TIMESTAMPTZ,
    provenance      JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (account_id, outcome_id, leg, price, quantity, closed_at)
);

CREATE TABLE IF NOT EXISTS outcome_funding (
    funding_id      UUID PRIMARY KEY,
    account_id      TEXT NOT NULL,
    outcome_id      UUID NOT NULL REFERENCES outcome_positions(outcome_id),
    funding_at      TIMESTAMPTZ NOT NULL,
    amount          NUMERIC NOT NULL,
    currency        TEXT NOT NULL,
    -- Venue sign contract recorded verbatim at ingest time; the formula reads
    -- it instead of assuming: "binance: income positive = received".
    venue_semantics TEXT NOT NULL,
    -- Venue settlements are >= 1h apart, so this is a stable replay key.
    UNIQUE (account_id, outcome_id, funding_at, currency)
);

CREATE TABLE IF NOT EXISTS outcome_coverage (
    account_id                   TEXT NOT NULL,
    window_start                 TIMESTAMPTZ NOT NULL,
    window_end                   TIMESTAMPTZ NOT NULL,
    fills_found                  BIGINT NOT NULL DEFAULT 0 CHECK (fills_found >= 0),
    ledger_realized_total        JSONB NOT NULL DEFAULT '[]'::jsonb,
    venue_realized_total         JSONB NOT NULL DEFAULT '[]'::jsonb,
    realized_diff                JSONB NOT NULL DEFAULT '[]'::jsonb,
    realized_diff_ratio          NUMERIC,
    -- Same vocabulary as FuturesTradeHistory.status (performance_history
    -- returns "unsupported" for non-Binance venues): 'unsupported' is a real
    -- outcome, not an error.
    status                       TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'error', 'unsupported', 'unavailable')),
    warnings                     JSONB NOT NULL DEFAULT '[]'::jsonb,
    unattributed_manual_detected BIGINT NOT NULL DEFAULT 0 CHECK (unattributed_manual_detected >= 0),
    partial_entry_count          BIGINT NOT NULL DEFAULT 0 CHECK (partial_entry_count >= 0),
    -- What the window did not cover (pre-enable history, skipped symbols) is
    -- published here instead of being folded into zeros.
    snapshot                     JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, window_start, window_end)
);

CREATE TABLE IF NOT EXISTS outcome_ledger_state (
    -- Restart catch-up cursor for the outcome ledger worker; one row per key.
    state_key    TEXT PRIMARY KEY,
    cursor_value TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
