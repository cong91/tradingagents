"""Exercise mounted LLM settings with real React in an isolated offline browser."""

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
const ts = require(process.env.LLM_TYPESCRIPT);
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
const dir = process.env.LLM_FIXTURE;
webpack({
  mode: "development",
  entry: path.join(dir, "entry.js"),
  output: {path: dir, filename: "bundle.js"},
  resolve: {extensions: [".tsx", ".ts", ".js"], alias: {"@": root},
    modules: [path.join(root, "node_modules"), "node_modules"]},
  module: {rules: [{test: /\.tsx?$/, use: path.join(dir, "loader.cjs")}]},
}, (error, stats) => {
  if (error || stats.hasErrors()) {
    console.error(error || stats.toString({all: false, errors: true}));
    process.exitCode = 1;
  }
});
"""

ENTRY = r"""
const React = require("react");
const {createRoot} = require("react-dom/client");
const {useSettings} = require(SETTINGS_MODULE);
const {LlmConfigCard} = require(CARD_MODULE);
const row = (key, value) => ({key, value, type: "str|null", default: null,
  env_var: key.toUpperCase(), editable: true, source: "default"});
const settings = {generated_at: "now", settings: [row("llm_provider", "openai"),
  row("quick_think_provider", ""), row("deep_think_provider", ""),
  row("quick_think_llm", "custom-model"), row("deep_think_llm", "deep-model"),
  row("backend_url", ""), row("quick_think_backend_url", ""),
  row("deep_think_backend_url", ""), row("openai_reasoning_effort", ""),
  row("google_thinking_level", ""), row("anthropic_effort", "")],
  credentials: [], gates_readonly: {}, effective_exec_mode: "dry"};
const credential = {supported: true, required: true, configured: false, source: "none",
  masked: null, last4: null, env_var: "EXAMPLE_API_KEY"};
const provider = (id) => ({id, label: id.toUpperCase(), auth_mode: "api_key",
  requires_backend_url: false, default_backend_url: null,
  credential: {quick: credential, deep: credential},
  models: {quick: [{id: id + "-model", label: id + " model"}],
    deep: [{id: id + "-model", label: id + " model"}]}});
const catalog = {generated_at: "now", providers: {openai: provider("openai"), google: provider("google")},
  tiers: {quick: {provider: "openai"}, deep: {provider: "openai"}}};
