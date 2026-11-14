-- The operator-selected candle timeframe is part of the cycle identity.
-- Keep the primary-key claim idempotent, but do not collapse distinct selected
-- candles that share the fixed 1h+4h confirmation pair.
ALTER TABLE futures_cycles
    DROP CONSTRAINT IF EXISTS futures_cycles_symbol_four_hour_open_one_hour_open_strategy_key;
