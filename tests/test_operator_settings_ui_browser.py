"""Run the real operator panes in an isolated browser with a fake API."""

import functools
import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from tests.support.node_renderer import require_web_renderer

LOADER = r"""
const ts = require(process.env.OPERATOR_TYPESCRIPT);
module.exports = function(source) {
  return ts.transpileModule(source, {
    fileName: this.resourcePath,
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  }).outputText;
};
"""

BUNDLE = r"""
const path = require("node:path");
const webpack = require("next/dist/compiled/webpack/webpack").webpack;
const root = process.cwd();
const dir = process.env.OPERATOR_FIXTURE;
webpack({
  mode: "development",
  entry: path.join(dir, "entry.js"),
  output: { path: dir, filename: "bundle.js" },
  resolve: {
    extensions: [".tsx", ".ts", ".js"],
    alias: { "@": root },
    modules: [path.join(root, "node_modules"), "node_modules"],
  },
  module: { rules: [{ test: /\.tsx?$/, use: path.join(dir, "loader.cjs") }] },
}, (error, stats) => {
  if (error || stats.hasErrors()) {
    console.error(error || stats.toString({ all: false, errors: true }));
    process.exitCode = 1;
  }
});
"""

ENTRY = r"""
const React = require("react");
const { createRoot } = require("react-dom/client");
const { useOperatorConfig } = require(process.env.OPERATOR_CONTROLLER);
const { OperatorModes, OperatorRisk } = require(process.env.OPERATOR_CARD);
const { setLocale } = require("@/lib/i18n/locale");
const settingsMessages = require("@/lib/i18n/messages/settings");
const row = (key, type, gate, value, override = false) => ({
  key, type, gate, group: gate ? "gates" : "risk", value, default: value,
  label: key, hint: key, override, stored_value: value, updated_at: null, updated_by: null,
});
let payload = {
  generated_at: "2026-11-09T12:00:00Z",
  settings: [
    row("auto_spot_enabled", "bool", true, false),
    row("auto_futures_enabled", "bool", true, false),
    row("risk_max_open_heat_pct", "float", false, 6, true),
    row("risk_max_derivatives_leverage", "float", false, 3, true),
    { ...row("risk_derivatives_break_even_r", "float", false, 1), default: 0.5 },
    row("risk_derivatives_max_hold_hours", "float", false, 72),
    row("risk_derivatives_time_stop_postpone_minutes", "float", false, 90),
    row("risk_derivatives_scale_out_enabled", "bool", false, true),
    row("risk_derivatives_override_min_r", "float", false, 2),
    row("risk_unknown_limit", "float", false, 4),
    { ...row("risk_no_hint", "float", false, 4), hint: "" },
  ],
  allowlist: ["auto_spot_enabled", "auto_futures_enabled", "risk_max_open_heat_pct", "risk_max_derivatives_leverage", "risk_derivatives_break_even_r", "risk_derivatives_override_min_r"],
  gates: { auto_spot: false, auto_futures: false, any_auto_enabled: false },
  effective_exec_mode: "dry",
};
let gets = 0;
let deletes = 0;
let writes = [];
let resolvePut;
class FakeEventSource {
  constructor(url) {
    if (url !== "/api/settings/events") throw new Error("Unexpected SSE");
    this.listeners = {};
    window.fakeEvents = this;
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  emit(name) { this.listeners[name](); }
  close() {}
}
window.EventSource = FakeEventSource;
window.fetch = (url, init = {}) => {
  if (url !== "/api/operator-config") throw new Error("Unexpected request: " + url);
  if (!init.method) {
    gets++;
    return Promise.resolve({ ok: true, json: async () => structuredClone(payload) });
  }
  if (init.method === "DELETE") { deletes++; throw new Error("Unexpected delete"); }
  if (init.method !== "PUT") throw new Error("Unexpected method: " + init.method);
  const body = JSON.parse(init.body);
  writes.push(body);
  return new Promise((resolve) => {
    resolvePut = () => {
      payload = { ...payload, settings: payload.settings.map((item) => ({
        ...item, value: body.settings[item.key] ?? item.value,
      })) };
      resolve({ ok: true, json: async () => structuredClone(payload) });
    };
  });
};

function TestPage() {
  const controller = useOperatorConfig();
  const [pane, setPane] = React.useState("modes");
  return React.createElement(React.Fragment, null,
    React.createElement("button", { id: "show-modes", onClick: () => setPane("modes") }, "Modes"),
    React.createElement("button", { id: "show-risk", onClick: () => setPane("risk") }, "Risk"),
    React.createElement("output", { id: "dirty" }, String(controller.dirtyCount)),
    pane === "modes"
      ? React.createElement(OperatorModes, { controller })
      : React.createElement(OperatorRisk, { controller })
  );
}
createRoot(document.getElementById("app")).render(React.createElement(TestPage));

const wait = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(test, message) {
  for (let i = 0; i < 150; i++) {
    if (test()) return;
    await wait();
  }
  throw new Error(message);
}
function check(condition, message) { if (!condition) throw new Error(message); }
function value(id, next) {
  const field = document.getElementById(id);
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(field, next);
  field.dispatchEvent(new Event("input", { bubbles: true }));
}
function button(text) {
  return [...document.querySelectorAll("button")].find((element) => element.textContent.trim().startsWith(text));
}
function checkField(key, messages, expectedUnit) {
  const id = "operator-" + key;
  const field = document.getElementById(id);
  const meta = messages.settingMeta[key];
  const label = meta?.label ?? key;
  const hint = meta?.hint ?? (key === "risk_no_hint" ? "" : key);
  const describedBy = (field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
  check([...field.labels].some((item) => item.textContent.trim() === label), key + " full label");
  check(describedBy.every((item) => document.getElementById(item)), key + " descriptions exist");
  check(describedBy.includes(id + "-hint") === Boolean(hint), key + " hint relationship");
  if (hint) check(document.getElementById(id + "-hint").textContent === hint, key + " full hint");
  check(describedBy.includes(id + "-unit") === Boolean(expectedUnit), key + " unit relationship");
  if (expectedUnit) {
    const unit = document.getElementById(id + "-unit");
    check(unit.textContent === expectedUnit, key + " unit text");
    check(unit.parentElement === field.parentElement && field.nextElementSibling === unit, key + " unit beside input");
  } else {
    check(!document.getElementById(id + "-unit"), key + " has no invented unit");
  }
  return describedBy;
}

(async () => {
  await until(() => document.getElementById("operator-auto_spot_enabled"), "initial fetch");
  check(gets === 1, "one GET");
  const gate = document.getElementById("operator-auto_spot_enabled");
  check(gate.getAttribute("aria-label") === settingsMessages.en.operator.draftSwitchAria(settingsMessages.en.gateLabels.auto_spot_enabled), "gate accessible label");
  check(gate.getAttribute("aria-describedby") === gate.id + "-status", "gate status preserved");
  document.getElementById("operator-auto_spot_enabled").click();
  await until(() => document.getElementById("operator-gate-reason"), "gate confirmation");
  check(document.body.textContent.includes("The server configuration changed") === false, "no conflict initially");
  document.querySelector('input[type="checkbox"]:not([role])').click();
  value("operator-gate-reason", "approved test");
  await until(() => !button("Save").disabled, "gate confirmation enabled");
  document.getElementById("show-risk").click();
  await until(() => document.getElementById("operator-risk_max_open_heat_pct"), "risk pane");
  check(document.getElementById("dirty").textContent === "1", "dirty across panes");
  const protection = document.getElementById("operator-risk_derivatives_break_even_r").closest("details");
  const advanced = document.getElementById("operator-risk_derivatives_override_min_r").closest("details");
  check(!protection.open && !advanced.open, "advanced groups start collapsed");
  checkField("risk_max_open_heat_pct", settingsMessages.en, "%");
  checkField("risk_max_derivatives_leverage", settingsMessages.en, "x");
  checkField("risk_derivatives_override_min_r", settingsMessages.en, null);
  checkField("risk_unknown_limit", settingsMessages.en, null);
  checkField("risk_no_hint", settingsMessages.en, null);
  check(document.getElementById("operator-risk_max_open_heat_pct").closest("li").textContent.includes("overridden"), "overridden badge");
  protection.querySelector("summary").click();
  check(protection.open, "protection can be opened");
  checkField("risk_derivatives_break_even_r", settingsMessages.en, "R");
  checkField("risk_derivatives_max_hold_hours", settingsMessages.en, "hours");
  checkField("risk_derivatives_time_stop_postpone_minutes", settingsMessages.en, "minutes");
  checkField("risk_derivatives_scale_out_enabled", settingsMessages.en, null);
  const nonOverride = document.getElementById("operator-risk_derivatives_break_even_r").closest("li");
  check(nonOverride.textContent.includes("no override"), "no-override badge despite effective non-default");
  check(nonOverride.textContent.includes("Effective: 1 · Default: 0.5"), "effective and default distinct");
  setLocale("vi", { persist: false });
  await until(() => document.getElementById("operator-risk_derivatives_max_hold_hours-unit").textContent === "giờ", "Vietnamese units");
  checkField("risk_max_open_heat_pct", settingsMessages.vi, "%");
  checkField("risk_max_derivatives_leverage", settingsMessages.vi, "x");
  checkField("risk_derivatives_break_even_r", settingsMessages.vi, "R");
  checkField("risk_derivatives_max_hold_hours", settingsMessages.vi, "giờ");
  checkField("risk_derivatives_time_stop_postpone_minutes", settingsMessages.vi, "phút");
  check(nonOverride.textContent.includes("không có override"), "localized no-override badge");
  setLocale("en", { persist: false });
  await until(() => document.getElementById("operator-risk_derivatives_max_hold_hours-unit").textContent === "hours", "English units restored");
  value("operator-risk_derivatives_break_even_r", "2");
  await until(() => document.getElementById("dirty").textContent === "2", "protection draft");
  document.getElementById("show-modes").click();
  await until(() => document.getElementById("operator-auto_spot_enabled"), "modes during protection draft");
  document.getElementById("show-risk").click();
  await until(() => document.getElementById("operator-risk_derivatives_break_even_r"), "risk after protection draft");
  await until(() => document.getElementById("operator-risk_derivatives_break_even_r").closest("details").open, "dirty protection reopens");
  check(!document.getElementById("operator-risk_derivatives_override_min_r").closest("details").open, "clean advanced remains closed");
  button("Undo").click();
  await until(() => document.getElementById("dirty").textContent === "1", "risk undo keeps gate draft");
  document.getElementById("show-modes").click();
  await until(() => document.getElementById("operator-gate-reason"), "return to confirmation");
  document.querySelector('input[type="checkbox"]:not([role])').click();
  value("operator-gate-reason", "approved test");
  await until(() => !button("Save").disabled, "confirm again after undo");
  document.getElementById("show-risk").click();
  await until(() => document.getElementById("operator-risk_max_open_heat_pct"), "return to risk");
  check(document.querySelector('button[aria-label^="Remove override"]').disabled, "reset blocked by gate draft");
  value("operator-risk_max_open_heat_pct", "-1");
  await until(() => document.getElementById("operator-risk_max_open_heat_pct").getAttribute("aria-invalid") === "true", "numeric validation");
  check(button("Save").disabled, "invalid number cannot save");
  check(document.getElementById("operator-risk_max_open_heat_pct-error").getAttribute("role") === "alert", "field error announced");
  check(checkField("risk_max_open_heat_pct", settingsMessages.en, "%").includes("operator-risk_max_open_heat_pct-error"), "error and other descriptions coexist");
  value("operator-risk_max_open_heat_pct", "2.75");
  await until(() => !button("Save").disabled, "valid number enables save");
  check(!checkField("risk_max_open_heat_pct", settingsMessages.en, "%").includes("operator-risk_max_open_heat_pct-error"), "error description clears after correction");
  button("Save").click();
  await until(() => writes.length === 1, "risk PUT");
  check(writes[0].settings.risk_max_open_heat_pct === 2.75, "numeric PUT");
  check(Object.keys(writes[0].settings).length === 1, "gate not sent with risk");
  check(document.getElementById("operator-risk_max_open_heat_pct").disabled, "risk locked during save");
  document.getElementById("show-modes").click();
  await until(() => document.getElementById("operator-auto_spot_enabled"), "return to modes");
  check(document.getElementById("operator-auto_spot_enabled").disabled, "gate locked during save");
  check(document.getElementById("operator-gate-reason").disabled, "reason locked during save");
  resolvePut();
  await until(() => !document.getElementById("operator-auto_spot_enabled").disabled, "save complete");
  check(document.getElementById("operator-auto_spot_enabled").checked, "gate draft preserved");
  check(document.getElementById("operator-gate-reason").value === "approved test", "gate reason preserved");
  check(document.getElementById("dirty").textContent === "1", "risk saved, gate still dirty");
  document.getElementById("operator-auto_futures_enabled").click();
  await until(() => document.getElementById("operator-gate-reason").value === "", "gate change clears reason");
  check(!document.querySelector('input[type="checkbox"]:not([role])').checked, "gate change clears confirmation");
  button("Undo").click();
  await until(() => document.getElementById("dirty").textContent === "0", "undo cleared modes");
  check(gets === 1 && deletes === 0, "no extra fetch or delete");

  document.getElementById("operator-auto_spot_enabled").click();
  await until(() => document.getElementById("operator-gate-reason"), "new gate draft");
  document.querySelector('input[type="checkbox"]:not([role])').click();
  value("operator-gate-reason", "review external change");
  await until(() => !button("Save").disabled, "confirmation before external update");
  payload = { ...payload, settings: payload.settings.map((item) => item.key === "risk_max_open_heat_pct" ? { ...item, value: 5 } : item) };
  window.fakeEvents.emit("changed");
  await until(() => document.body.textContent.includes("The server configuration changed"), "conflict warning");
  check(button("Save").disabled, "conflict blocks save");
  check(document.getElementById("operator-auto_spot_enabled").checked, "conflict preserves draft");
  button("Load latest data and keep draft").click();
  await until(() => !document.body.textContent.includes("The server configuration changed"), "latest snapshot reviewed");
  check(document.getElementById("operator-auto_spot_enabled").checked, "draft retained after review");
  check(document.getElementById("operator-gate-reason").value === "", "review clears confirmation reason");
  check(!document.querySelector('input[type="checkbox"]:not([role])').checked, "review clears confirmation");
  check(gets === 2 && deletes === 0, "SSE triggers only a fake GET");
  document.body.dataset.testResult = "pass";
})().catch((error) => {
  document.body.dataset.testResult = "fail";
  document.getElementById("result").textContent = error.stack;
});
"""


