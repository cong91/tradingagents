"""Exercise the real data-source card in an offline browser with mocked fetch."""

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
const ts = require(process.env.DATA_SOURCE_TYPESCRIPT);
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
const dir = process.env.DATA_SOURCE_FIXTURE;
webpack({
  mode: "development",
  entry: path.join(dir, "entry.js"),
  output: { path: dir, filename: "bundle.js" },
  resolve: { extensions: [".tsx", ".ts", ".js"], alias: { "@": root },
    modules: [path.join(root, "node_modules"), "node_modules"] },
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
const { DataSourceCard } = require(CARD_MODULE);
const initial = {
  generated_at: "now", key_path: "test-key-path", providers: ["reddit", "x"].map((provider) => ({
    provider, configured: true, decryptable: true, valid: true, cookie_count: 1,
    cookie_names: ["session"], updated_at: null, updated_by: null,
  })),
};
const unconfigured = (provider) => ({ ...initial.providers.find((item) => item.provider === provider),
  configured: false, valid: false, cookie_count: 0, cookie_names: [] });
let deleteResponse;
const requests = [];
const prompts = [];
let confirmResult = false;
window.confirm = (message) => { prompts.push(message); return confirmResult; };
window.fetch = async (url, init = {}) => {
  const method = init.method || "GET";
  requests.push({ url, method, body: init.body });
  if (url === "/api/data-sources" && method === "GET") {
    return { ok: true, json: async () => structuredClone(initial) };
  }
  if (method === "DELETE" && ["/api/data-sources/reddit", "/api/data-sources/x"].includes(url)) {
    return { ok: true, json: async () => structuredClone(deleteResponse) };
  }
  throw new Error("Unexpected request");
};
function TestPage() {
  const [dirty, setDirty] = React.useState(false);
  return React.createElement(React.Fragment, null,
    React.createElement("output", { id: "dirty" }, String(dirty)),
    React.createElement(DataSourceCard, { onDirtyChange: setDirty }));
}
createRoot(document.getElementById("app")).render(React.createElement(TestPage));
const wait = () => new Promise((resolve) => setTimeout(resolve, 20));
async function until(predicate, message) {
  for (let i = 0; i < 150; i++) { if (predicate()) return; await wait(); }
  throw new Error(message);
}
function check(condition, message) { if (!condition) throw new Error(message); }
function field(index, value) {
  const element = document.querySelectorAll("textarea")[index];
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
}
function buttons(label) {
  return [...document.querySelectorAll("button")].filter((button) => button.textContent.trim() === label);
}
(async () => {
  await until(() => document.querySelectorAll("textarea").length === 2, "initial fetch");
  field(0, "synthetic-cookie-secret");
  field(1, "other-draft");
  await until(() => document.getElementById("dirty").textContent === "true", "draft state");
  buttons("Remove")[0].click();
  check(prompts[0].includes("Reddit") && prompts[0].includes("unsaved"), "named draft warning");
  check(requests.length === 1, "cancel did not send a DELETE");
  check(document.querySelectorAll("textarea")[0].value === "synthetic-cookie-secret", "cancel kept draft");

  confirmResult = true;
  deleteResponse = { ...initial, removed: false, providers: [unconfigured("reddit"), initial.providers[1]] };
  buttons("Remove")[0].click();
  await until(() => document.querySelector('[role="alert"]'), "unconfirmed result");
  check(document.querySelector('[role="alert"]').textContent.includes("Could not confirm"), "uncertain alert");
  check(document.querySelectorAll("textarea")[0].value === "synthetic-cookie-secret", "uncertain kept draft");
  check(buttons("Remove").length === 2, "uncertain kept snapshot");

  deleteResponse = { ...initial, providers: [unconfigured("reddit"), initial.providers[1]] };
  buttons("Remove")[0].click();
  await until(() => document.querySelector('[role="status"]')?.textContent.includes("Removed Reddit"), "confirmed removal");
  check(document.querySelectorAll("textarea")[0].value === "", "confirmed cleared target");
  check(document.querySelectorAll("textarea")[1].value === "other-draft", "other draft kept");
  check(document.getElementById("dirty").textContent === "true", "other draft remains dirty");
  check(buttons("Remove").length === 1, "only target status changed");

  field(1, "");
  await until(() => document.getElementById("dirty").textContent === "false", "all drafts cleared");
  confirmResult = false;
  buttons("Remove")[0].click();
  check(prompts.at(-1).includes("X (Twitter)") && !prompts.at(-1).includes("unsaved"), "named clean confirmation");
  check(requests.filter((request) => request.method === "DELETE").length === 2, "cancel sent no further DELETE");
  check(requests.filter((request) => request.method === "DELETE").every((request) => request.body === "{}"), "DELETE contains no cookies");
  document.body.dataset.testResult = "pass";
})().catch((error) => {
  document.body.dataset.testResult = "fail";
  document.getElementById("result").textContent = error.message;
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
def test_data_source_card_in_isolated_browser(tmp_path):
    web_root = require_web_renderer()
    browser = _browser()
    if browser is None:
        pytest.skip("No Chrome, Chromium or Edge browser available")
    (tmp_path / "loader.cjs").write_text(LOADER, encoding="utf-8")
    (tmp_path / "entry.js").write_text(
        ENTRY.replace("CARD_MODULE", repr(str(web_root / "components/settings/data-source-card.tsx"))),
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body><div id="app"></div>'
        '<pre id="result"></pre><script>window.onerror=function(m){'
        'document.body.dataset.testResult="fail";document.getElementById("result").textContent='
        'String(m);};</script><script src="/bundle.js"></script></body></html>',
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "DATA_SOURCE_FIXTURE": str(tmp_path),
        "DATA_SOURCE_TYPESCRIPT": str(web_root / "node_modules/typescript"),
    }
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
