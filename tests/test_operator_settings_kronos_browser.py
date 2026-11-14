"""Production-browser coverage for the profile-only Kronos Settings surface."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from tests.browser.fixture_server import (
    build_fixture_root,
    start_production_server,
    stop_production_server,
    wait_for_server,
)
from tests.support.node_renderer import require_web_renderer

SETTINGS_FIXTURE_SCRIPT = r"""
(() => {
  window.__consoleErrors = [];
  const originalConsoleError = console.error;
  console.error = (...args) => {
    window.__consoleErrors.push(args.map(String).join(" "));
    originalConsoleError.apply(console, args);
  };
  window.addEventListener("error", event => window.__consoleErrors.push(String(event.message)));
  window.addEventListener("unhandledrejection", event => window.__consoleErrors.push(String(event.reason)));
  const auto = {auto_spot_enabled: true, auto_futures_enabled: false};
  const requests = [];
  const eventSources = new Set();
  const settingRows = [
    {key: "kronos_active_profile", env_var: "TRADINGAGENTS_KRONOS_ACTIVE_PROFILE",
      value: "small_base", default: "small_base", type: "str", editable: true, source: "default"},
    {key: "quick_think_llm", env_var: "TRADINGAGENTS_QUICK_THINK_LLM",
      value: "fixture-model", default: "fixture-model", type: "str", editable: true, source: "default"},
  ];
  const row = (key, value, type = "bool", gate = true, group = "gates") => ({
    key, type, gate, group, value, default: value, label: key, hint: key,
    override: value !== false, stored_value: value, updated_at: null, updated_by: null,
  });
  const operatorPayload = () => {
    const mini = settingRows.find(item => item.key === "kronos_active_profile")?.value === "mini_2k";
    return {
      generated_at: "2026-11-12T12:00:00Z",
      settings: [
        row("auto_spot_enabled", auto.auto_spot_enabled, "bool", true),
        row("auto_futures_enabled", auto.auto_futures_enabled, "bool", true),
      ],
      allowlist: ["auto_spot_enabled", "auto_futures_enabled"],
      gates: {auto_spot: auto.auto_spot_enabled, auto_futures: auto.auto_futures_enabled,
        any_auto_enabled: auto.auto_spot_enabled || auto.auto_futures_enabled},
      effective_exec_mode: "dry",
      kronos: {
        profile_id: mini ? "mini_2k" : "small_base", timeframe: "4h",
        horizon_hours: 24, lookback_bars: mini ? 2048 : 512,
        max_context: mini ? 2048 : 512, paths: 16, temperature: 0.5,
        readiness: {status: "unavailable", reason: "fixture model unavailable"},
        model: {
          source_revision: "source-revision", model_revision: "model-revision",
          repo: mini ? "NeoQuasar/Kronos-mini" : "NeoQuasar/Kronos-small",
          tokenizer_repo: mini ? "NeoQuasar/Kronos-Tokenizer-2k" : "NeoQuasar/Kronos-Tokenizer-base",
          tokenizer_revision: "tokenizer-revision",
        },
      },
    };
  };
  const settingsPayload = () => ({generated_at: "2026-11-12T12:00:00Z",
    settings: settingRows, credentials: [],
    gates_readonly: {auto_spot: auto.auto_spot_enabled, auto_futures: auto.auto_futures_enabled,
      any_auto_enabled: auto.auto_spot_enabled || auto.auto_futures_enabled},
    effective_exec_mode: "dry"});
  const json = (value, status = 200) => Promise.resolve(new Response(JSON.stringify(value), {
    status, headers: {"Content-Type": "application/json"}}));
  window.fixtureRequests = requests;
  window.fetch = async (input, init = {}) => {
    const path = new URL(typeof input === "string" ? input : input.url, location.href).pathname;
    const method = (init.method || "GET").toUpperCase();
    requests.push({path, method, body: init.body ? JSON.parse(init.body) : null});
    if (path === "/api/operator-config" && method === "GET") return json(operatorPayload());
    if (path === "/api/operator-config" && method === "PUT") return json(operatorPayload());
    if (path === "/api/settings" && method === "GET") return json(settingsPayload());
    if (path === "/api/settings" && method === "PUT") {
      const body = JSON.parse(init.body);
      for (const [key, value] of Object.entries(body.settings || {})) {
        const setting = settingRows.find(item => item.key === key);
        if (setting) setting.value = value;
      }
      for (const source of eventSources) source.dispatch("changed");
      return json({...settingsPayload(), applied_at: "2026-11-12T12:01:00Z"});
    }
    if (path === "/api/health") return json({halted_today: false, audit_log_readable: true});
    if (path === "/api/autoloop") return json({generated_at: "2026-11-12T12:00:00Z", enabled: false,
      interval_minutes: 1440, running_cycle: false,
      modes: {auto_spot: auto.auto_spot_enabled, auto_futures: auto.auto_futures_enabled,
        any_auto_enabled: auto.auto_spot_enabled || auto.auto_futures_enabled}, last_cycle: null});
    if (path === "/api/llm-config") return json({generated_at: "2026-11-12T12:00:00Z", providers: {},
      tiers: {quick: {provider: "", model: "", backend_url: null}, deep: {provider: "", model: "", backend_url: null}}});
    if (path === "/api/data-sources") return json({generated_at: "2026-11-12T12:00:00Z",
      providers: [], key_path: "fixture"});
    return json({detail: "fixture route not found"}, 404);
  };
  window.EventSource = class {
    constructor() { this.listeners = new Map(); eventSources.add(this); }
    addEventListener(name, listener) {
      const listeners = this.listeners.get(name) || [];
      listeners.push(listener); this.listeners.set(name, listeners);
    }
    dispatch(name) { for (const listener of this.listeners.get(name) || []) listener(new Event(name)); }
    close() { eventSources.delete(this); }
  };
})();
"""

BROWSER_DRIVER = r"""
const {spawn, execSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const browser = process.env.TEST_BROWSER;
const base = process.env.TEST_BASE;
const profile = process.env.TEST_PROFILE;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
function check(value, message) { if (!value) throw new Error(message); }
async function until(fn, message, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { const value = await fn(); if (value) return value; await wait(50); }
  throw new Error('Timed out: ' + message);
}
function reap(child) {
  if (!child?.pid) return;
  if (process.platform === 'win32') { try { execSync(`taskkill /pid ${child.pid} /T /F`, {stdio: 'ignore'}); return; } catch {} }
  try { child.kill('SIGKILL'); } catch {}
}
(async () => {
  const chrome = spawn(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--disable-extensions', '--disable-background-networking',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
  let ws;
  const pending = new Map();
  try {
    const portFile = path.join(profile, 'DevToolsActivePort');
    const port = await until(() => fs.existsSync(portFile) && fs.readFileSync(portFile, 'utf8').split('\n')[0], 'Chrome CDP port');
    const version = await (await fetch('http://127.0.0.1:' + port + '/json/version')).json();
    ws = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
    let id = 0;
    ws.onmessage = event => {
      const message = JSON.parse(event.data); const entry = pending.get(message.id);
      if (!entry) return; pending.delete(message.id);
      if (message.error) entry.reject(new Error(message.error.message)); else entry.resolve(message.result);
    };
    const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
      const next = ++id; const timer = setTimeout(() => { pending.delete(next); reject(new Error('CDP timeout: ' + method)); }, 20000);
      pending.set(next, {resolve: value => { clearTimeout(timer); resolve(value); }, reject});
      ws.send(JSON.stringify({id: next, method, params, ...(sessionId ? {sessionId} : {})}));
    });
    const target = await send('Target.createTarget', {url: 'about:blank'});
    const attached = await send('Target.attachToTarget', {targetId: target.targetId, flatten: true});
    const cmd = (method, params = {}) => send(method, params, attached.sessionId);
    await cmd('Page.enable'); await cmd('Runtime.enable');
    await cmd('Page.navigate', {url: base + '/settings'});
    const evaluate = async expression => {
      const result = await cmd('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true, userGesture: true});
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
      return result.result.value;
    };
    const get = expression => evaluate('(' + expression + ')');
    await until(() => get('document.readyState === "complete"'), 'settings page load');
    await until(() => get('document.getElementById("operator-auto_spot_enabled") !== null'), 'operator config load');
    await evaluate(`document.querySelector('a[href="#ai"]').click()`);
    await until(() => get('document.getElementById("setting-kronos_active_profile") !== null'), 'Kronos profile selector');
    const initial = await get(`(() => ({
      options: Array.from(document.getElementById('setting-kronos_active_profile').options).map(option => option.value),
      value: document.getElementById('setting-kronos_active_profile').value,
      status: document.querySelector('[data-kronos-status]')?.textContent || '',
      readiness: document.querySelector('[data-kronos-readiness]')?.getAttribute('data-kronos-readiness'),
      switch: document.getElementById('operator-kronos_active_enabled'),
      tuning: document.getElementById('setting-kronos_active_paths'),
      model: document.querySelector('[data-kronos-status]')?.textContent.includes('Kronos-small + Kronos-Tokenizer-base')
    }))()`);
    check(JSON.stringify(initial.options) === JSON.stringify(['small_base', 'mini_2k']), 'profile options are not closed');
    check(initial.value === 'small_base', 'small_base is not the initial profile');
    check(initial.readiness === 'unavailable', 'readiness state is missing');
    check(initial.model, 'model-tokenizer pair is not shown');
    check(!initial.switch && !initial.tuning, 'retired switch or tuning field is visible');
    await evaluate(`(() => { const select = document.getElementById('setting-kronos_active_profile'); select.value = 'mini_2k'; select.dispatchEvent(new Event('change', {bubbles: true})); })()`);
    await until(() => get('document.querySelectorAll("[data-settings-action=\\"save\\"]").length > 0'), 'profile save bar');
    await evaluate(`Array.from(document.querySelectorAll('#operator-forecast button')).find(button => /Save settings|Lưu cài đặt/.test(button.textContent)).click()`);
    await until(() => get('window.fixtureRequests.some(row => row.path === "/api/settings" && row.method === "PUT")'), 'profile PUT');
    const saved = await get(`(() => ({
      body: window.fixtureRequests.find(row => row.path === '/api/settings' && row.method === 'PUT')?.body,
      value: document.getElementById('setting-kronos_active_profile').value,
      text: document.querySelector('[data-kronos-status]')?.textContent || ''
    }))()`);
    check(JSON.stringify(saved.body?.settings) === JSON.stringify({kronos_active_profile: 'mini_2k'}), 'profile save sent retired settings');
    check(saved.value === 'mini_2k', 'mini_2k selection was not retained');
    check(saved.text.includes('Kronos-mini + Kronos-Tokenizer-2k'), 'mini_2k readiness did not refresh');
    const consoleErrors = await get('window.__consoleErrors || []');
    check(consoleErrors.length === 0, 'browser console errors: ' + JSON.stringify(consoleErrors));
    process.stdout.write(JSON.stringify({result: 'pass', initial, saved}));
  } finally {
    for (const entry of pending.values()) entry.reject(new Error('driver closing'));
    pending.clear(); if (ws?.readyState <= 1) ws.close(); reap(chrome);
  }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
"""


def _browser() -> str | None:
    candidates = [
        shutil.which("chromium"), shutil.which("google-chrome"), shutil.which("chrome"),
        shutil.which("msedge"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google/Chrome/Application/chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft/Edge/Application/msedge.exe"),
    ]
    return next((item for item in candidates if item and Path(item).is_file()), None)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.integration
def test_kronos_profile_in_production_browser(tmp_path):
    web = require_web_renderer()
    browser = _browser()
    if browser is None:
        pytest.skip("No Chrome, Chromium or Edge browser available")

    layout = (
        'import "./globals.css";\n'
        'import { SettingsScreen } from "@/components/settings/settings-screen";\n'
        'export default function Layout({children}: {children: React.ReactNode}) { return '
        '<html lang="en"><head><script dangerouslySetInnerHTML={{__html: ' +
        json.dumps(SETTINGS_FIXTURE_SCRIPT) + '}} /></head><body>{children}</body></html>; }\n'
    )
    page = 'import {SettingsScreen} from "@/components/settings/settings-screen"; export default function Page(){return <SettingsScreen/>;}\n'
    root = tmp_path / "kronos-settings-next"
    build_fixture_root(web, root, layout_source=layout, pages={"settings/page.tsx": page})
    driver_path = root / "driver.cjs"
    driver_path.write_text(BROWSER_DRIVER, encoding="utf-8")
    server, logfile = start_production_server(web, root, _port())
    url = f"http://127.0.0.1:{server.args[-1]}"
    try:
        wait_for_server(server, url + "/settings")
        environment = {**os.environ, "TEST_BROWSER": browser, "TEST_BASE": url,
                       "TEST_PROFILE": str(tmp_path / "chrome-profile")}
        result = subprocess.run(["node", str(driver_path)], cwd=root, env=environment,
                                capture_output=True, check=False, encoding="utf-8", timeout=240)
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert json.loads(result.stdout)["result"] == "pass"
    finally:
        stop_production_server(server, logfile)