def _browser():
    candidates = [
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        shutil.which("msedge"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google/Chrome/Application/chrome.exe"),
        os.path.join(
            os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft/Edge/Application/msedge.exe"
        ),
    ]
    return next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)


@pytest.mark.unit
def test_operator_panes_in_isolated_browser(tmp_path):
    web_root = require_web_renderer()
    browser = _browser()
    if browser is None:
        pytest.skip("No Chrome, Chromium or Edge browser available")

    (tmp_path / "loader.cjs").write_text(LOADER, encoding="utf-8")
    (tmp_path / "entry.js").write_text(ENTRY, encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body><div id="app"></div>'
        '<pre id="result"></pre><script>window.onerror=function(m,s,l,c,e){'
        'document.body.dataset.testResult="fail";document.getElementById("result").textContent='
        'String(m)+" "+String(e&&e.stack);};</script>'
        '<script src="/bundle.js"></script></body></html>',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "OPERATOR_FIXTURE": str(tmp_path),
        "OPERATOR_TYPESCRIPT": str(web_root / "node_modules/typescript"),
        "OPERATOR_CONTROLLER": str(web_root / "components/settings/operator-config-controller.ts"),
        "OPERATOR_CARD": str(web_root / "components/settings/operator-config-card.tsx"),
    })
    # Webpack substitutes these module paths at build time, not from the browser environment.
    entry = (tmp_path / "entry.js").read_text(encoding="utf-8")
    entry = entry.replace("process.env.OPERATOR_CONTROLLER", repr(env["OPERATOR_CONTROLLER"]))
    entry = entry.replace("process.env.OPERATOR_CARD", repr(env["OPERATOR_CARD"]))
    (tmp_path / "entry.js").write_text(entry, encoding="utf-8")
    built = subprocess.run(
        ["node", "-e", BUNDLE], cwd=web_root, env=env, capture_output=True,
        check=False, encoding="utf-8", timeout=120,
    )
    assert built.returncode == 0, built.stderr

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                browser, "--headless=new", "--disable-gpu", "--disable-background-networking",
                "--disable-extensions", "--disable-sync", "--no-first-run", "--no-default-browser-check",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=10000",
                "--dump-dom", f"http://127.0.0.1:{server.server_port}/index.html",
            ],
            capture_output=True, check=False, encoding="utf-8", errors="replace", timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.returncode == 0, result.stderr
    assert 'data-test-result="pass"' in result.stdout, result.stdout + result.stderr
