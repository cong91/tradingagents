"""Offline rendering of forecast deferral counts and recorded reasons."""

import html
import json
import os
import subprocess

import pytest

from tests.support.node_renderer import require_web_renderer
from tests.test_performance_ui_render import NODE_RENDERER

pytestmark = pytest.mark.unit


_RENDERER = NODE_RENDERER.replace(
    'const source = fs.readFileSync(filename, "utf8");',
    '''let source = fs.readFileSync(filename, "utf8");
    if (filename.endsWith("futures-cycle-funnel.tsx")) {
      source = source.replace("useState(false)", "useState(true)")
        .replace("useState<FuturesCycle[] | null>(null)",
                 "useState<FuturesCycle[] | null>(global.__testCycles)");
    }''',
).replace(
    "  return [name, renderToStaticMarkup(React.createElement(component, item.props))];",
    "  global.__testCycles = item.cycles;\n"
    "  return [name, renderToStaticMarkup(React.createElement(component, item.props))];",
)


def _case(*, reason, latest_reason, policy="defer", execution_status="not_started"):
    summary = dict.fromkeys((
        "market_rejected", "confirmation_rejected", "proposal_rejected", "entered",
        "awaiting_approval", "risk_rejected", "not_executed", "execution_failed",
        "in_flight", "interrupted", "other", "forecast_deferred",
    ), 0)
    summary.update(total=1, forecast_deferred=int(policy == "defer"))
    row = {
        **summary, "symbol": "BTC/USDT:USDT", "last_updated": "2026-11-12T00:00:00Z",
        "last_status": "rejected", "last_phase": "forecast",
        "last_execution_status": execution_status, "last_executed": "false",
        "last_execution_reason": None, "last_kronos_policy": policy,
        "last_kronos_defer_reason": latest_reason, "last_signal": None,
        "last_signal_reason": None, "last_market_rejection_reason": None,
        "last_confirmation_reason": None,
    }
    cycle = {
        "cycle_id": "btc-1", "symbol": "BTC/USDT:USDT", "status": "rejected",
        "phase": "forecast", "created_at": "2026-11-12T00:00:00Z",
        "payload": {"execution_status": execution_status, "kronos_policy": policy,
                    "kronos_defer_reason": reason},
    }
    return {"component": "funnel", "props": {"summary": summary, "symbols": [row]},
            "cycles": [cycle]}


@pytest.mark.parametrize(
    ("locale", "label", "outcome", "breakdown", "phase"),
    [
        ("en", "Deferred after Kronos forecast failure", "analysis did not run",
         "1 forecast deferred", "Kronos forecast"),
        ("vi", "Hoãn vì dự báo Kronos lỗi", "chưa phân tích",
         "1 hoãn vì dự báo lỗi", "Dự báo Kronos"),
    ],
)
def test_forecast_deferral_renders_counts_and_both_recorded_reasons(
    locale, label, outcome, breakdown, phase,
):
    web_root = require_web_renderer()
    cases = {
        "deferred": _case(reason="cycle-specific reason", latest_reason="latest-specific reason"),
        "no_reason": _case(reason=None, latest_reason=None),
        "not_deferred": _case(reason="must not display", latest_reason="must not display", policy=None),
        "contradiction": _case(reason="cycle reason", latest_reason="latest reason",
                               execution_status="awaiting_approval"),
    }
    env = os.environ.copy()
    env.update({"TZ": "UTC", "NODE_ENV": "production", "RENDER_LOCALE": locale})
    result = subprocess.run(
        ["node", "-e", _RENDERER], cwd=web_root, input=json.dumps(cases),
        capture_output=True, encoding="utf-8", env=env, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    rendered = {name: html.unescape(value) for name, value in json.loads(result.stdout).items()}
    page = rendered["deferred"]
    assert label in page
    assert breakdown in page
    assert phase in page
    assert page.count(outcome) >= 2
    assert "cycle-specific reason" in page
    assert "latest-specific reason" in page
    assert rendered["no_reason"].count(outcome) >= 2
    assert "must not display" not in rendered["not_deferred"]
    assert "cycle reason" in rendered["contradiction"]
    assert "latest reason" in rendered["contradiction"]
    assert rendered["contradiction"].count(outcome) >= 2
