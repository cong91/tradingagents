"""Offline browser coverage for the dashboard operating brief.

Renders the real ``OverviewDashboard`` (branch code, no repo file edited) against
a fixture API injected into the page, and drives a real Chrome over CDP. The
browser is launched with ``--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE
127.0.0.1``, so the page can reach only the fixture server: "offline" is a
property of the environment, not an assertion about intent.

What this pins (each named test states the finding it locks down):

* B1 — a coverage error with NO net rows still renders its coverage row and the
  manual/unlinked fill count, instead of vanishing behind an early return;
* B2 — percentages carry exactly one ``%`` (``18.4%%`` was the bug);
* B3 — the halt row's label and value are not the same words twice;
* B4 — a failing brief does NOT hide the halt / pending-approval warnings;
* B5 — a slow brief does NOT blank the panels that own their own data;
* the four per-panel states, the Spot/Futures scope filter, drilldown links, and
  the retry path.

The fixture serves only GET; any other method returns 404 so an accidental write
fails loudly. The suite is marked ``integration`` because it needs a browser.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.browser.dashboard_fixture import build_dashboard_fixture
from tests.browser.fixture_server import (
    start_production_server,
    stop_production_server,
    wait_for_server,
)
from tests.support.node_renderer import require_web_renderer

# The fixture state is chosen per run through TEST_STATE, so one Next build
# serves every scenario instead of rebuilding per case.

DRIVER = r"""
const {spawn, execSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const browserPath = process.env.TEST_BROWSER;
const base = process.env.TEST_BASE;
const profile = process.env.TEST_PROFILE;
const state = JSON.parse(process.env.TEST_STATE);
// Every wait has a bound: page load, browser boot, the websocket, and each
// individual CDP command. Nothing here can hang forever, and the driver always
// exits on its own so the pytest-side timeout is a backstop, not the mechanism.
const COMMAND_TIMEOUT_MS = 30000;
const SOCKET_TIMEOUT_MS = 20000;
const BOOT_TIMEOUT_MS = 40000;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function check(ok, message) { if (!ok) throw new Error(message); }
function withTimeout(promise, timeout, message) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Timed out: ' + message)), timeout); }),
  ]).finally(() => clearTimeout(timer));
}
async function until(fn, message, timeout = 40000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { const value = await fn(); if (value) return value; await sleep(50); }
  throw new Error('Timed out: ' + message);
}
/** Kill the whole process tree of a child this driver spawned — on assertion
 * failure too. Only children created by THIS driver are touched. */
