"""Ad-hoc replay run: baseline vs counterfactual over the exported snapshot."""
import json
import sys

from server import futures_replay as fr

cycles = fr.load_cycles()
report = {"loaded": len(cycles)}
for name, rule in fr.RULES.items():
    outs = fr.replay_rule(cycles, rule)
    s = fr.summary(outs)
    report[name] = {
        "total": s["total"],
        "confirmed": s["confirmed_by_rule"],
        "rejected": s["rejected_by_rule"],
        "by_signal": s["by_signal"],
        "by_1h": s["by_one_hour_direction"],
        "by_4h": s["by_four_hour_direction"],
        "rule_reasons": s["rule_reasons"],
        "recorded_reasons": s["recorded_reasons"],
        "flips_to_confirm": s["flips_to_confirm"],
        "flips_to_reject": s["flips_to_reject"],
    }
with open("tests/fixtures/replay_report.json", "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2, default=str)
for key in ("baseline", "htf_dominance_bounded_pullback"):
    entry = report[key]
    print(f"== {key}: total={entry['total']} confirmed={entry['confirmed']} rejected={entry['rejected']}")
    print("   signals:", entry["by_signal"])
    print("   1h:", entry["by_1h"], "| 4h:", entry["by_4h"])
    print("   rule_reasons:", entry["rule_reasons"])
    print("   flips_to_confirm:", len(entry["flips_to_confirm"]), "| flips_to_reject:", len(entry["flips_to_reject"]))
sys.stdout.flush()
