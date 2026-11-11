"""Capture UX evidence for the dashboard acceptance (desktop + mobile).

Reuses the OFFLINE production fixture from
``tests/browser/test_dashboard_brief_offline.py`` — the same build + fixture API
the browser acceptance uses — so the screenshots are of the real dashboard
rendered by a real Chrome, with no network beyond the test-owned local server
(``--host-resolver-rules`` maps everything else to NOTFOUND).

Outputs (under .zcode/artifacts/trading-delivery-20261111/):
- desktop-overview.png        — 1440x900, brief fold, forecast details closed
- desktop-forecast-open.png   — same page with the forecast disclosure opened
- mobile-overview.png         — 390x844 mobile viewport, full page
- measurements.json           — the measured W1-W6 evidence (viewport-keyed)

This is an evidence tool, not a pass/fail gate: it reports measurements, and
the pass/fail assertions live in the browser acceptance test.

Usage:
    .venv/Scripts/python.exe scripts/capture_dashboard_ux_evidence.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from tests.browser.dashboard_fixture import build_dashboard_fixture  # noqa: E402
from tests.browser.test_dashboard_brief_offline import (  # noqa: E402
    _browser,
    _port,
)
from tests.support.node_renderer import require_web_renderer  # noqa: E402

ARTIFACTS = _REPO / ".zcode" / "artifacts" / "trading-delivery-20261111"

# Everything is bounded: browser boot 40s, websocket 20s, each CDP command 30s,
# each page wait 60s. The driver always reaps the Chrome tree it spawned.
CAPTURE_DRIVER = r"""
const {spawn, execSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const browserPath = process.env.TEST_BROWSER;
const base = process.env.TEST_BASE;
const profile = process.env.TEST_PROFILE;
const outDir = process.env.TEST_OUT;
const COMMAND_TIMEOUT_MS = 30000;
const SOCKET_TIMEOUT_MS = 20000;
const BOOT_TIMEOUT_MS = 40000;
const PAGE_TIMEOUT_MS = 60000;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function check(ok, message) { if (!ok) throw new Error(message); }
function withTimeout(promise, timeout, message) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error('Timed out: ' + message)), timeout)),
  ]);
}
async function until(fn, message, timeout = 40000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { const value = await fn(); if (value) return value; await sleep(50); }
  throw new Error('Timed out: ' + message);
}
function reapProcessTree(child) {
  if (!child || child.pid === undefined) return;
  if (process.platform === 'win32') {
    try { execSync(`taskkill /pid ${child.pid} /T /F`, {stdio: 'ignore'}); return; } catch {}
  }
  try { child.kill('SIGKILL'); } catch {}
}
const MEASURE = `(() => {
  const visible = el => el.getClientRects().length > 0;
  const interactive = Array.from(document.querySelectorAll('button, a, summary, input'))
    .filter(visible);
  const targets = interactive.map(el => {
    const r = el.getBoundingClientRect();
    return {tag: el.tagName.toLowerCase(), text: (el.textContent || el.value || '').trim().slice(0, 40),
            w: Math.round(r.width), h: Math.round(r.height)};
  });
  const smallTargets = targets.filter(t => (t.h > 0 && t.h < 44) || (t.w > 0 && t.w < 44));
  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
    .map(h => +h.tagName[1]);
  let skipped = null;
  let prev = 0;
  for (const level of headings) {
    if (level > prev + 1 && prev !== 0) { skipped = level; break; }
    prev = level;
  }
  // Contrast of muted text on its effective background. Colors may be oklch()
  // in this Tailwind v4 theme, so resolve both through a canvas (the browser
  // returns a concrete #rrggbb / rgba there).
  let contrast = null;
  const muted = Array.from(document.querySelectorAll('.text-muted-foreground')).find(visible);
  if (muted) {
    const canvas = document.createElement('canvas');
    canvas.width = 1; canvas.height = 1;
    const ctx = canvas.getContext('2d');
    const resolve = value => {
      if (!value) return null;
      ctx.fillStyle = '#000';
      ctx.fillStyle = value;
      const m = /rgba?\\(([^)]+)\\)/.exec(ctx.fillStyle || '');
      if (!m) return null;
      const parts = m[1].split(',').map(Number);
      return {r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1};
    };
    const lum = c => {
      const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
    };
    const fg = resolve(getComputedStyle(muted).color);
    let bgNode = muted;
    let bg = null;
    while (bgNode && (!bg || bg.a === 0)) {
      bg = resolve(getComputedStyle(bgNode).backgroundColor) || bg;
      bgNode = bgNode.parentElement;
    }
    if (fg && bg && bg.a !== 0) {
      const l1 = lum(fg), l2 = lum(bg);
      contrast = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      contrast = Math.round(contrast * 100) / 100;
    }
  }
  const refresh = Array.from(document.querySelectorAll('button'))
    .filter(b => visible(b) && /Làm mới|Refresh/i.test(b.textContent || '')).length;
  const doc = document.documentElement;
  const briefBlocks = Array.from(document.querySelectorAll('[data-brief-block], [data-brief-state]'));
  const overflowBlocks = briefBlocks
    .filter(b => visible(b) && b.scrollWidth > b.clientWidth + 1)
    .map(b => (b.getAttribute('data-brief-block') || b.getAttribute('data-brief-state') || 'block'));
  const body = document.body.textContent || '';
  return {
    url: location.pathname,
    docHeight: Math.round(document.body.scrollHeight),
    pageHorizontalOverflow: doc.scrollWidth > doc.clientWidth + 1,
    interactiveCount: targets.length,
    smallTargets,
    h1: headings.filter(l => l === 1).length,
    h2: headings.filter(l => l === 2).length,
    h3: headings.filter(l => l === 3).length,
    headingSkipped: skipped,
    mutedContrast: contrast,
    refreshButtons: refresh,
    briefOverflowBlocks: overflowBlocks,
    notEnoughDataLabelled: /Not enough data|Chưa đủ dữ liệu/.test(body),
    heatZeroFabricated: /Heat (in use|đang dùng)\\s*\\b0%/.test(body),
    heatUnknownShown: /Heat (in use|đang dùng)\\s*(Unknown|Chưa xác định)/.test(body),
    activeForecastState: document.querySelector('[data-brief-block="active-forecast"]')
      ? document.querySelector('[data-brief-block="active-forecast"]').getAttribute('data-active-forecast-state')
      : null,
    forecastDetailsClosed: (() => {
      const d = document.querySelector('[data-brief-block="active-forecast"] details');
      return d ? !d.open : null;
    })(),
  };
})()`;
(async () => {
  const chrome = spawn(browserPath, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--disable-extensions', '--disable-sync', '--disable-background-networking',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
  let socket = null;
  const results = {};
  const failPending = () => {};
  try {
    const portFile = path.join(profile, 'DevToolsActivePort');
    const port = await until(() => fs.existsSync(portFile) &&
      fs.readFileSync(portFile, 'utf8').split('\n')[0], 'debugger port file', BOOT_TIMEOUT_MS);
    // The port file can appear moments before the HTTP endpoint accepts; retry
    // a bounded number of times instead of failing on an empty first response.
    const version = await until(async () => {
      try {
        const response = await fetch('http://127.0.0.1:' + port + '/json/version');
        const text = await response.text();
        return text ? JSON.parse(text) : null;
      } catch { return null; }
    }, 'json/version', COMMAND_TIMEOUT_MS);
    socket = new WebSocket(version.webSocketDebuggerUrl);
    await withTimeout(new Promise((resolve, reject) => {
      socket.onopen = resolve; socket.onerror = reject;
    }), SOCKET_TIMEOUT_MS, 'websocket open');
    let id = 0;
    const pending = new Map();
    socket.onmessage = event => {
      const data = JSON.parse(event.data);
      if (data.id) {
        const callbacks = pending.get(data.id); pending.delete(data.id);
        if (callbacks) {
          clearTimeout(callbacks.timer);
          if (data.error) callbacks.reject(new Error(data.error.message));
          else callbacks.resolve(data.result);
        }
      }
    };
    socket.onclose = () => {
      for (const callbacks of pending.values()) { clearTimeout(callbacks.timer); callbacks.reject(new Error('socket closed')); }
      pending.clear();
    };
    function send(method, params = {}, sessionId) {
      return new Promise((resolve, reject) => {
        const next = ++id;
        const timer = setTimeout(() => { pending.delete(next); reject(new Error('CDP command timed out: ' + method)); }, COMMAND_TIMEOUT_MS);
        pending.set(next, {resolve, reject, timer});
        try { socket.send(JSON.stringify({id: next, method, params, ...(sessionId ? {sessionId} : {})})); }
        catch (error) { clearTimeout(timer); pending.delete(next); reject(error); }
      });
    }
    const target = await send('Target.createTarget', {url: 'about:blank'});
    const {sessionId} = await send('Target.attachToTarget', {targetId: target.targetId, flatten: true});
    const cmd = (method, params) => send(method, params, sessionId);
    await cmd('Page.enable'); await cmd('Runtime.enable');
    await cmd('Page.addScriptToEvaluateOnNewDocument', {
      // The fixture API reads its scenario knobs from this object; the capture
      // uses the defaults (brief ok, active forecast ok, shadow ok).
      source: 'window.__FIXTURE_STATE__ = {};\n' +
              'window.__consoleErrors = [];\n' +
              'window.addEventListener("error", e => window.__consoleErrors.push(String(e.message)));'});
    async function evaluate(expression) {
      const result = await cmd('Runtime.evaluate', {expression, returnByValue: true,
        awaitPromise: true, userGesture: true});
      if (result.exceptionDetails) {
        throw new Error(result.exceptionDetails.text + ': ' +
          (result.exceptionDetails.exception && result.exceptionDetails.exception.description));
      }
      return result.result.value;
    }
    const get = expression => evaluate('(' + expression + ')');
    const wait = (expression, message) => until(() => get('Boolean(' + expression + ')'), message, PAGE_TIMEOUT_MS);
    async function screenshot(filePath, fullPage = true) {
      const shot = await cmd('Page.captureScreenshot', {format: 'png', captureBeyondViewport: fullPage});
      fs.writeFileSync(filePath, Buffer.from(shot.data, 'base64'));
      return filePath;
    }
    async function loadAndMeasure(viewportName) {
      await cmd('Page.navigate', {url: base + '/'});
      await wait('document.readyState === "complete"', 'page load');
      await wait('document.querySelector("[data-brief-state=\\"ready\\"]")', 'brief ready');
      const errors = await get('window.__consoleErrors');
      check(Array.isArray(errors) && errors.length === 0, 'page errors: ' + JSON.stringify(errors));
      const measurement = await get(MEASURE);
      return measurement;
    }

    // Desktop first: 1440x900.
    await cmd('Emulation.setDeviceMetricsOverride',
      {width: 1440, height: 900, deviceScaleFactor: 1, mobile: false});
    results.desktop = await loadAndMeasure('desktop');
    // Keyboard: one Tab press must move focus to an interactive element with a
    // visible focus indicator (focus-visible outline), per the design contract.
    await cmd('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9});
    await cmd('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9});
    await sleep(100);
    results.desktop.keyboardFocus = await get(`(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return {focused: null};
      const cs = getComputedStyle(el);
      // The Button's own indicator is a ring (box-shadow); the global
      // :focus-visible outline is killed by outline-none on the component. A
      // visible indicator exists when either fires.
      return {focused: el.tagName.toLowerCase() + ':' + (el.textContent || '').trim().slice(0, 30),
              outlineStyle: cs.outlineStyle, outlineWidth: cs.outlineWidth,
              boxShadow: cs.boxShadow === 'none' ? 'none' : cs.boxShadow.slice(0, 120)};
    })()`);
    await screenshot(path.join(outDir, 'desktop-overview.png'));
    // Open the forecast disclosure and capture immediately: a background
    // re-render may reset the uncontrolled <details>, so open+shoot+verify in
    // one tight loop instead of waiting for the state to stick.
    let forecastShot = false;
    for (let attempt = 0; attempt < 5 && !forecastShot; attempt++) {
      const opened = await get(`(() => {
        const d = document.querySelector('[data-brief-block="active-forecast"] details');
        if (!d) return false;
        d.scrollIntoView({block: 'center'});
        d.open = true;
        return d.open === true; })()`);
      check(opened, 'the forecast disclosure did not exist on the desktop view');
      await screenshot(path.join(outDir, 'desktop-forecast-open.png'));
      forecastShot = await get(
        `document.querySelector('[data-brief-block="active-forecast"] details')?.open === true`);
    }
    check(forecastShot, 'the forecast disclosure kept closing before the shot');
    results.desktop.forecastDisclosureOpened = true;

    // Mobile: 390x844, mobile emulation, fresh load so reflow is real.
    await cmd('Emulation.setDeviceMetricsOverride',
      {width: 390, height: 844, deviceScaleFactor: 2, mobile: true});
    results.mobile = await loadAndMeasure('mobile');
    await screenshot(path.join(outDir, 'mobile-overview.png'));

    process.stdout.write(JSON.stringify({result: 'ok', browser: version.Browser, results}));
  } finally {
    if (socket && socket.readyState <= 1) socket.close();
    reapProcessTree(chrome);
  }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
"""


def main() -> int:
    web = require_web_renderer()
    browser = _browser()
    if not browser:
        print("FAIL: no Chrome/Chromium/Edge browser found for the capture")
        return 1
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="ux-evidence-next-"))
    server = None
    logfile = None
    try:
        print(f"[fixture] building production fixture in {root}")
        build_dashboard_fixture(web, root)
        port = _port()
        from tests.browser.fixture_server import (
            start_production_server,
            stop_production_server,
            wait_for_server,
        )

        server, logfile = start_production_server(web, root, port)
        url = f"http://127.0.0.1:{port}"
        wait_for_server(server, url)
        print(f"[fixture] serving {url}")

        profile = root / "capture-profile"
        env = {**os.environ, "TEST_BROWSER": browser, "TEST_BASE": url,
               "TEST_PROFILE": str(profile), "TEST_OUT": str(ARTIFACTS)}
        (root / "capture.cjs").write_text(CAPTURE_DRIVER, encoding="utf-8")
        result = subprocess.run(
            ["node", str(root / "capture.cjs")], cwd=root, env=env,
            capture_output=True, check=False, encoding="utf-8", timeout=600,
        )
        if result.returncode != 0 or not result.stdout.strip():
            print(f"FAIL: capture driver failed\n{result.stdout}\n{result.stderr}")
            return 1
        payload = json.loads(result.stdout)
        measurements_path = ARTIFACTS / "measurements.json"
        measurements_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[done] measurements -> {measurements_path}")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    finally:
        if server is not None and logfile is not None:
            stop_production_server(server, logfile)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