function reapProcessTree(child) {
  if (!child || child.pid === undefined) return;
  if (process.platform === 'win32') {
    try {
      execSync(`taskkill /pid ${child.pid} /T /F`, {stdio: 'ignore'});
      return;
    } catch { /* fall through to kill() */ }
  }
  try { child.kill('SIGKILL'); } catch { /* already gone */ }
}
(async () => {
  const chrome = spawn(browserPath, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--disable-extensions', '--disable-sync', '--disable-background-networking',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
  let socket = null;
  let failPending = () => {};
  try {
    const portFile = path.join(profile, 'DevToolsActivePort');
    const port = await until(() => {
      if (!fs.existsSync(portFile)) return null;
      try { return fs.readFileSync(portFile, 'utf8').split('\n')[0]; }
      catch (error) {
        if (['EBUSY', 'EACCES', 'ENOENT'].includes(error.code)) return null;
        throw error;
      }
    }, 'debugger port file', BOOT_TIMEOUT_MS);
    const version = await withTimeout((async () => {
      const response = await fetch('http://127.0.0.1:' + port + '/json/version');
      if (!response.ok) throw new Error('json/version failed: ' + response.status);
      return response.json();
    })(), COMMAND_TIMEOUT_MS, 'json/version');
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
    failPending = reason => {
      for (const callbacks of pending.values()) {
        clearTimeout(callbacks.timer);
        callbacks.reject(new Error(reason));
      }
      pending.clear();
    };
    socket.onclose = () => failPending('the DevTools websocket closed');
    function send(method, params = {}, sessionId) {
      return new Promise((resolve, reject) => {
        const next = ++id;
        const timer = setTimeout(() => {
          pending.delete(next);
          reject(new Error('CDP command timed out: ' + method));
        }, COMMAND_TIMEOUT_MS);
        pending.set(next, {resolve, reject, timer});
        try {
          socket.send(JSON.stringify({id: next, method, params, ...(sessionId ? {sessionId} : {})}));
        } catch (error) {
          clearTimeout(timer); pending.delete(next); reject(error);
        }
      });
    }
    const target = await send('Target.createTarget', {url: 'about:blank'});
    const {sessionId} = await send('Target.attachToTarget', {targetId: target.targetId, flatten: true});
    const cmd = (method, params) => send(method, params, sessionId);
    await cmd('Page.enable'); await cmd('Runtime.enable');
    await cmd('Runtime.addBinding', {name: '__fixtureLog'}).catch(() => {});
    if (state !== null) await cmd('Page.addScriptToEvaluateOnNewDocument', {
      source: 'window.__FIXTURE_STATE__ = ' + JSON.stringify(state) + ';\n' +
              'window.__consoleErrors = [];\n' +
              'window.addEventListener("error", e => window.__consoleErrors.push(String(e.message)));'});
    else await cmd('Page.addScriptToEvaluateOnNewDocument', {
      source: 'window.__consoleErrors = [];\n' +
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
    const wait = (expression, message, timeout) =>
      until(() => get('Boolean(' + expression + ')'), message, timeout);
    const text = selector => get(`(() => { const e = document.querySelector(${JSON.stringify(selector)});
      return e ? e.textContent : null; })()`);

    // The fixture server (Next, production build) has no /api routes: the
    // injected fetch intercepts them, so a page load is a real Next render
    // with instant fixture data.
    await cmd('Page.navigate', {url: base + '/'});
    await wait('document.readyState === "complete"', 'page load');

    const results = {};

    if (state === null) {
      await wait('document.querySelector("[data-brief-state=\\"ready\\"]")', 'manual dashboard ready');
      await wait('document.querySelector("[data-fixture-manual=\\"true\\"]")', 'manual fixture badge');
      const response = await evaluate("fetch('/api/__fixture_unknown_probe__').then(response => response.status)");
      check(response === 404, 'unknown API did not return the fixture server 404: ' + response);
      const requests = await get('window.__apiRequests');
      check(requests.some(request => request.path === '/api/__fixture_unknown_probe__' && request.method === 'GET'),
        'unknown API probe was not recorded by the fixture fetch guard');
      results.manualOpen = 'safe default state, visible fixture badge, local unknown API 404';
    } else if (state.brief === 500) {
      // B4: the brief failed, so its attention list is gone. The halt and the
      // pending approvals must still be on screen, from the health poll.
      await wait('document.body.textContent.includes("Could not read the brief")',
        'brief error state');
      const body = await get('document.body.textContent');
      check(/halt/i.test(body), 'B4: halt warning vanished when the brief failed');
      check(/awaiting approval/i.test(body),
        'B4: pending approvals vanished when the brief failed');
      results.b4 = 'halt+approvals survive a brief failure';
    } else {
      await wait('document.querySelector("[data-brief-state=\\"ready\\"]")', 'brief ready');
      const bodyText = await get('document.body.textContent');

      // B1: coverage evidence and manual fills must render even with no net rows.
      if (state.coverageOnly) {
        const rows = await get('document.querySelectorAll("[data-coverage-status]").length');
        check(rows > 0, 'B1: coverage row missing when net_by_currency is empty');
        const manual = await get('!!document.querySelector("[data-brief-block=\\"unattributed\\"]")');
        check(manual, 'B1: manual/unlinked fills block missing when net rows are empty');
        const netRows = await get('document.querySelectorAll("[data-net-currency]").length');
        check(netRows === 0, 'B1 fixture expected zero net rows, saw ' + netRows);
        results.b1 = 'coverage + manual fills render without net rows';
      } else {
        const netRows = await get('document.querySelectorAll("[data-net-currency]").length');
        check(netRows === 1, 'expected one net currency row, saw ' + netRows);
      }

      // B2: exactly one % on the exposure row and in the limits disclosure.
      const exposure = await text('[data-brief-row="exposure"]');
      check(!/%%/.test(exposure), 'B2: doubled percent in exposure row: ' + exposure);
      check(/18[.,]4%/.test(exposure), 'B2: exposure percent missing: ' + exposure);
      await evaluate('document.querySelectorAll("details")[0].open = true');
      const limits = await text('details');
      check(!/%%/.test(limits), 'B2: doubled percent in limits: ' + limits);
      check(/80%/.test(limits), 'B2: total exposure limit missing: ' + limits);
      results.b2 = 'single percent sign in exposure and limits';
      // W3 semantics: exactly one h1, section titles are h2, and no level is
      // skipped on the way down (an h3 with no h2 above it is the exact defect
      // the audit found: h2=0, h3=9). Asserted on the live DOM, not source.
      const headings = await get(`(() => {
        const hs = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
          .map(h => ({level: +h.tagName[1], text: h.textContent.trim().slice(0, 40)}));
        const h1 = hs.filter(h => h.level === 1).length;
        // A skipped level: some heading's level exceeds its nearest
        // preceding-or-ancestor heading's level by more than 1.
        let skipped = null;
        let prev = 0;
        for (const h of hs) { if (h.level > prev + 1 && prev !== 0) { skipped = h; break; } prev = h.level; }
        return {h1, h2: hs.filter(h => h.level === 2).length,
                h3: hs.filter(h => h.level === 3).length, skipped, sample: hs.slice(0, 3)}; })()`);
      check(headings.h1 === 1,
        'W3: the dashboard must have exactly one h1, saw ' + headings.h1);
      // The section cards that always render (the Portfolio card 404s in this
      // fixture, so it is asserted only when present). Every one of these is a
      // real page-region title and must be an h2, not a bare div.
      const h2Text = await get(`Array.from(document.querySelectorAll('h2'))
        .map(h => h.textContent.trim())`);
      // The sections this fixture provably renders. "Current activity" is
      // excluded: its card only mounts with live autoloop data, so demanding it
      // here would assert a section the fixture cannot produce.
      for (const expected of ["Operating brief", "Portfolio", "Trading performance",
                              "Decision performance", "Recent executed orders"]) {
        check(h2Text.some(t => t.includes(expected)),
          'W3: section "' + expected + '" is not an h2 (saw ' + JSON.stringify(h2Text) + ')');
      }
      check(headings.h2 >= 4,
        'W3: section cards are not h2 headings: ' + JSON.stringify(headings));
      check(headings.skipped === null,
        'W3: heading level skipped: ' + JSON.stringify(headings.skipped));
      // The brief's own title must be a real heading, not a div (outline entry).
      const briefHeading = await get(`(() => {
        const card = document.querySelector('[data-brief-state]');
        const h = card && card.closest('[data-slot="card"]')
          ?.querySelector('[data-slot="card-title"]');
        return h ? h.tagName : null; })()`);
      check(briefHeading === "H2",
        'W3: the brief card title is not an h2, saw ' + briefHeading);
      results.w3 = JSON.stringify(headings);

      // B3: the halt row must not repeat its own label as the value.
      const halted = await text('[data-brief-row="halted"]');
      const words = halted.replace(/\s+/g, ' ').trim();
      const half = Math.floor(words.length / 2);
      check(words.slice(0, half).trim() !== words.slice(half).trim(),
        'B3: halt label and value are the same words: ' + words);
      results.b3 = 'halt row reads as label + value: ' + words;

      // Drift: the server sends a fraction, the UI shows a percentage.
      const coverageText = await text('[data-coverage-status]');
      if (/partial/.test(await get('document.querySelector("[data-coverage-status]").getAttribute("data-coverage-status")'))) {
        check(/1[.,]2|1[.,]3/.test(coverageText),
          'drift fraction was not converted to a percentage: ' + coverageText);
      }
      results.drift = coverageText;

      // Drilldown links exist and point at real screens.
      const links = await get(`Array.from(document.querySelectorAll("[data-attention-kind] a"))
        .map(a => a.getAttribute("href"))`);
      check(links.length > 0, 'no attention drilldown links');
      check(links.every(href => /^\/(audit|cycles|approvals|settings)/.test(href)),
        'attention link points somewhere unexpected: ' + JSON.stringify(links));
      results.drilldown = links;

      // B6: when the halted state is set, the warning must appear exactly ONCE
      // as an attention item and NOT as a fallback banner. A brief that has
      // data owns that warning; restating it here would train the operator to
      // ignore banners (measured before the fix: the halt appeared 3 times).
      if (state.halted === true) {
        const haltedAttention = await get(
          `document.querySelectorAll("[data-attention-kind=\\"risk_halted\\"]").length`);
        const haltedBanner = await get(
          `document.querySelectorAll("[data-fallback-banner=\\"halt\\"]").length`);
        check(haltedAttention === 1,
          'B6: expected exactly one halt attention item, saw ' + haltedAttention);
        check(haltedBanner === 0,
          'B6: halt banner shown while the brief still has its attention list');
        results.b6 = 'halt stated once (attention), no fallback banner';
      }

      // Stage 4 shadow slot: each "nothing recorded yet" state must render its
      // OWN sentence. Before this, one shared line said "not enabled" even when
      // the worker was enabled, which is a false statement about the operator's
      // configuration.
      const shadowState = await get(
        `(document.querySelector("[data-shadow-state]") || {}).getAttribute
           ? document.querySelector("[data-shadow-state]").getAttribute("data-shadow-state")
           : null`);
      const shadowText = await get(
        `(() => { const e = document.querySelector("[data-brief-block=\\"shadow\\"]");
          return e ? e.textContent : null; })()`);
      check(shadowText, 'shadow block missing from the brief');
      if (state.shadowState === 'disabled') {
        check(shadowState === 'disabled', 'expected shadow state disabled, saw ' + shadowState);
        check(/disabled|off/i.test(shadowText), 'disabled copy missing: ' + shadowText);
        check(!/no observation has been recorded/i.test(shadowText),
          'disabled state used the empty-state sentence: ' + shadowText);
      } else if (state.shadowState === 'empty') {
        check(shadowState === 'empty', 'expected shadow state empty, saw ' + shadowState);
        check(/no observation has been recorded|no observation has been/i.test(shadowText),
          'empty copy missing: ' + shadowText);
        check(!/disabled in Settings|Observation is off/i.test(shadowText),
          'EMPTY state wrongly told the operator observation is off: ' + shadowText);
      } else if (state.shadowState === 'error') {
        check(shadowState === 'error', 'expected shadow state error, saw ' + shadowState);
        check(/could not read/i.test(shadowText), 'read-failure copy missing: ' + shadowText);
        check(!/Observation is off/i.test(shadowText),
          'READ-FAILURE state wrongly said observation is off: ' + shadowText);
      } else {
        // Populated: provider counts carry a denominator and the calibration
        // note rides along, so a sample ratio cannot be read as a probability.
        const providerEl = await get(
          `document.querySelectorAll("[data-shadow-provider]").length`);
        check(providerEl === 1, 'expected one shadow provider block, saw ' + providerEl);
        check(/not a calibrated probability/i.test(shadowText),
          'calibration note missing beside the ratio: ' + shadowText);
        check(/no ratio, band or dispersion/i.test(shadowText),
          'deterministic-rule note missing: ' + shadowText);
        const kronos = await get(
          `(document.querySelector("[data-shadow-kronos]") || {}).getAttribute
             ? document.querySelector("[data-shadow-kronos]").getAttribute("data-shadow-kronos")
             : null`);
        check(kronos === String(Boolean(state.shadowKronos)),
          'kronos availability not reported truthfully: ' + kronos);
      }
      results.shadow = JSON.stringify({state: shadowState});

      // The ACTIVE forecast block: its own section, separate from the shadow
      // block, one per page, keyed by state. Every state the server can report
      // must render its own sentence.
      const activeState = await get(
        `document.querySelector("[data-brief-block=\\"active-forecast\\"]")
           ?.getAttribute("data-active-forecast-state") ?? null`);
      check(activeState !== null, 'the active forecast block is missing from the brief');
      const activeNothing = await get(
        `document.querySelector("[data-active-forecast-nothing]")?.getAttribute("data-active-forecast-nothing") ?? null`);
      const activeFailed = await get(
        `!!document.querySelector("[data-active-forecast-failed]")`);
      const shadowBlocks = await get(
        `document.querySelectorAll("[data-brief-block=\\"shadow\\"]").length`);
      check(shadowBlocks === 1, 'expected exactly one shadow block, saw ' + shadowBlocks);
      if (state.forecastState === 'off') {
        check(activeState === 'off', 'expected active forecast state off, saw ' + activeState);
        check(activeNothing === 'off', 'the off state must render its own sentence');
        check(/Off in Settings/i.test(bodyText),
          'the off state does not say the switch is off: ' + JSON.stringify(results));
      } else if (state.forecastState === 'empty') {
        check(activeState === 'empty', 'expected active forecast state empty, saw ' + activeState);
        check(activeNothing === 'empty', 'the empty state must render its own sentence');
        check(/no recorded cycle has a Kronos forecast/i.test(bodyText),
          'the empty state copy is missing');
      } else if (state.forecastState === 'error') {
        check(activeState === 'error', 'expected active forecast state error, saw ' + activeState);
        check(activeNothing === 'error', 'the error state must render its own sentence');
        check(/could not be read/i.test(bodyText), 'the read-failure copy is missing');
      } else if (state.forecastState === 'unavailable') {
        check(activeState === 'unavailable', 'expected unavailable, saw ' + activeState);
        check(activeFailed, 'the unavailable state must show the failure, with no numbers');
        const numbers = await get(
          `document.querySelectorAll("[data-brief-block=\\"active-forecast\\"] details").length`);
        check(numbers === 0, 'a failed forecast rendered number rows: ' + numbers);
      } else if (state.forecastState === 'stale') {
        check(activeState === 'stale', 'expected stale, saw ' + activeState);
        check(/window has fully elapsed|fully elapsed/i.test(bodyText),
          'the stale state does not say the window has elapsed');
      } else {
        check(activeState === 'ok', 'expected active forecast state ok, saw ' + activeState);
        // The numbers live behind progressive disclosure: closed by default,
        // one details element, and every required field present once opened.
        const before = await get(
          `document.querySelectorAll("[data-brief-block=\\"active-forecast\\"] details").length`);
        check(before === 1, 'expected one disclosure in the active forecast, saw ' + before);
        const openState = await get(
          `document.querySelector("[data-brief-block=\\"active-forecast\\"] details").open`);
        check(openState === false, 'the forecast details must start closed (compact fold)');
        await evaluate(
          `document.querySelector("[data-brief-block=\\"active-forecast\\"] details").open = true`);
        const detailText = await get(
          `(() => { const e = document.querySelector("[data-brief-block=\\"active-forecast\\"] details");
            return e ? e.textContent : null; })()`);
        check(/9\/16/.test(detailText), 'direction sample ratio with denominator missing: ' + detailText);
        check(/24h/.test(detailText), 'horizon missing from the forecast detail: ' + detailText);
        check(/60,100|60100/.test(detailText),
          'the p50 band value is missing: ' + detailText);
        check(/USDT/.test(detailText), 'the band unit (quote currency) is missing: ' + detailText);
        check(/2,512|2512/.test(detailText), 'wall latency missing: ' + detailText);
        check(/kronos-fixture/.test(detailText), 'model revision missing: ' + detailText);
        check(/\d{1,2}\/\d{1,2}\/\d{2,4}/.test(detailText),
          'cutoff/generated/recorded times missing: ' + detailText);
        const policy = await text('[data-active-forecast-policy]');
        check(/placed into the decision context/.test(policy),
          'the use policy is not stated: ' + policy);
        // The active forecast is its own section heading — it must not merge
        // into the shadow block.
        const activeTitle = await get(`(() => {
          const block = document.querySelector("[data-brief-block=\\"active-forecast\\"]");
          const section = block && block.closest('section');
          return section ? section.querySelector('h3')?.textContent : null; })()`);
        check(activeTitle && /Kronos forecast/i.test(activeTitle),
          'the active forecast is not its own titled section: ' + activeTitle);
      }
      results.activeForecast = JSON.stringify({state: activeState});

      // UX plan §⑤: "Recent executed orders" is a digest — capped rows plus a
      // link to the full history. The overview must not render an unbounded
      // audit table (the fallback halt banner link is NOT this link: it is
      // absent while the brief has data, so this asserts the section's own).
      const auditLink = await get(`(() => {
        const a = Array.from(document.querySelectorAll('a[href="/audit"]'));
        return {count: a.length, text: a.length ? a[a.length - 1].textContent.trim() : ''}; })()`);
      check(auditLink.count >= 1,
        'no link to /audit on the dashboard: ' + JSON.stringify(auditLink));
      check(/Lịch sử|History/i.test(auditLink.text),
        'the /audit link does not mention the history screen: ' + JSON.stringify(auditLink));
      // The fixture serves 14 executed rows, so the cap must actually bite.
      const auditRows = await get(`(() => {
        const title = Array.from(document.querySelectorAll('[data-slot="card-title"]'))
          .find(t => /Lệnh thực thi gần đây|Recent executed orders/i.test(t.textContent));
        const card = title && title.closest('[data-slot="card"]');
        const table = card && card.querySelector('table');
        return table ? table.querySelectorAll('tbody tr').length : -1; })()`);
      check(auditRows === 10,
        'the overview audit table should be capped at 10 of 14 rows, saw ' + auditRows);
      check(/10\/14/.test(auditLink.text),
        'the /audit link does not disclose the shown/total count: ' + JSON.stringify(auditLink));
      results.audit = JSON.stringify({link: auditLink, rows: auditRows});

      // Scope filter: Spot hides the futures-only activity block AND the
      // Futures section of the Portfolio card; All restores both.
      // Compare only the POSITION tables: the fills/events tables appear and
      // disappear with their own polls, so a raw document table count is racy.
      const positionTables = () => get(`Array.from(document.querySelectorAll("table caption"))
        .filter(c => /Futures positions|Spot positions/i.test(c.textContent)).length`);
      const positionTablesAll = await positionTables();
      check(positionTablesAll === 2,
        'expected spot + futures position tables on All scope, saw ' + positionTablesAll);
      await evaluate(`Array.from(document.querySelectorAll('button'))
        .find(b => b.textContent.trim() === 'Spot').click()`);
      await wait(`Array.from(document.querySelectorAll('button'))
        .some(b => b.textContent.trim() === 'Spot' && b.getAttribute('aria-pressed') === 'true')`,
        'spot scope pressed');
      const spotState = await get(`(() => {
        const rows = Array.from(document.querySelectorAll("[data-brief-row]"))
          .map(r => r.getAttribute("data-brief-row"));
        return {rows, futures: rows.includes("open_futures"), exposure: rows.includes("exposure")}; })()`);
      check(!spotState.futures && !spotState.exposure,
        'Spot scope still shows futures-only rows: ' + JSON.stringify(spotState));
      // The Futures position table must be gone from the Portfolio card too,
      // not just from the brief (scope is a page filter, not a brief filter).
      await until(async () => (await positionTables()) === 1,
        'Spot scope did not drop the Futures position table');
      check(!(await get('document.body.textContent.includes("Futures positions")')),
        'Spot scope still renders the Futures positions heading');
      await evaluate(`Array.from(document.querySelectorAll('button'))
        .find(b => b.textContent.trim() === 'All').click()`);
      await wait('document.querySelector("[data-brief-row=\\"exposure\\"]")', 'All scope restores');
      await until(async () => (await positionTables()) === 2,
        'All scope did not restore both position tables');
      results.scope = JSON.stringify({positionTablesAll, spot: spotState.rows});

      // No control on this page may change state.
      const writes = await get(`window.__apiRequests.filter(r => r.method !== "GET")`);
      check(writes.length === 0, 'a non-GET request was issued: ' + JSON.stringify(writes));
      const ports = await get(`window.__apiRequests.filter(r => r.path === "/api/portfolio").length`);
      check(ports === 0, 'the dashboard still polls /api/portfolio separately: ' + ports);
      results.requests = 'GET only; /api/portfolio not polled';
    }

    const errors = await get('window.__consoleErrors');
    check(Array.isArray(errors) && errors.length === 0,
      'page reported errors: ' + JSON.stringify(errors));
    process.stdout.write(JSON.stringify({result: 'pass', state: state?.name ?? 'manual_open', results,
      browser: version.Browser}));
  } finally {
    if (socket && typeof failPending === 'function') {
      if (socket.readyState <= 1) socket.close();
      failPending('driver finished');
    }
    reapProcessTree(chrome);
  }
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


# name -> fixture state driven into the page
SCENARIOS = {
    "ready": {"name": "ready"},
    "manual_open": None,
    "coverage_only": {"name": "coverage_only", "coverageOnly": True},
    "brief_error": {"name": "brief_error", "brief": 500, "halted": True, "approvals": 2},
    "halted": {"name": "halted", "halted": True, "approvals": 2},
    "production_venue": {"name": "production_venue", "venueProfile": "production"},
    # Stage 4: each "nothing recorded yet" state must say its own thing.
    "shadow_disabled": {"name": "shadow_disabled", "shadowState": "disabled"},
    "shadow_empty": {"name": "shadow_empty", "shadowState": "empty"},
    "shadow_error": {"name": "shadow_error", "shadowState": "error"},
    "shadow_kronos_ready": {"name": "shadow_kronos_ready", "shadowState": "ok",
                            "shadowKronos": True},
    # Stage 5 active forecast: the ok numbers ride the ready scenario; here the
    # off switch renders its own sentence and nothing pretends a forecast ran.
    "forecast_off": {"name": "forecast_off", "forecastState": "off"},
    # A forecast whose window fully elapsed: history, not a current sample.
    "forecast_stale": {"name": "forecast_stale", "forecastState": "stale"},
}


@pytest.fixture(scope="module")
def api_trap():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(418)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.fixture(scope="module")
def dashboard_fixture(tmp_path_factory, api_trap):
    """A PRODUCTION Next server (next build + next start) with the REAL
    dashboard and an injected fixture API.

    The fixture was previously served by ``next dev`` while the runner and docs
    called it a production check — that claim was false, so the fixture now
    actually builds and serves the production output. The build is a real part
    of the check: a broken build fails the fixture with the build log.
    """
    web = require_web_renderer()
    root = tmp_path_factory.mktemp("brief-offline-next")
    try:
        build_dashboard_fixture(web, root, env_overrides={"API_INTERNAL_URL": api_trap[0]})
    except RuntimeError as exc:
        pytest.fail(str(exc))
    (root / "driver.cjs").write_text(DRIVER, encoding="utf-8")

    port = _port()
    server, logfile = start_production_server(web, root, port, env_overrides={"API_INTERNAL_URL": api_trap[0]})
    url = f"http://127.0.0.1:{port}"
    try:
        wait_for_server(server, url)
        yield root, url
    finally:
        stop_production_server(server, logfile)


@pytest.mark.integration
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_dashboard_brief_offline(dashboard_fixture, api_trap, tmp_path, scenario):
    browser = _browser()
    if not browser:
        pytest.skip("No Chrome, Chromium or Edge browser available")
    root, url = dashboard_fixture
    env = {**os.environ, "TEST_BROWSER": browser, "TEST_BASE": url,
           "TEST_PROFILE": str(tmp_path / f"profile-{scenario}"),
           "TEST_STATE": json.dumps(SCENARIOS[scenario])}
    result = subprocess.run(["node", str(root / "driver.cjs")], cwd=root, env=env,
                            capture_output=True, check=False, encoding="utf-8", timeout=300)
    assert result.returncode == 0, f"[{scenario}] {result.stdout}\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["result"] == "pass", payload
    assert api_trap[1] == [], "fixture API requests escaped to the configured upstream"
