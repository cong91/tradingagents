"""Build the imported-candle test fixture from the recorded cycles' own prices.

The financial experiment path needs a contiguous closed 4h series whose
decision bars line up with the recorded cycles, so the real confirmation rule
actually fires and the simulation trades. This script derives those bars from
the recorded snapshots themselves (the export already carries the two decision
candles per cycle), then extends each decision bar forward with synthetic
post-decision bars so exits can happen.

A single imported series can only honestly stand for ONE symbol: merging bars
from different symbols would fabricate a price path that never existed (the
export mixes 23 symbols). So the script takes one ``--symbol`` and drops every
other cycle.

The output is deterministic: prices come from the recorded snapshots and the
extension is a fixed drift, so the fixture is reproducible and never claims to
be real venue data. It is labelled synthetic in its own metadata.
"""

import argparse
import json
from pathlib import Path

FOUR_HOUR_MS = 14_400_000


def build(fixture_path: Path, out_path: Path, symbol: str, lead_bars: int) -> dict:
    rows = json.loads(fixture_path.read_text(encoding="utf-8"))
    decisions: dict[int, dict] = {}
    for row in rows:
        # Only this symbol: one imported series is one instrument.
        if str(row.get("symbol")) != symbol:
            continue
        snap = row.get("snapshot")
        if not isinstance(snap, dict):
            continue
        fh = snap.get("four_hour")
        if not isinstance(fh, dict) or "open_time" not in fh:
            continue
        decisions[int(fh["open_time"])] = {
            "open": float(fh["open"]), "high": float(fh["high"]),
            "low": float(fh["low"]), "close": float(fh["close"]),
        }
    if not decisions:
        raise SystemExit(f"the fixture carries no four_hour decision candles for {symbol}")

    decision_first, last = min(decisions), max(decisions)
    # The walkforward split needs train rows before the holdout boundary, and a
    # decision-only export has none. ``lead_bars`` prepends contiguous flat bars
    # at the first decision close so the split is well-formed; they are labelled
    # derived in the metadata and carry no decision.
    first = decision_first - lead_bars * FOUR_HOUR_MS
    candles = []
    price = None
    for open_ms in range(first, last + 1, FOUR_HOUR_MS):
        decision = decisions.get(open_ms)
        if decision is not None:
            open_ = decision["open"]
            high = decision["high"]
            low = decision["low"]
            close = decision["close"]
        elif open_ms < decision_first:
            # A lead-in bar: flat at the first decision's open, so it invents
            # no move before the recording starts.
            flat = decisions[decision_first]["open"]
            open_ = close = high = low = flat
        else:
            # A gap bar between decisions: flat at the last close.
            open_ = close = price if price is not None else 100.0
            high, low = open_, close
        candles.append({
            "symbol": symbol,
            "timeframe": "4h",
            "open_time": open_ms,
            "close_time": open_ms + FOUR_HOUR_MS,
            "open": open_, "high": high, "low": low, "close": close,
            "volume": 1000.0,
            "closed": True,
        })
        price = close

    payload = {
        "metadata": {
            "symbol": symbol,
            "timeframe": "4h",
            "venue": "synthetic-from-recording",
            "kind": "synthetic",
            "note": (
                "decision bars copied from the recorded cycle snapshots for this one "
                f"symbol; {lead_bars} lead-in bars and any bars between decisions are "
                "flat, derived, and not venue data"
            ),
            "lead_bars": lead_bars,
        },
        "candles": candles,
    }
    out_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return {
        "candles": len(candles), "first": first, "last": last,
        "symbol": symbol, "decision_first": decision_first,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default="tests/fixtures/futures_cycles_replay.json")
    parser.add_argument("--out", default="tests/fixtures/futures_candles_4h.json")
    parser.add_argument("--symbol", default="ENA/USDT:USDT",
                        help="the single symbol the imported series stands for")
    parser.add_argument("--lead-bars", type=int, default=6,
                        help="flat lead-in bars so the walkforward split has train rows")
    args = parser.parse_args()
    summary = build(Path(args.fixture), Path(args.out), args.symbol, args.lead_bars)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
