# Debug: Futures budget sizing and post-fill safety

**Opened:** 2026-11-14
**Status:** fixing
**Reporter:** self-detected through local review

## Symptoms

- [ ] Venue contract metadata is not carried into the planned order/live amount contract.
- [ ] Live post-fill safety can use stale equity and can close unresolved partial/unknown entries.
- [ ] A global 1% per-entry cap was added during review but is not part of the approved sizing contract.
- [ ] Simulation fill-time behavior can diverge from live fill-time safety.

## Eliminated Hypotheses

- [x] Full-suite failure is the root cause: the current full suite reached 4,723 passed; the remaining issues are behavioral safety gaps not exposed by existing tests.
- [x] PostgreSQL setup error is a product assertion failure: prior setup errors were test-port infrastructure collisions.

## Evidence Log

| Timestamp | Data | Interpretation |
|---|---|---|
| 2026-11-14 | `futures_account._normalize_position` multiplies venue contracts by `contractSize`; derivatives live amount sends `order.quantity` without carrying that metadata. | Planning/live sizing can use inconsistent units for non-unit contracts. |
| 2026-11-14 | `_refresh_live_risk_context` reads a fresh account but returns no portfolio; post-fill code calls `_equity(portfolio)`. | Post-fill risk can use stale equity or skip the check. |
| 2026-11-14 | `complete` is false for open/partial ACKs, but safety-close branch is unconditional and can use `bracket:*:unknown`. | Residual entry exposure can outlive the protection task. |
| 2026-11-14 | Monitor returns `manual_reconciliation_required` for `time_stop_*` statuses before BracketManager retry branch. | Transient close failure is not automatically retried. |

## Current Hypothesis

The safe contract requires a single explicit unit boundary: budget notional is base exposure, the venue amount is contracts derived from verified market metadata, and all post-fill/recovery decisions are allowed only after full fill identity, exact close evidence, and fresh flat-position evidence. Partial or unknown entries remain durable unresolved intents.

## Fix Attempts

| # | Change | Result | Notes |
|---|---|---|---|
| 1 | Added post-fill safety close path | Failed review | It ran for partial/unknown fills, used stale equity, and did not reconcile the independent close into entry intent. |
| 2 | Added fixed 1% per-entry cap | Rejected as unapproved contract | Remove from global sizing/RiskGuard; preserve account-level controls and override-specific cap. |
| 3 | Split fixes across contract/risk, live recovery, and simulation agents | In progress | Must merge only after focused red/green tests and cross-diff review. |

## Resolution

**Root Cause:** pending.
**Fix:** pending.
**Date:** pending.
