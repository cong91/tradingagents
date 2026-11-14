-- Standardize the futures cycle identity (no legacy ids are kept).
--
-- Every row now carries the operator-selected candle as explicit columns and
-- cycle_id is the canonical 7-segment key:
--   futures:{symbol}:{4h open ISO}:{1h open ISO}:1:{timeframe}:{selected open ms}
--
-- Backfill policy for rows written before selected timeframes existed: the
-- pre-timeframe runtime analyzed the fixed 4h candle (DEFAULT_TIMEFRAME of the
-- Kronos wrapper and the analysis path), so timeframe='4h' with
-- selected_open = the row's four_hour_open is the faithful reconstruction,
-- not an invented value. Rows that already carry a 7-segment id read the
-- selected identity recorded in their snapshot's candle_identity.
--
-- Coordinated rewrites (all keyed by exact old cycle_id, no FKs exist):
--   * outcome_positions.cycle_id
--   * outcome_positions.account_id for cycle-error coverage rows
--     ("cycle-error:{cycle_id}")
--   * outcome_coverage.account_id for the same cycle-error rows
--   * outcome_ledger_state.state_key "reconcile:{cycle_id}" / "failed:{cycle_id}"
--   * outcome_ledger_state cursor_value "updated_at|cycle_id" (the ledger
--     cursor key row only)
-- The shadow_forecasts.cycle_id references are deliberately NOT rewritten:
-- project rules forbid reading or writing the historical Shadow tables in the
-- current runtime, so pre-migration shadow rows keep their historical ids
-- (documented dangling references, never joined by the runtime).
--
-- Tie-break note: the ledger scans order by (updated_at, cycle_id); rewriting
-- ids can reorder rows whose updated_at is exactly equal, which only affects
-- processing order inside one poll batch, never attribution.

ALTER TABLE futures_cycles ADD COLUMN IF NOT EXISTS timeframe TEXT;
ALTER TABLE futures_cycles ADD COLUMN IF NOT EXISTS selected_open BIGINT;

-- 1. Backfill the selected identity columns.
UPDATE futures_cycles SET
    timeframe = COALESCE(
        payload->'snapshot'->'candle_identity'->>'timeframe',
        '4h'),
    selected_open = COALESCE(
        NULLIF(payload->'snapshot'->'candle_identity'->>'open_time', '')::bigint,
        (EXTRACT(EPOCH FROM four_hour_open::timestamptz) * 1000)::bigint)
WHERE timeframe IS NULL OR selected_open IS NULL;

-- 2. Map every old id to its canonical form.
CREATE TEMP TABLE _cycle_id_map AS
SELECT cycle_id AS old_id,
       'futures:' || symbol || ':' || four_hour_open || ':' || one_hour_open
         || ':1:' || timeframe || ':' || selected_open::text AS new_id
FROM futures_cycles;

-- 3. A legacy 5-segment row can share a target with an existing canonical
--    row for the same candle (the mixed case the dropped 0002 unique key used
--    to crash on). The canonical row is authoritative; the legacy artifact is
--    removed rather than kept under a second key.
DELETE FROM futures_cycles c
USING _cycle_id_map m
WHERE c.cycle_id = m.old_id
  AND c.cycle_id <> m.new_id
  AND EXISTS (SELECT 1 FROM futures_cycles keep WHERE keep.cycle_id = m.new_id);

-- 4. Rewrite the primary keys and every exact-key reference.
UPDATE futures_cycles c
SET cycle_id = m.new_id
FROM _cycle_id_map m
WHERE c.cycle_id = m.old_id AND c.cycle_id <> m.new_id;

UPDATE outcome_positions p
SET cycle_id = m.new_id
FROM _cycle_id_map m
WHERE p.cycle_id = m.old_id;

UPDATE outcome_positions p
SET account_id = 'cycle-error:' || m.new_id
FROM _cycle_id_map m
WHERE p.account_id = 'cycle-error:' || m.old_id;

UPDATE outcome_coverage cov
SET account_id = 'cycle-error:' || m.new_id
FROM _cycle_id_map m
WHERE cov.account_id = 'cycle-error:' || m.old_id;

UPDATE outcome_ledger_state s
SET state_key = 'reconcile:' || m.new_id
FROM _cycle_id_map m
WHERE s.state_key = 'reconcile:' || m.old_id;

UPDATE outcome_ledger_state s
SET state_key = 'failed:' || m.new_id
FROM _cycle_id_map m
WHERE s.state_key = 'failed:' || m.old_id;

-- reconcile rows carry the cycle id in cursor_value too
-- (server/outcome_store.py): without this rewrite reconciliation_batch can
-- never join the migrated row to futures_cycles again and an in-flight
-- cycle's outcome reconciliation silently stops. 'permanent' markers keep
-- their sentinel value. (failed: rows carry attempt counts or 'permanent',
-- not a cycle id, so the failed: rewrite below is defensive symmetry only.)
UPDATE outcome_ledger_state s
SET cursor_value = m.new_id
FROM _cycle_id_map m
WHERE s.state_key = 'reconcile:' || m.new_id
  AND s.cursor_value = m.old_id;

UPDATE outcome_ledger_state s
SET cursor_value = m.new_id
FROM _cycle_id_map m
WHERE s.state_key = 'failed:' || m.new_id
  AND s.cursor_value = m.old_id;

-- The ledger restart cursor is "updated_at|cycle_id"; rewrite its trailing id.
UPDATE outcome_ledger_state s
SET cursor_value = left(s.cursor_value, length(s.cursor_value) - length(m.old_id)) || m.new_id
FROM _cycle_id_map m
WHERE s.state_key = 'outcome_ledger'
  AND right(s.cursor_value, length(m.old_id)) = m.old_id;

DROP TABLE _cycle_id_map;

-- 5. Lock the format in the database: the id is exactly the concatenation of
--    its own columns, so a producer that drifts from the canonical builder
--    cannot persist a row at all.
ALTER TABLE futures_cycles
    ALTER COLUMN timeframe SET NOT NULL,
    ALTER COLUMN selected_open SET NOT NULL;
ALTER TABLE futures_cycles ADD CONSTRAINT futures_cycles_canonical_id CHECK (
    cycle_id = 'futures:' || symbol || ':' || four_hour_open || ':' || one_hour_open
                || ':1:' || timeframe || ':' || selected_open::text
);
