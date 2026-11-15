# Debug: Variable Kronos history shortfall

**Opened:** 2026-11-15
**Status:** resolved
**Reporter:** user-reported production deferral

## Symptoms

- [ ] Active Kronos defers when venue returns fewer bars than the configured profile context.
- [ ] The observed 2047/2048 case is only an example; the available history can be any shorter length.
- [ ] The candidate must reach PM/LLM when the returned history is contiguous, closed, point-in-time, and within the model profile's supported length range.

## Eliminated Hypotheses

- [x] Switch to `small_base` as a blanket fallback: this changes the operator-selected model/tokenizer profile and hides the actual data-length contract.
- [x] Accept any short history: the sampler declares a `MIN_LOOKBACK_BARS` floor and still rejects gaps, forming bars, stale data, malformed candles, and out-of-profile lengths.

## Evidence Log

| Timestamp | Data | Interpretation |
|---|---|---|
| 2026-11-15 | `sampler.py` accepts `MIN_LOOKBACK_BARS <= len(candles) <= profile.max_context`. | Kronos supports variable-length context within the profile range. |
| 2026-11-15 | `series_problem()` rejects `len(candles) < configured lookback_bars`; paginated market data also rejects fewer than `limit`. | The active path enforces an exact configured length before the sampler can run. |
| 2026-11-15 | Existing `test_kronos_2k_history_defer.py` expects 2047/2048 to defer. | This test encodes the old strict contract and must change for the approved variable-length behavior; gap cases remain deferred. |

## Current Hypothesis

The root cause is an active-path exact-length requirement, not a model inability to process shorter context. The fix should let the active path return the complete available closed grid, derive an effective lookback from its length, and preserve the minimum/profile/gap/point-in-time gates.

## Fix Attempts

| # | Change | Result | Notes |
|---|---|---|---|
| 1 | Strict history and pre-inference retrospective guard | Existing full suite passed, but valid shorter histories still deferred before inference. | Guard is retained; history-length contract needs a separate change. |
| 2 | Active fetch accepts a contiguous closed suffix at or above `MIN_LOOKBACK_BARS`; effective length is passed into the selected profile's service settings. | Focused tests pass for 32, 201, 2047, and 2048 bars; strict generic pagination remains strict without `minimum_bars`. | No model-profile fallback, padding, interpolation, or look-ahead was added. |

## Resolution

**Root Cause:** The active path treated the configured profile context ceiling as an exact required history length in both paginated retrieval and service input validation.
**Fix:** `fetch_closed_candles(..., minimum_bars=MIN_LOOKBACK_BARS)` now permits any contiguous closed suffix that meets the mechanical sampler floor, and the active service is constructed with `lookback_bars=len(candles)`. The selected profile, point-in-time checks, gap checks, freshness checks, and retrospective guard remain enforced.
**Verification:** Focused variable-history tests pass, including arbitrary valid short contexts, below-floor rejection, gap defer before graph/LLM, and strict default pagination behavior. Full gates pass: `ruff check .`; `pytest -q` reports 3891 passed, 897 skipped, 25 deselected, 100 subtests passed; clean imports pass.
**Date:** 2026-11-15
