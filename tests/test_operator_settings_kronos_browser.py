"""Production-browser coverage for the managed Kronos Settings switch."""

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
  const auto = {auto_spot_enabled: true, auto_futures_enabled: false};
  let kronosEnabled = false;
  const requests = [];
  const eventSources = new Set();
  const row = (key, value, type = "bool", gate = true, group = "forecast") => ({
    key, type, gate, group, value, default: value, label: key, hint: key,
    override: value !== false, stored_value: value, updated_at: null, updated_by: null,
  });
  const operatorPayload = () => ({
    generated_at: "2026-11-11T12:00:00Z",
    settings: [
      row("auto_spot_enabled", auto.auto_spot_enabled, "bool", true, "gates"),
      row("auto_futures_enabled", auto.auto_futures_enabled, "bool", true, "gates"),
      row("kronos_active_enabled", kronosEnabled),
    ],
    allowlist: ["auto_spot_enabled", "auto_futures_enabled", "kronos_active_enabled"],
    gates: {auto_spot: auto.auto_spot_enabled, auto_futures: auto.auto_futures_enabled,
      any_auto_enabled: auto.auto_spot_enabled || auto.auto_futures_enabled},
    effective_exec_mode: "dry",
    kronos: {enabled: kronosEnabled, mode: "context_only", timeframe: "4h",
      horizon_hours: 24, paths: 16, temperature: 0.5, lookback_bars: 96,
      max_batch_rows: 80, failure_policy: "defer", max_band_spread_ratio: 0.15,
      settings_problem: null,
      readiness: {status: "unavailable", reason: "fixture model unavailable"},
      model: {source_revision: "source", model_revision: "revision123456789", repo: "thuml/Kronos"}},
  });
  const settingRows = [
    {key: "kronos_active_paths", env_var: "TRADINGAGENTS_KRONOS_ACTIVE_PATHS", value: 16,
      default: 16, type: "int", editable: true, source: "default"},
  ];
  const settingsPayload = () => ({generated_at: "2026-11-11T12:00:00Z", settings: settingRows,
    credentials: [], gates_readonly: {auto_spot: auto.auto_spot_enabled,
      auto_futures: auto.auto_futures_enabled}, effective_exec_mode: "dry"});
  const json = (value, status = 200) => Promise.resolve(new Response(JSON.stringify(value), {
    status, headers: {"Content-Type": "application/json"}}));
  window.fixtureRequests = requests;
  window.fixtureBackend = {
    auto,
    get kronosEnabled() { return kronosEnabled; },
    setKronosEnabled(value) {
      kronosEnabled = value;
      for (const source of eventSources) source.dispatch("changed");
    },
  };
  window.fetch = async (input, init = {}) => {
    const path = new URL(typeof input === "string" ? input : input.url, location.href).pathname;
    const method = (init.method || "GET").toUpperCase();
    requests.push({path, method, body: init.body ? JSON.parse(init.body) : null});
    if (path === "/api/operator-config" && method === "GET") return json(operatorPayload());
    if (path === "/api/operator-config" && method === "PUT") {
      const body = JSON.parse(init.body);
      if (body.settings.kronos_active_enabled !== undefined) {
        if (body.confirm !== true || !body.reason) return json({error: {
          code: "gate_confirmation_required", message: "confirm and reason required", details: {}}}, 412);
        kronosEnabled = body.settings.kronos_active_enabled;
      }
      if (body.settings.auto_spot_enabled !== undefined) auto.auto_spot_enabled = body.settings.auto_spot_enabled;
      if (body.settings.auto_futures_enabled !== undefined) auto.auto_futures_enabled = body.settings.auto_futures_enabled;
      return json(operatorPayload());
    }
    if (path === "/api/settings" && method === "GET") return json(settingsPayload());
    if (path === "/api/settings" && method === "PUT") {
      const body = JSON.parse(init.body);
      if (body.settings.kronos_active_paths < 16) return json({error: {
        code: "validation_error", message: "invalid Kronos tuning", details: {
          fields: {kronos_active_paths: "paths must be between 16 and 32"}}}}, 422);
      settingRows[0].value = body.settings.kronos_active_paths;
      return json({...settingsPayload(), applied_at: "2026-11-11T12:01:00Z"});
    }
    if (path === "/api/health") return json({halted_today: false, audit_log_readable: true});
    if (path === "/api/autoloop") return json({generated_at: "2026-11-11T12:00:00Z", enabled: false,
      interval_minutes: 1440, running_cycle: false, modes: {auto_spot: auto.auto_spot_enabled,
        auto_futures: auto.auto_futures_enabled, any_auto_enabled: auto.auto_spot_enabled || auto.auto_futures_enabled},
      last_cycle: null});
    if (path === "/api/llm-config") return json({generated_at: "2026-11-11T12:00:00Z", providers: {},
      tiers: {quick: {provider: "", model: "", backend_url: null}, deep: {provider: "", model: "", backend_url: null}}});
    if (path === "/api/data-sources") return json({generated_at: "2026-11-11T12:00:00Z",
      providers: [{provider: "reddit", configured: false, decryptable: false, valid: false,
        cookie_count: 0, cookie_names: [], updated_at: null, updated_by: null},
        {provider: "x", configured: false, decryptable: false, valid: false,
        cookie_count: 0, cookie_names: [], updated_at: null, updated_by: null}], key_path: "fixture"});
    return json({detail: "fixture route not found"}, 404);
  };
  window.EventSource = class {
    constructor() { this.listeners = new Map(); eventSources.add(this); }
    addEventListener(name, listener) {
      const listeners = this.listeners.get(name) || [];
      listeners.push(listener); this.listeners.set(name, listeners);
    }
    dispatch(name) {
      for (const listener of this.listeners.get(name) || []) listener(new Event(name));
    }
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
const COMMAND_TIMEOUT = 20000;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
function check(value, message) { if (!value) throw new Error(message); }
function timeout(promise, ms, message) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer=setTimeout(() => reject(new Error(message)), ms); }),
  ]).finally(() => clearTimeout(timer));
}
async function until(fn, message, ms = 20000) {
  const stop = Date.now() + ms;
  while (Date.now() < stop) { const result = await fn(); if (result) return result; await wait(50); }
  throw new Error('Timed out: ' + message);
}
function reap(child) {
  if (!child?.pid) return;
  if (process.platform === 'win32') {
    try { execSync(`taskkill /pid ${child.pid} /T /F`, {stdio: 'ignore'}); return; } catch {}
  }
  try { child.kill('SIGKILL'); } catch {}
}
(async () => {
  const chrome = spawn(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--disable-extensions', '--disable-background-networking', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
  let ws;
  const pending = new Map();
  try {
    const portFile = path.join(profile, 'DevToolsActivePort');
    const port = await until(() => fs.existsSync(portFile) && fs.readFileSync(portFile, 'utf8').split('\n')[0], 'Chrome CDP port');
    const version = await timeout(fetch('http://127.0.0.1:' + port + '/json/version').then(response => response.json()),
      COMMAND_TIMEOUT, 'CDP version timeout');
    ws = new WebSocket(version.webSocketDebuggerUrl);
    await timeout(new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; }), 15000, 'CDP socket timeout');
    let id = 0;
    ws.onmessage = event => {
      const message = JSON.parse(event.data);
      if (!message.id) return;
      const entry = pending.get(message.id);
      if (!entry) return;
      pending.delete(message.id); clearTimeout(entry.timer);
      if (message.error) entry.reject(new Error(message.error.message)); else entry.resolve(message.result);
    };
    const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
      const next = ++id;
      const timer = setTimeout(() => { pending.delete(next); reject(new Error('CDP timeout: ' + method)); }, COMMAND_TIMEOUT);
      pending.set(next, {resolve, reject, timer});
      ws.send(JSON.stringify({id: next, method, params, ...(sessionId ? {sessionId} : {})}));
    });
    const target = await send('Target.createTarget', {url: 'about:blank'});
    const attached = await send('Target.attachToTarget', {targetId: target.targetId, flatten: true});
    const cmd = (method, params = {}) => send(method, params, attached.sessionId);
    await cmd('Page.enable'); await cmd('Runtime.enable');
    await cmd('Page.addScriptToEvaluateOnNewDocument', {source: 'window.__consoleErrors=[];window.addEventListener("error",e=>window.__consoleErrors.push(e.message));'});
    const evaluate = async expression => {
      const result = await cmd('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true, userGesture: true});
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text + ': ' + (result.exceptionDetails.exception?.description || ''));
      return result.result.value;
    };
    const get = expression => evaluate('(' + expression + ')');
    await cmd('Page.navigate', {url: base + '/settings'});
    await until(() => get('document.readyState === "complete"'), 'Settings page load');
    await until(() => get('document.getElementById("operator-auto_spot_enabled") !== null'), 'operator config load');
    await evaluate(`document.querySelector('a[href="#ai"]').click()`);
    await until(() => get('document.getElementById("operator-kronos_active_enabled") !== null'), 'Kronos switch visible');
    const initial = await get(`(() => ({auto: document.getElementById('operator-auto_spot_enabled').checked,
      futures: document.getElementById('operator-auto_futures_enabled').checked,
      title: document.getElementById('operator-forecast').textContent,
      inAutomation: document.getElementById('automation').textContent.includes('Kronos'),
      aria: document.getElementById('operator-kronos_active_enabled').getAttribute('aria-describedby')}))()`);
    check(/Kronos/.test(initial.title), 'Kronos forecast card not findable in AI section');
    check(!initial.inAutomation, 'Kronos switch appeared in Automation beside Auto modes');
    check(initial.auto && !initial.futures, 'fixture Auto initial state incorrect');
    check(initial.aria.includes('operator-kronos_active_enabled-status'), 'switch status relationship missing');

    await evaluate('document.getElementById("operator-kronos_active_enabled").click()');
    await until(() => get('document.getElementById("operator-forecast-reason") !== null'), 'Kronos confirmation');
    const saveButton = `Array.from(document.querySelectorAll('#operator-forecast button')).find(button => button.textContent.includes('Save'))`;
    check(await get(saveButton + '.disabled'), 'Kronos save enabled before confirmation and reason');
    await evaluate(`document.querySelector('#operator-forecast input[type="checkbox"]:not([id])').click()`);
    await evaluate(`(() => { const input=document.getElementById('operator-forecast-reason');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'test context only');
      input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await until(() => get('!' + saveButton + '.disabled'), 'confirmed Save enabled');
    await evaluate(saveButton + '.click()');
    await until(() => get('window.fixtureBackend.kronosEnabled === true'), 'Kronos enabled save');
    const saved = await get(`(() => ({requests: window.fixtureRequests.filter(row=>row.path==='/api/operator-config'&&row.method==='PUT'),
      auto: window.fixtureBackend.auto, status: document.querySelector('[data-kronos-enabled="true"]') !== null}))()`);
    check(saved.requests.length === 1, 'expected one operator-config PUT');
    check(Object.keys(saved.requests[0].body.settings).join(',') === 'kronos_active_enabled', 'PUT changed keys other than Kronos');
    check(saved.requests[0].body.confirm === true && saved.requests[0].body.reason === 'test context only', 'confirmation/reason body incorrect');
    check(saved.status && saved.auto.auto_spot_enabled && !saved.auto.auto_futures_enabled, 'save changed Auto settings or failed status refresh');

    await evaluate('document.getElementById("operator-kronos_active_enabled").click()');
    await until(() => get('document.getElementById("operator-forecast-reason") !== null'), 'Kronos disable confirmation');
    const undoButton = `Array.from(document.querySelectorAll('#operator-forecast button')).find(button => button.textContent.includes('Undo'))`;
    await evaluate(undoButton + '.click()');
    await until(() => get('document.getElementById("operator-kronos_active_enabled").checked === true'), 'Kronos undo');
    check(await get('window.fixtureBackend.kronosEnabled === true'), 'Undo changed persisted Kronos state');

    await evaluate('document.getElementById("operator-kronos_active_enabled").click()');
    await until(() => get('document.getElementById("operator-forecast-reason") !== null'), 'Kronos disable draft');
    await evaluate(`document.querySelector('#operator-forecast input[type="checkbox"]:not([id])').click()`);
    await evaluate(`(() => { const input=document.getElementById('operator-forecast-reason');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'disable in fixture');
      input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await until(() => get('!' + saveButton + '.disabled'), 'confirmed disable enabled');
    await evaluate(saveButton + '.click()');
    await until(() => get('window.fixtureBackend.kronosEnabled === false'), 'Kronos disable save');
    check(await get('window.fixtureBackend.auto.auto_spot_enabled && !window.fixtureBackend.auto.auto_futures_enabled'), 'disable changed Auto modes');

    await evaluate('document.getElementById("operator-kronos_active_enabled").click()');
    await until(() => get('document.getElementById("operator-forecast-reason") !== null'), 'Kronos conflict draft');
    await evaluate(`document.querySelector('#operator-forecast input[type="checkbox"]:not([id])').click()`);
    await evaluate(`(() => { const input=document.getElementById('operator-forecast-reason');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'authorization before server refresh');
      input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await until(() => get('!' + saveButton + '.disabled'), 'conflict draft initially authorized');
    await evaluate('window.fixtureBackend.setKronosEnabled(true)');
    const reviewButton = `Array.from(document.querySelectorAll('#operator-forecast button')).find(button => button.textContent.includes('Load latest data'))`;
    await until(() => get(reviewButton + ' !== undefined'), 'Kronos server-ahead warning');
    const conflict = await get(`(() => ({draft: document.getElementById('operator-kronos_active_enabled').checked,
      confirm: document.querySelector('#operator-forecast input[type="checkbox"]:not([id])').checked,
      reason: document.getElementById('operator-forecast-reason').value,
      puts: window.fixtureRequests.filter(row=>row.path==='/api/operator-config'&&row.method==='PUT').length}))()`);
    check(conflict.draft && !conflict.confirm && conflict.reason === '', 'server change lost draft or retained stale authorization');
    check(await get(saveButton + '.disabled'), 'server-ahead state allowed a stale save');
    await evaluate(saveButton + '.click()');
    check(await get(`window.fixtureRequests.filter(row=>row.path==='/api/operator-config'&&row.method==='PUT').length === ${conflict.puts}`),
      'server-ahead state sent a stale PUT');
    await evaluate(reviewButton + '.click()');
    await until(() => get('document.getElementById("operator-forecast-reason") === null && document.querySelector(\'[data-kronos-enabled="true"]\') !== null'),
      'review acknowledges the matching server value without another write');
    check(await get('window.fixtureBackend.auto.auto_spot_enabled && !window.fixtureBackend.auto.auto_futures_enabled'), 'Kronos conflict changed Auto modes');
    check(await get(`window.fixtureRequests.filter(row=>row.path==='/api/operator-config'&&row.method==='PUT').length === ${conflict.puts}`),
      'review of server data wrote a switch value');

    const tuningDetails = `Array.from(document.querySelectorAll('#ai details')).find(details => details.querySelector('summary')?.textContent.includes('Kronos'))`;
    await evaluate(tuningDetails + '.open = true');
    await until(() => get('document.getElementById("setting-kronos_active_paths") !== null'), 'Kronos tuning field');
    await evaluate(`(() => { const input=document.getElementById('setting-kronos_active_paths');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'3');
      input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await evaluate(`Array.from(document.querySelectorAll('#ai button')).find(button=>button.textContent.includes('Save settings')).click()`);
    await until(() => get('document.getElementById("setting-kronos_active_paths-error") !== null'), 'domain 422 field error');
    const domain = await get(`(() => ({invalid: document.getElementById('setting-kronos_active_paths').getAttribute('aria-invalid'),
      error: document.getElementById('setting-kronos_active_paths-error').textContent,
      autoSpot: window.fixtureBackend.auto.auto_spot_enabled, autoFutures: window.fixtureBackend.auto.auto_futures_enabled,
      puts: window.fixtureRequests.filter(row=>row.path==='/api/settings'&&row.method==='PUT').length}))()`);
    check(domain.invalid === 'true' && /between 16 and 32/.test(domain.error), 'domain error not mapped to Kronos field');
    check(domain.puts === 1 && domain.autoSpot && !domain.autoFutures, 'domain error mutated unrelated state');
    check(await get('window.__consoleErrors.length === 0'), 'browser console error: ' + await get('window.__consoleErrors'));
    process.stdout.write(JSON.stringify({result: 'pass', initial, saved, domain}));
  } finally {
    for (const entry of pending.values()) clearTimeout(entry.timer);
    pending.clear();
    if (ws?.readyState <= 1) ws.close();
    reap(chrome);
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
def test_kronos_operator_switch_in_production_browser(tmp_path):
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