let failSettings = false;
let getSettingsCount = 0;
window.fetch = async (url, init = {}) => {
  if (url === "/api/settings" && !init.method) {
    getSettingsCount++;
    if (failSettings) throw new Error("offline");
    return {ok: true, json: async () => structuredClone(settings)};
  }
  if (url === "/api/llm-config" && !init.method) {
    return {ok: true, json: async () => structuredClone(catalog)};
  }
  throw new Error("Unexpected request: " + url);
};
function TestPage() {
  const controller = useSettings();
  const [dirty, setDirty] = React.useState(false);
  const [show, setShow] = React.useState(true);
  return React.createElement(React.Fragment, null,
    React.createElement("button", {id: "reload", onClick: () => {failSettings = true; void controller.reload();}}, "Reload"),
    React.createElement("button", {id: "toggle", onClick: () => setShow((value) => !value)}, "Toggle"),
    React.createElement("output", {id: "dirty"}, String(dirty)),
    React.createElement("output", {id: "phase"}, controller.phase),
    React.createElement("section", {id: "ai", hidden: !show}, controller.payload &&
      React.createElement(LlmConfigCard, {controller, onDirtyChange: setDirty}))
  );
}
createRoot(document.getElementById("app")).render(React.createElement(TestPage));
const wait = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate, message) {
  for (let i = 0; i < 200; i++) { if (predicate()) return; await wait(); }
  throw new Error(message);
}
function check(condition, message) { if (!condition) throw new Error(message); }
function input(id, value) {
  const element = document.getElementById(id);
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(element, value);
  element.dispatchEvent(new Event("input", {bubbles: true}));
}
function select(id, value) {
  const element = document.getElementById(id);
  element.value = value;
  element.dispatchEvent(new Event("change", {bubbles: true}));
}
(async () => {
  await until(() => document.getElementById("llm-quick-api-key"), "initial settings and catalog");
  const cardKey = document.getElementById("llm-quick-api-key");
  input("llm-quick-api-key", "unsaved-key");
  await until(() => document.getElementById("dirty").textContent === "true", "draft propagated");
  document.getElementById("toggle").click();
  await until(() => document.getElementById("ai").hidden, "AI hidden");
  check(document.getElementById("llm-quick-api-key") === cardKey, "component remains mounted");
  document.getElementById("reload").click();
  await until(() => getSettingsCount >= 2, "failed settings refresh");
  await wait();
  check(document.getElementById("phase").textContent === "ready", "settings payload retained");
  check(document.getElementById("dirty").textContent === "true", "dirty callback preserved");
  document.getElementById("toggle").click();
  await until(() => !document.getElementById("ai").hidden, "AI visible again");
  check(document.getElementById("llm-quick-api-key").value === "unsaved-key", "key preserved");
  const details = document.querySelector("details");
  check(!details.open, "advanced starts collapsed");
  details.querySelector("summary").click();
  check(details.open, "native disclosure opens");
  check(document.getElementById("llm-quick-backend-url"), "inherited tier URL accessible");
  select("llm-quick-provider", "google");
  await until(() => document.getElementById("llm-quick-api-key").value === "", "separate provider key");
  check(document.body.textContent.includes("provider selection is not saved yet"), "provider draft warning");
  select("llm-quick-provider", "");
  await until(() => document.getElementById("llm-quick-api-key").value === "unsaved-key", "draft restored");
  input("llm-quick-api-key", "");
  await until(() => document.getElementById("dirty").textContent === "false", "dirty cleared");
  document.body.dataset.testResult = "pass";
})().catch((error) => {
  document.body.dataset.testResult = "fail";
  document.getElementById("result").textContent = error.stack;
});
"""


def _browser():
    candidates = [
        shutil.which("chromium"), shutil.which("google-chrome"), shutil.which("chrome"),
        shutil.which("msedge"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google/Chrome/Application/chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft/Edge/Application/msedge.exe"),
    ]
    return next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)


@pytest.mark.unit
def test_llm_draft_survives_hidden_parent_and_failed_refresh(tmp_path):
    web_root = require_web_renderer()
    browser = _browser()
    if browser is None:
        pytest.skip("No Chrome, Chromium or Edge browser available")
    (tmp_path / "loader.cjs").write_text(LOADER, encoding="utf-8")
    entry = ENTRY.replace(
        "SETTINGS_MODULE", repr(str(web_root / "components/settings/use-settings.ts"))
    ).replace("CARD_MODULE", repr(str(web_root / "components/settings/llm-config-card.tsx")))
    (tmp_path / "entry.js").write_text(entry, encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body><div id="app"></div>'
        '<pre id="result"></pre><script>window.onerror=function(m,s,l,c,e){'
        'document.body.dataset.testResult="fail";document.getElementById("result").textContent='
        'String(m)+" "+String(e&&e.stack);};</script>'
        '<script src="/bundle.js"></script></body></html>',
        encoding="utf-8",
    )
    env = {**os.environ, "LLM_FIXTURE": str(tmp_path),
           "LLM_TYPESCRIPT": str(web_root / "node_modules/typescript")}
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
            [browser, "--headless=new", "--disable-gpu", "--disable-background-networking",
             "--disable-extensions", "--disable-sync", "--no-first-run", "--no-default-browser-check",
             "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
             f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=10000",
             "--dump-dom", f"http://127.0.0.1:{server.server_port}/index.html"],
            capture_output=True, check=False, encoding="utf-8", errors="replace", timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.returncode == 0, result.stderr
    assert 'data-test-result="pass"' in result.stdout, result.stdout + result.stderr
