"""Offline browser coverage for the Settings navigation guard in Next App Router."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.support.node_renderer import require_web_renderer

FAKE_API = r"""
(() => {
  const row = (key, value) => ({key, value, default: value, type: "str", editable: true,
    env_var: "TEST_" + key.toUpperCase(), source: "default"});
  const credential = {supported: true, required: true, configured: false, source: "none",
    masked: null, last4: null, env_var: "TEST_KEY"};
  const provider = {id: "openai", label: "OpenAI", auth_mode: "api_key",
    requires_backend_url: false, default_backend_url: null,
    credential: {quick: credential, deep: credential}, models: {quick: [], deep: []}};
  const fixtures = {
    "/api/settings": {generated_at: "2026-11-09T00:00:00Z",
      settings: [row("llm_provider", "openai"), row("benchmark_ticker", "SPY")],
      credentials: [], gates_readonly: {}, effective_exec_mode: "dry"},
    "/api/operator-config": {generated_at: "2026-11-09T00:00:00Z", settings: [{
      key: "risk_max_open_heat_pct", type: "float", group: "risk", label: "Risk",
      hint: "Risk", gate: false, value: 6, default: 6, override: false,
      stored_value: null, updated_at: null, updated_by: null}],
      allowlist: ["risk_max_open_heat_pct"],
      gates: {auto_spot: false, auto_futures: false, any_auto_enabled: false}, effective_exec_mode: "dry"},
    "/api/llm-config": {generated_at: "2026-11-09T00:00:00Z", providers: {openai: provider},
      tiers: {quick: {provider: "openai"}, deep: {provider: "openai"}}},
    "/api/data-sources": {generated_at: "2026-11-09T00:00:00Z", key_path: "test", providers:
      ["reddit", "x"].map(provider => ({provider, configured: false, decryptable: true,
        valid: false, cookie_count: 0, cookie_names: [], updated_at: null, updated_by: null}))},
    "/api/autoloop": {generated_at: "2026-11-09T00:00:00Z", enabled: true,
      interval_minutes: 60, running_cycle: false,
      modes: {auto_spot: true, auto_futures: false, any_auto_enabled: true}, last_cycle: null},
    "/api/health": {audit_log_readable: true, halted_today: false, exec_mode: "dry"},
  };
  const fetchOriginal = window.fetch.bind(window);
  window.__apiRequests = [];
  window.fetch = (url, init = {}) => {
    const requestUrl = typeof url === "string" ? url : url.url;
    const parsed = new URL(requestUrl, location.href);
    if (!parsed.pathname.startsWith("/api/")) return fetchOriginal(url, init);
    window.__apiRequests.push({path: parsed.pathname, method: init.method || "GET"});
    if ((init.method && init.method !== "GET") || !(parsed.pathname in fixtures)) {
      return Promise.resolve(new Response("{}", {status: 404}));
    }
    return Promise.resolve(new Response(JSON.stringify(fixtures[parsed.pathname]), {
      status: 200, headers: {"Content-Type": "application/json"}}));
  };
  window.EventSource = class { addEventListener() {} close() {} };
})();
"""

DRIVER = r"""
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const browserPath = process.env.TEST_BROWSER;
const base = process.env.TEST_BASE;
const profile = process.env.TEST_PROFILE;
const fallback = process.env.TEST_FALLBACK === '1';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function check(ok, message) { if (!ok) throw new Error(message); }
async function until(fn, message, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { const value = await fn(); if (value) return value; await sleep(50); }
  throw new Error('Timed out: ' + message);
}
(async () => {
  const chrome = spawn(browserPath, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--disable-extensions', '--disable-sync', '--disable-background-networking',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
  try {
    const portFile = path.join(profile, 'DevToolsActivePort');
    const port = await until(() => fs.existsSync(portFile) && fs.readFileSync(portFile, 'utf8').split('\n')[0], 'debugger');
    const version = await (await fetch('http://127.0.0.1:' + port + '/json/version')).json();
    const socket = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    let id = 0;
    const pending = new Map();
    const events = [];
    let dialogDecision = false;
    socket.onmessage = event => {
      const data = JSON.parse(event.data);
      if (data.id) {
        const callbacks = pending.get(data.id); pending.delete(data.id);
        if (data.error) callbacks.reject(new Error(data.error.message)); else callbacks.resolve(data.result);
      } else {
        events.push(data);
        if (data.method === 'Page.javascriptDialogOpening') {
          send('Page.handleJavaScriptDialog', {accept: dialogDecision}, data.sessionId).catch(() => {});
        }
      }
    };
    function send(method, params = {}, sessionId) {
      return new Promise((resolve, reject) => {
        const next = ++id; pending.set(next, {resolve, reject});
        socket.send(JSON.stringify({id: next, method, params, ...(sessionId ? {sessionId} : {})}));
      });
    }
    const target = await send('Target.createTarget', {url: 'about:blank'});
    const {sessionId} = await send('Target.attachToTarget', {targetId: target.targetId, flatten: true});
    const cmd = (method, params) => send(method, params, sessionId);
    await cmd('Page.enable'); await cmd('Runtime.enable');
    const init = `
      window.__navigations = [];
      ${fallback ? 'Object.defineProperty(window, "navigation", {value: undefined, configurable: true});' : ''}
      if (window.navigation) window.navigation.addEventListener('navigate', event => {
        window.__navigations.push({type: event.navigationType, cancelable: event.cancelable,
          sameDocument: event.destination.sameDocument, url: event.destination.url});
      });`;
    await cmd('Page.addScriptToEvaluateOnNewDocument', {source: init});
    async function evaluate(expression) {
      const result = await cmd('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true, userGesture: true});
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text + ': ' + result.exceptionDetails.exception?.description);
      return result.result.value;
    }
    const get = expression => evaluate('(' + expression + ')');
    async function wait(expression, message) { return until(() => get('Boolean(' + expression + ')'), message); }
    async function click(selector) {
      await wait(`document.querySelector(${JSON.stringify(selector)})`, 'element ' + selector);
      await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
    }
    async function navigate(url) {
      dialogDecision = true;
      await cmd('Page.navigate', {url: base + url});
      await wait('document.readyState === "complete" && !!document.querySelector("#fixture-ready")', 'page load');
      dialogDecision = false;
    }
    async function homeToSettings(hash = '') {
      await navigate('/');
      await click('a[href="/settings"]');
      await wait('location.pathname === "/settings" && document.querySelector("#llm-quick-api-key") && document.querySelector("#operator-risk_max_open_heat_pct") && document.querySelector("#autoloop-interval") && document.querySelector("textarea")', 'settings hydrated');
      if (hash) { await click('a[href="#' + hash + '"]'); await wait('location.hash === "#' + hash + '"', 'hash'); }
    }
    async function setValue(selector, value, dirty = true) {
      await evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)});
        Object.getOwnPropertyDescriptor(e instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value').set.call(e, ${JSON.stringify(value)});
        e.dispatchEvent(new Event('input', {bubbles: true})); })()`);
      await wait('document.body.textContent.includes("You have unsaved changes. Save or discard") === ' + dirty, 'dirty state');
      await sleep(150);
    }
    const prompts = () => events.filter(event => event.method === 'Page.javascriptDialogOpening');
    const beforeUnload = () => get(`(() => { const e = new Event('beforeunload', {cancelable:true});
      window.dispatchEvent(e); return e.defaultPrevented; })()`);
    async function historyStep(direction, expectedPrompt) {
      const previous = prompts().length;
      const history = await cmd('Page.getNavigationHistory');
      const target = history.entries[history.currentIndex + (direction === 'back' ? -1 : 1)];
      check(target, direction + ' destination missing');
      await cmd('Page.navigateToHistoryEntry', {entryId: target.id});
      if (expectedPrompt) {
        await until(() => prompts().length === previous + 1, direction + ' confirmation');
        await sleep(500);
      } else await sleep(500);
    }
    async function storageDoesNotContain(secret) {
      return get(`![JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie,
        JSON.stringify(history.state)].some(x => String(x).includes(${JSON.stringify(secret)}))`);
    }
    const input = '#llm-quick-api-key';
    const secret = 'synthetic-private-key-for-guard';

    // Real Next Link and each independent draft source trigger exactly one confirmation.
    for (const [area, selector, value] of [
      ['ai', input, secret], ['connections', 'textarea', 'synthetic-cookie-draft'],
      ['risk', '#operator-risk_max_open_heat_pct', '8'],
      ['automation', '#autoloop-interval', '90'],
      ['system', '#setting-benchmark_ticker', 'QQQ'],
    ]) {
      await homeToSettings(area);
      check(!await beforeUnload(), area + ' clean page has unload warning');
      if (area === 'system') await evaluate('document.querySelector("#system details").open = true');
      await setValue(selector, value);
      check(await beforeUnload(), area + ' dirty page lacks unload warning');
      if (value.length > 8) check(await storageDoesNotContain(value), area + ' draft leaked into browser storage');
      const before = prompts().length;
      await click('a[href="/"]');
      check(prompts().length === before + 1 && prompts().at(-1).params.type === 'confirm', area + ' Link did not confirm once');
      check(await get('location.pathname') === '/settings', area + ' cancel left settings');
      check(await get(`document.querySelector(${JSON.stringify(selector)}).value`) === value, area + ' cancel lost draft');
      dialogDecision = true;
      await click('a[href="/"]');
      await wait('location.pathname === "/"', area + ' accepted Link');
      dialogDecision = false;
    }

    // Hash navigation and history traversal between the five panes never confirm.
    await homeToSettings('ai'); await setValue(input, secret);
    const start = prompts().length;
    for (const section of ['risk', 'connections', 'system', 'automation', 'ai']) await click('a[href="#' + section + '"]');
    await historyStep('back', false);
    await wait('location.hash === "#automation"', 'hash back');
    await historyStep('forward', false);
    await wait('location.hash === "#ai"', 'hash forward');
    check(prompts().length === start, 'hash navigation prompted');
    check(await get('document.querySelector("#llm-quick-api-key").value') === secret, 'hash lost draft');
    check(await storageDoesNotContain(secret), 'secret persisted after hash traversal');

    // Ctrl-click must not be intercepted; prevent a real popup after the capture phase.
    await evaluate(`(() => { const a = document.querySelector('a[href="/"]');
      a.addEventListener('click', e => e.preventDefault(), {once:true});
      a.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, button:0, ctrlKey:true})); })()`);
    check(prompts().length === start, 'Ctrl-click prompted');
    await setValue(input, '', false);
    check(!await beforeUnload(), 'cleared draft still warns on unload');
    await click('a[href="/"]');
    await wait('location.pathname === "/"', 'clean link navigation');
    check(prompts().length === start, 'clean link prompted');

    // Deep link aliases select automation and are retained when editing.
    await navigate('/settings#exec-gates');
    await wait('document.querySelector("a[href=\\"#automation\\"]").getAttribute("aria-current") === "page"', 'deep link selection');
    await wait('document.querySelector("#autoloop-interval")', 'deep link data');
    await setValue('#autoloop-interval', '90');
    check(await get('location.hash') === '#exec-gates', 'deep link changed');
    const unloadPrompts = prompts().length;
    await cmd('Page.navigate', {url: base + '/'});
    await until(() => prompts().length === unloadPrompts + 1, 'native beforeunload');
    check(prompts().at(-1).params.type === 'beforeunload', 'native unload dialog missing');
    check(await get('location.pathname') === '/settings', 'unload cancel left settings');
    check(await get('document.querySelector("#autoloop-interval").value') === '90', 'unload cancel lost draft');
    dialogDecision = true;
    await cmd('Page.navigate', {url: base + '/'});
    await wait('location.pathname === "/"', 'unload accept');
    dialogDecision = false;

    // Back from a Next client transition. This stays in the same document.
    await homeToSettings('ai'); await setValue(input, secret);
    const previous = prompts().length;
    await historyStep('back', false); // ai hash -> settings root
    await wait('location.hash === ""', 'back to root hash');
    await historyStep('back', !fallback);
    if (fallback) {
      await wait('location.pathname === "/"', 'legacy back proceeds without Navigation API');
    } else {
      check(await get('location.pathname') === '/settings', 'back cancel lost settings');
      check(await get('document.querySelector("#llm-quick-api-key").value') === secret, 'back cancel lost key');
      check(prompts().length === previous + 1, 'back cancel prompt count');
      dialogDecision = true;
      await historyStep('back', true);
      await wait('location.pathname === "/"', 'back accept');
      dialogDecision = false;
    }

    // Forward is tested from a real Next entry restored by Back.
    if (fallback) {
      await homeToSettings();
      await click('a[href="/"]'); await wait('location.pathname === "/"', 'legacy forward setup home');
      await historyStep('back', false);
      await wait('location.pathname === "/settings" && document.querySelector("#llm-quick-api-key")', 'legacy forward setup settings');
      await setValue(input, secret);
      const before = prompts().length;
      await historyStep('forward', false);
      await wait('location.pathname === "/"', 'legacy forward proceeds without Navigation API');
      check(prompts().length === before, 'unexpected legacy forward prompt');
    } else {
      await homeToSettings();
      await click('a[href="/"]'); await wait('location.pathname === "/"', 'forward setup home');
      await historyStep('back', false);
      await wait('location.pathname === "/settings" && document.querySelector("#llm-quick-api-key")', 'forward setup settings');
      await setValue(input, secret);
      await historyStep('forward', true);
      check(await get('location.pathname') === '/settings', 'forward cancel lost settings');
      check(await get('document.querySelector("#llm-quick-api-key").value') === secret, 'forward cancel lost key');
      dialogDecision = true;
      await historyStep('forward', true);
      await wait('location.pathname === "/"', 'forward accept');
      dialogDecision = false;
    }
    check(await get('window.__apiRequests.every(x => x.method === "GET")'), 'unexpected API write');
    process.stdout.write(JSON.stringify({result:'pass', fallback, browser:version.Browser}));
    socket.close();
  } finally { chrome.kill(); }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
"""


def _browser():
    candidates = [
        shutil.which("chromium"), shutil.which("google-chrome"), shutil.which("chrome"),
        shutil.which("msedge"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google/Chrome/Application/chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft/Edge/Application/msedge.exe"),
    ]
    return next((item for item in candidates if item and Path(item).is_file()), None)


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def next_fixture(tmp_path_factory):
    web = require_web_renderer()
    root = tmp_path_factory.mktemp("settings-guard-next")
    shutil.copytree(web / "components", root / "components")
    shutil.copytree(web / "lib", root / "lib")
    modules = root / "node_modules"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(modules), str(web / "node_modules")],
                       check=True, capture_output=True, encoding="utf-8")
    else:
        modules.symlink_to(web / "node_modules", target_is_directory=True)
    (root / "package.json").write_text('{"private":true}', encoding="utf-8")
    (root / "tsconfig.json").write_text(json.dumps({"compilerOptions": {
        "target": "ES2017", "lib": ["dom", "dom.iterable", "esnext"], "jsx": "react-jsx",
        "module": "esnext", "moduleResolution": "bundler", "strict": True, "noEmit": True,
        "skipLibCheck": True, "esModuleInterop": True, "paths": {"@/*": ["./*"]}}}), encoding="utf-8")
    (root / "app/settings").mkdir(parents=True)
    (root / "app/layout.tsx").write_text(
        'import Link from "next/link"; export default function Layout({children}: '
        '{children: React.ReactNode}) {return <html lang="en"><head><script '
        'dangerouslySetInnerHTML={{__html: ' + json.dumps(FAKE_API) + '}} /></head><body>'
        '<div id="fixture-ready"/><nav><Link href="/">Home</Link><Link href="/settings">Settings</Link>'
        '</nav>{children}</body></html>}', encoding="utf-8")
    (root / "app/page.tsx").write_text('export default function Home() {return <p>Home</p>}', encoding="utf-8")
    (root / "app/settings/page.tsx").write_text(
        'import {SettingsScreen} from "@/components/settings/settings-screen"; '
        'export default function Page() {return <SettingsScreen/>}', encoding="utf-8")
    (root / "driver.cjs").write_text(DRIVER, encoding="utf-8")
    port = _port()
    logfile = (root / "next.log").open("w+", encoding="utf-8")
    env = {**os.environ, "NEXT_TELEMETRY_DISABLED": "1", "NODE_ENV": "development"}
    server = subprocess.Popen(
        ["node", str(web / "node_modules/next/dist/bin/next"), "dev", "--webpack", "-H", "127.0.0.1", "-p", str(port)],
        cwd=root, env=env, stdout=logfile, stderr=subprocess.STDOUT,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if server.poll() is not None:
                break
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        else:
            pytest.fail("Next fixture startup timed out")
        if server.poll() is not None:
            logfile.flush()
            pytest.fail((root / "next.log").read_text(encoding="utf-8"))
        yield root, url
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        logfile.close()


@pytest.mark.unit
@pytest.mark.parametrize("fallback", [False, True], ids=["navigation-api", "no-navigation-api"])
def test_settings_navigation_guard_in_next_browser(next_fixture, tmp_path, fallback):
    browser = _browser()
    if not browser:
        pytest.skip("No Chrome, Chromium or Edge browser available")
    root, url = next_fixture
    env = {**os.environ, "TEST_BROWSER": browser, "TEST_BASE": url,
           "TEST_PROFILE": str(tmp_path / "profile"), "TEST_FALLBACK": str(int(fallback))}
    result = subprocess.run(["node", str(root / "driver.cjs")], cwd=root, env=env,
                            capture_output=True, check=False, encoding="utf-8", timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["result"] == "pass"
