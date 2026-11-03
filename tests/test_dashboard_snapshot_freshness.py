"""Dashboard timestamp classification without a browser or live APIs."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"

NODE_CHECK = r"""
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const webRoot = process.cwd();
const ts = require(require.resolve("typescript", { paths: [webRoot] }));
function compile(module, filename) {
  const source = fs.readFileSync(filename, "utf8");
  const result = ts.transpileModule(source, {
    fileName: filename,
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  });
  module._compile(result.outputText, filename);
}
Module._extensions[".ts"] = compile;
Module._extensions[".tsx"] = compile;
const resolveFilename = Module._resolveFilename;
Module._resolveFilename = function (request, parent, isMain, options) {
  return resolveFilename.call(this, request.startsWith("@/")
    ? path.join(webRoot, request.slice(2)) : request, parent, isMain, options);
};
const { portfolioTimestamp, isFuturesStale } = require(
  path.join(webRoot, "components/overview/dashboard.tsx")
);
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map(({ state, portfolioStale, clock }) => ({
  spotTimestamp: portfolioTimestamp(state.data),
  futuresStale: isFuturesStale(state, portfolioStale, clock),
}))));
"""


@pytest.mark.unit
def test_dashboard_keeps_spot_and_futures_timestamp_status_separate():
    clock = 1_700_000_000_000
    generated_at = "2023-11-14T22:13:20.000Z"

    def case(observed_at, *, status="ready", futures_status="ok", spot_stale=False, snapshot=None):
        return {
            "clock": clock,
            "portfolioStale": spot_stale,
            "state": {
                "status": status,
                "data": {
                    "generated_at": generated_at,
                    "snapshot": snapshot,
                    "futures": {
                        "status": futures_status,
                        "positions_read": {"observed_at": observed_at},
                    },
                },
            },
        }

    cases = [
        case("2023-11-14T22:12:50.000Z"),  # exactly 30 seconds old
        case("2023-11-14T22:12:49.999Z"),
        case("2023-11-14T22:13:25.000Z"),  # exactly 5 seconds ahead
        case("2023-11-14T22:13:25.001Z"),
        case(None),
        case("not-a-timestamp"),
        case("2023-11-14T22:13:20.000Z", status="stale"),
        case("2023-11-14T22:13:20.000Z", spot_stale=True),
        case(None, futures_status="error"),
        case("2023-11-14T22:13:20.000Z", snapshot={"dirty": True}),
    ]
    result = subprocess.run(
        ["node", "-e", NODE_CHECK],
        cwd=WEB_ROOT,
        input=json.dumps(cases),
        capture_output=True,
        check=False,
        encoding="utf-8",
        env={**os.environ, "TZ": "UTC", "NODE_ENV": "production"},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert [item["futuresStale"] for item in values] == [
        False, True, False, True, True, True, True, True, True, False,
    ]
    assert all(item["spotTimestamp"] == clock for item in values[:-1])
    assert values[-1]["spotTimestamp"] is None
