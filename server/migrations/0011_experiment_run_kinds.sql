-- Extend the experiment-run kind vocabulary (plan §4, stage `experiments`).
-- Additive only: widens one CHECK, rewrites no row and backfills nothing.
--
-- Migration 0009 allowed `walkforward` and `replay_experiment`. The financial
-- entry point (`tradingagents.experiments.financial_experiment`) registers runs
-- whose proposals come from the RECORDED cycles plus a confirmation rule and
-- whose prices come from an imported closed-candle series: no agent or LLM is
-- re-run. Calling that a `walkforward` would overstate it, so it gets its own
-- honest kind and the constraint must admit it.
--
-- The constraint is dropped by its generated name and re-added with the widened
-- list; the query drops the old constraint only when it exists, so a re-run on a
-- database that already applied this migration is a no-op.

ALTER TABLE experiment_runs
    DROP CONSTRAINT IF EXISTS experiment_runs_kind_check;

ALTER TABLE experiment_runs
    ADD CONSTRAINT experiment_runs_kind_check
    CHECK (kind IN ('walkforward', 'replay_experiment', 'replay_of_recording'));
