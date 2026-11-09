"""Exercise Autoloop controls with mocked fetch and timers, without a live API."""

import json
import os
import subprocess

import pytest

from tests.support.node_renderer import require_web_renderer

pytestmark = pytest.mark.unit

NODE_TEST = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const webRoot = process.cwd();
const ts = require(require.resolve("typescript", { paths: [webRoot] }));
const React = require("react");
const test = JSON.parse(fs.readFileSync(0, "utf8"));

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

const settings = require(path.join(webRoot, "lib/i18n/messages/settings.ts"))[test.locale || "en"];
const originalLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "@/lib/i18n") return {
    useT: () => ({ settings, common: { retry: "Retry", cancel: "Cancel" } }),
    intlLocale: () => "en-US",
  };
  if (request === "@/lib/api") return {
    networkHint: () => "network unavailable",
    parseErrorResponse: async () => ({ code: null, message: "HTTP error" }),
  };
  if (request.startsWith("@/components/ui/")) {
    return new Proxy({}, { get: (_, name) => function Component() { return name; } });
  }
  return originalLoad.call(this, request, parent, isMain);
};

let timers = new Map();
let intervals = new Map();
let nextTimer = 1;
global.setTimeout = (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, ms }); return id; };
global.clearTimeout = (id) => timers.delete(id);
global.setInterval = (fn, ms) => { const id = nextTimer++; intervals.set(id, { fn, ms }); return id; };
global.clearInterval = (id) => intervals.delete(id);
function runTimeouts(ms) {
  for (const [id, timer] of [...timers]) if (timer.ms === ms) {
    timers.delete(id);
    timer.fn();
  }
}
function runIntervals(ms) {
  for (const timer of [...intervals.values()]) if (timer.ms === ms) timer.fn();
}

const queued = new Map();
const requests = [];
function enqueue(url, response) {
  const values = queued.get(url) || [];
  values.push(response);
  queued.set(url, values);
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
function response(data, ok = true) {
  return { ok, status: ok ? 200 : 500, json: async () => data };
}
global.fetch = async (url, init = {}) => {
  const method = init.method || "GET";
  requests.push({ url, method, body: init.body, cache: init.cache });
  const values = queued.get(`${method} ${url}`) || [];
  if (!values.length) throw new Error(`No mocked response for ${method} ${url}`);
  const value = values.shift();
  if (value instanceof Error) throw value;
  return await value;
};

let slots = [];
let cursor = 0;
let pendingEffects = [];
let currentComponent;
let currentProps;
let tree;
let active = true;
let scheduled = false;
function scheduleRender() {
  if (scheduled || !active) return;
  scheduled = true;
  queueMicrotask(() => { scheduled = false; if (active) render(); });
}
React.useState = (initial) => {
  const index = cursor++;
  if (!slots[index]) {
    const slot = { value: typeof initial === "function" ? initial() : initial };
    slot.set = (next) => {
      slot.value = typeof next === "function" ? next(slot.value) : next;
      scheduleRender();
    };
    slots[index] = slot;
  }
  return [slots[index].value, slots[index].set];
};
React.useRef = (value) => {
  const index = cursor++;
  if (!slots[index]) slots[index] = { current: value };
  return slots[index];
};
function sameDeps(left, right) {
  return left && right && left.length === right.length && left.every((v, i) => Object.is(v, right[i]));
}
React.useCallback = (callback, deps) => {
  const index = cursor++;
  if (!slots[index] || !sameDeps(slots[index].deps, deps)) slots[index] = { value: callback, deps };
  return slots[index].value;
};
React.useEffect = (effect, deps) => {
  const index = cursor++;
  if (!slots[index] || !sameDeps(slots[index].deps, deps)) {
    const previous = slots[index];
    const next = { deps };
    slots[index] = next;
    pendingEffects.push(() => {
      if (previous?.cleanup) previous.cleanup();
      next.cleanup = effect();
    });
  }
};
function render() {
  cursor = 0;
  pendingEffects = [];
  tree = currentComponent(currentProps);
  for (const effect of pendingEffects) effect();
}
function mount(component, props = {}) {
  currentComponent = component;
  currentProps = props;
  render();
}
function unmount() {
  active = false;
  for (const slot of slots) if (slot?.cleanup) slot.cleanup();
}
async function flush() {
  for (let i = 0; i < 6; i++) await new Promise((done) => setImmediate(done));
}
function elements(node) {
  if (arguments.length === 0) node = tree;
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!node || typeof node !== "object" || !node.props) return [];
  return [node, ...elements(node.props.children)];
}
function text(node) {
  if (arguments.length === 0) node = tree;
  if (Array.isArray(node)) return node.map(text).join("");
  if (typeof node === "string" || typeof node === "number") return String(node);
  return node?.props ? text(node.props.children) : "";
}
function button(label) {
  return elements().find((node) => typeof node.props.onClick === "function" && text(node).includes(label));
}
function input() { return elements().find((node) => node.props.id === "autoloop-interval"); }
function click(label) {
  const item = button(label);
  assert.ok(item, `missing button ${label}: ${text()}`);
  assert.notEqual(item.props.disabled, true);
  item.props.onClick();
}
const c = settings.autoloop;
const { AutoloopCard, AutoloopHaltControl } = require(
  path.join(webRoot, "components/settings/autoloop-card.tsx")
);
const payload = (interval) => ({
  generated_at: "2026-11-09T00:00:00Z", enabled: true, interval_minutes: interval,
  running_cycle: false, modes: { auto_spot: true, auto_futures: false, any_auto_enabled: true },
  last_cycle: null,
});

async function healthCase(test) {
  enqueue("GET /api/health", test.error ? new Error("offline") : response(test.health, !test.httpError));
  mount(AutoloopHaltControl);
  assert.ok(text().includes(c.haltLoading));
  assert.ok(button(c.haltAll));
  assert.equal(button(c.resetHalt), undefined);
  runTimeouts(0);
  await flush();
  assert.ok(text().includes(c[test.expected]), text());
  assert.equal(Boolean(button(c.resetHalt)), test.expected === "haltActive");
  assert.equal(Boolean(button(c.haltAll)), test.expected !== "haltActive");
  assert.deepEqual(requests.map(({ url, method }) => [url, method]), [["/api/health", "GET"]]);
  assert.equal(requests[0].cache, "no-store");
  unmount();
}

async function haltLifecycle() {
  const older = deferred();
  enqueue("GET /api/health", older.promise);
  mount(AutoloopHaltControl);
  runTimeouts(0);
  await flush();
  enqueue("GET /api/health", response({ audit_log_readable: true, halted_today: true }));
  runIntervals(30000);
  await flush();
  assert.ok(button(c.resetHalt));
  older.resolve(response({ audit_log_readable: true, halted_today: false }));
  await flush();
  assert.ok(button(c.resetHalt), "late health response must not overwrite newer snapshot");

  enqueue("GET /api/health", response({ audit_log_readable: false, halted_today: false }));
  runIntervals(30000);
  await flush();
  assert.ok(text().includes(c.haltUnknown));
  assert.equal(button(c.resetHalt), undefined);
  assert.ok(button(c.haltAll));

  enqueue("POST /api/halt", response({ halted: true }));
  enqueue("GET /api/health", response({ audit_log_readable: false, halted_today: false }));
  click(c.haltAll);
  await flush();
  assert.ok(text().includes(c.haltUnknown));
  assert.equal(button(c.resetHalt), undefined);
  assert.equal(requests.filter((r) => r.method === "POST").length, 1);
  assert.equal(requests.find((r) => r.method === "POST").url, "/api/halt");

  enqueue("GET /api/health", response({ audit_log_readable: true, halted_today: true }));
  runIntervals(30000);
  await flush();
  assert.ok(button(c.resetHalt));
  enqueue("GET /api/health", response({ audit_log_readable: true, halted_today: true }));
  enqueue("POST /api/halt/reset", response({ halted: false }));
  enqueue("GET /api/health", response({ audit_log_readable: false, halted_today: false }));
  click(c.resetHalt);
  await flush();
  assert.ok(text().includes(c.clearHaltConfirmTitle));
  assert.ok(text().includes(c.clearHaltConfirmDescription));
  assert.equal(requests.filter((r) => r.method === "POST").length, 1);
  click("Cancel");
  await flush();
  assert.equal(requests.filter((r) => r.method === "POST").length, 1);
  click(c.resetHalt);
  await flush();
  click(c.clearHaltConfirmAction);
  await flush();
  assert.ok(text().includes(c.haltUnknown));
  assert.ok(button(c.haltAll));
  assert.deepEqual(requests.filter((r) => r.method === "POST").map((r) => r.url), ["/api/halt", "/api/halt/reset"]);
  unmount();
}

async function resetRequiresFreshHealth() {
  enqueue("GET /api/health", response({ audit_log_readable: true, halted_today: true }));
  mount(AutoloopHaltControl);
  runTimeouts(0);
  await flush();
  click(c.resetHalt);
  await flush();
  const verification = deferred();
  enqueue("GET /api/health", verification.promise);
  click(c.clearHaltConfirmAction);
  click(c.clearHaltConfirmAction);
  await flush();
  assert.equal(requests.filter((r) => r.method === "POST").length, 0);
  assert.equal(requests.filter((r) => r.url === "/api/health").length, 2, "double confirmation starts one health check");
  runIntervals(30000);
  await flush();
  assert.equal(requests.filter((r) => r.url === "/api/health").length, 2, "poll must not supersede reset verification");
  assert.ok(button(c.haltAll));
  assert.equal(button(c.haltAll).props.disabled, false, "STOP stays available during verification");
  if (test.stopDuringRefresh) {
    enqueue("POST /api/halt", response({ halted: true }));
    enqueue("GET /api/health", response({ audit_log_readable: true, halted_today: true }));
    click(c.haltAll);
    await flush();
    verification.resolve(response({ audit_log_readable: true, halted_today: true }));
  } else if (test.failed) {
    verification.resolve(response({ audit_log_readable: false, halted_today: true }));
  } else {
    verification.resolve(response({ audit_log_readable: true, halted_today: false }));
  }
  await flush();
  assert.equal(requests.filter((r) => r.url === "/api/halt/reset").length, 0);
  if (!test.stopDuringRefresh) {
    assert.ok(text().includes(test.failed ? c.haltUnknown : c.haltInactive));
  }
  unmount();
}

async function healthRefreshTransitions() {
  enqueue("GET /api/health", response({ audit_log_readable: true, halted_today: true }));
  mount(AutoloopHaltControl);
  runTimeouts(0);
  await flush();
  assert.ok(button(c.resetHalt));
  click(c.resetHalt);
  await flush();
  assert.ok(button(c.clearHaltConfirmAction));
  const waiting = deferred();
  enqueue("GET /api/health", waiting.promise);
  runIntervals(30000);
  await flush();
  assert.equal(button(c.resetHalt), undefined, "a stale halt cannot authorize reset during refresh");
  assert.equal(button(c.clearHaltConfirmAction), undefined, "refresh closes stale confirmation");
  assert.ok(button(c.haltAll));
  waiting.resolve(response({ audit_log_readable: true, halted_today: false }));
  await flush();
  assert.ok(text().includes(c.haltInactive));
  assert.ok(button(c.haltAll));
  enqueue("GET /api/health", new Error("offline"));
  runIntervals(30000);
  await flush();
  assert.ok(text().includes(c.haltUnknown));
  assert.equal(button(c.resetHalt), undefined);
  enqueue("GET /api/health", response({ audit_log_readable: true, halted_today: true }));
  click("Retry");
  await flush();
  assert.ok(button(c.resetHalt), "retry reloads halt state");
  assert.equal(button(c.clearHaltConfirmAction), undefined, "confirmation does not reopen on recovery");
  assert.equal(requests.filter((r) => r.method === "POST").length, 0);
  unmount();
}

async function unavailableAfterPost() {
  enqueue("GET /api/health", response({ audit_log_readable: true, halted_today: false }));
  mount(AutoloopHaltControl);
  runTimeouts(0);
  await flush();
  const waitingHealth = deferred();
  enqueue("POST /api/halt", response({ halted: true }));
  enqueue("GET /api/health", waitingHealth.promise);
  click(c.haltAll);
  await flush();
  assert.ok(text().includes(c.haltLoading));
  assert.ok(button(c.haltAll));
  assert.equal(button(c.haltAll).props.disabled, false, "STOP remains usable during health refresh");
  assert.equal(button(c.resetHalt), undefined);
  waitingHealth.resolve(response({ audit_log_readable: false, halted_today: false }));
  await flush();
  assert.ok(text().includes(c.haltUnknown));
  unmount();
}

async function dirtyInterval() {
  const dirty = [];
  enqueue("GET /api/autoloop", response(payload(60)));
  mount(AutoloopCard, { hideHalt: true, onDirtyChange: (value) => dirty.push(value) });
  runTimeouts(0);
  await flush();
  assert.equal(input().props.value, "60");
  input().props.onChange({ target: { value: "90" } });
  await flush();
  assert.deepEqual(dirty, [true]);
  enqueue("GET /api/autoloop", response(payload(30)));
  runIntervals(30000);
  await flush();
  assert.equal(input().props.value, "90", "poll must not overwrite dirty draft");
  input().props.onChange({ target: { value: "30" } });
  await flush();
  assert.deepEqual(dirty, [true, false]);

  input().props.onChange({ target: { value: "90" } });
  await flush();
  const stale = deferred();
  enqueue("GET /api/autoloop", stale.promise);
  runIntervals(30000);
  await flush();
  enqueue("POST /api/autoloop", response(payload(90)));
  click(c.apply);
  await flush();
  assert.deepEqual(dirty, [true, false, true, false]);
  assert.equal(input().props.value, "90");
  stale.resolve(response(payload(30)));
  await flush();
  assert.equal(input().props.value, "90", "old poll must not overwrite completed save");
  assert.deepEqual(JSON.parse(requests.find((r) => r.method === "POST").body), {
    enabled: true, interval_minutes: 90,
  });
  unmount();
}

async function draftMatchesServer() {
  const dirty = [];
  enqueue("GET /api/autoloop", response(payload(60)));
  mount(AutoloopCard, { hideHalt: true, onDirtyChange: (value) => dirty.push(value) });
  runTimeouts(0);
  await flush();
  input().props.onChange({ target: { value: "90" } });
  await flush();
  enqueue("GET /api/autoloop", response(payload(90)));
  runIntervals(30000);
  await flush();
  assert.equal(input().props.value, "90");
  assert.deepEqual(dirty, [true, false]);
  unmount();
}

async function schedulerFailure() {
  enqueue("GET /api/autoloop", new Error("offline"));
  mount(AutoloopCard);
  assert.ok(elements().some((node) => node.type === AutoloopHaltControl));
  runTimeouts(0);
  await flush();
  assert.ok(elements().some((node) => node.type === AutoloopHaltControl));
  assert.ok(button("Retry"));
  unmount();
}

async function schedulerState() {
  const state = payload(60);
  state.enabled = test.enabled;
  state.modes = {
    auto_spot: test.mode, auto_futures: false, any_auto_enabled: test.mode,
  };
  enqueue("GET /api/autoloop", response(state));
  mount(AutoloopCard, { hideHalt: true });
  runTimeouts(0);
  await flush();
  assert.ok(text().includes(test.mode ? c.enabled : c.disabled));
  assert.ok(text().includes(test.enabled ? c.schedulerEnabled : c.schedulerInactive));
  assert.ok(text().includes(c.schedulerDescription));
  assert.equal(text().includes(c.description), false);
  assert.equal(text().includes(c.enabledFeedback(60)), false);
  assert.equal(button(c.apply).props.disabled, test.mode && !test.enabled);
  assert.equal(button(c.runNow).props.disabled, !test.mode);
  assert.equal(Boolean(button("Retry")), test.mode && !test.enabled);
  assert.equal(requests.filter((r) => r.method === "POST").length, 0);
  unmount();
}

async function staleScheduler() {
  const dirty = [];
  const initial = payload(60);
  initial.last_cycle = { started_at: "2026-11-09T00:00:00Z", finished_at: null,
    status: "running", reason: null, error: null, candidates: [], results: [] };
  initial.running_cycle = true;
  enqueue("GET /api/autoloop", response(initial));
  mount(AutoloopCard, { hideHalt: true, onDirtyChange: (value) => dirty.push(value) });
  runTimeouts(0);
  await flush();
  input().props.onChange({ target: { value: "90" } });
  await flush();
  enqueue("GET /api/autoloop", new Error("offline"));
  runIntervals(5000);
  await flush();
  assert.ok(text().includes(c.statusStale));
  assert.equal(text().includes(c.enabled), false);
  assert.equal(text().includes(c.modeAvailable), false);
  assert.equal(text().includes(c.lastCycle), false);
  assert.equal(text().includes(c.schedulerEnabled), false);
  assert.equal(input().props.value, "90");
  assert.equal(button(c.apply).props.disabled, true);
  assert.equal(button(c.runNow).props.disabled, true);
  input().props.onChange({ target: { value: "60" } });
  await flush();
  assert.deepEqual(dirty, [true], "a stale snapshot cannot mark a draft clean");
  input().props.onChange({ target: { value: "90" } });
  await flush();
  assert.deepEqual(dirty, [true]);
  assert.equal(requests.filter((r) => r.method === "POST").length, 0);
  enqueue("GET /api/autoloop", new Error("offline again"));
  click("Retry");
  await flush();
  assert.ok(text().includes(c.statusStale));
  assert.equal(input().props.value, "90");
  enqueue("GET /api/autoloop", response(payload(30)));
  click("Retry");
  await flush();
  assert.equal(text().includes(c.statusStale), false);
  assert.ok(text().includes(c.schedulerEnabled));
  assert.equal(input().props.value, "90");
  assert.equal(button(c.apply).props.disabled, false);
  assert.deepEqual(dirty, [true]);
  assert.equal(requests.filter((r) => r.method === "POST").length, 0);
  unmount();
}

async function saveReturnsInactive() {
  enqueue("GET /api/autoloop", response(payload(60)));
  mount(AutoloopCard, { hideHalt: true });
  runTimeouts(0);
  await flush();
  const inactive = payload(90);
  inactive.enabled = false;
  enqueue("POST /api/autoloop", response(inactive));
  input().props.onChange({ target: { value: "90" } });
  await flush();
  click(c.apply);
  await flush();
  assert.ok(text().includes(c.intervalSaved));
  assert.ok(text().includes(c.schedulerInactive));
  assert.equal(text().includes(c.enabledFeedback(90)), false);
  assert.deepEqual(JSON.parse(requests.find((r) => r.method === "POST").body), {
    enabled: true, interval_minutes: 90,
  });
  unmount();
}

async function fallbackCopy() {
  enqueue("GET /api/health", response({ audit_log_readable: true, halted_today: true }));
  mount(AutoloopHaltControl);
  runTimeouts(0);
  await flush();
  assert.match(text(), /UTC/);
  assert.match(text(), test.locale === "vi" ? /không đóng vị thế/i : /does not close positions/i);
  assert.match(text(), test.locale === "vi" ? /không hủy lệnh/i : /cancel existing orders/i);
  unmount();
}

async function fallbackRunCopy() {
  enqueue("GET /api/autoloop", response(payload(60)));
  mount(AutoloopCard, { hideHalt: true });
  runTimeouts(0);
  await flush();
  const label = test.locale === "vi" ? "Chạy ngay" : "Run now";
  assert.ok(button(label));
  assert.equal(button(c.runOnce), undefined);
  assert.match(text(), test.locale === "vi" ? /có thể đặt lệnh thật/i : /may place real orders/i);
  assert.equal(text().includes(c.description), false);
  unmount();
}

async function runNowCopy() {
  enqueue("GET /api/autoloop", response(payload(60)));
  mount(AutoloopCard, { hideHalt: true });
  runTimeouts(0);
  await flush();
  assert.ok(button(c.runNow));
  assert.ok(text().includes(c.runNowHint));
  assert.equal(button(c.runOnce), undefined);
  enqueue("POST /api/autoloop/run-now", response({ status: "started" }));
  click(c.runNow);
  await flush();
  assert.ok(text().includes(c.runStarted));
  assert.equal(text().includes(c.runStatus.started), false);
  assert.equal(requests.filter((r) => r.method === "POST")[0].url, "/api/autoloop/run-now");
  unmount();
}

(async () => {
  if (test.scenario === "health") await healthCase(test);
  else if (test.scenario === "halt") await haltLifecycle();
  else if (test.scenario === "health-transitions") await healthRefreshTransitions();
  else if (test.scenario === "reset-refresh") await resetRequiresFreshHealth();
  else if (test.scenario === "health-after-post") await unavailableAfterPost();
  else if (test.scenario === "dirty") await dirtyInterval();
  else if (test.scenario === "draft-matches-server") await draftMatchesServer();
  else if (test.scenario === "scheduler-error") await schedulerFailure();
  else if (test.scenario === "scheduler-state") await schedulerState();
  else if (test.scenario === "scheduler-stale") await staleScheduler();
  else if (test.scenario === "save-inactive") await saveReturnsInactive();
  else if (test.scenario === "fallback-copy") await fallbackCopy();
  else if (test.scenario === "fallback-run-copy") await fallbackRunCopy();
  else if (test.scenario === "run-now-copy") await runNowCopy();
  else throw new Error("unknown scenario");
  process.stdout.write("ok");
})().catch((err) => { console.error(err); process.exitCode = 1; });
"""


def _run_node(case):
    web_root = require_web_renderer()
    env = os.environ.copy()
    env.update({"TZ": "UTC", "NODE_ENV": "production"})
    result = subprocess.run(
        ["node", "-e", NODE_TEST],
        cwd=web_root,
        input=json.dumps(case),
        capture_output=True,
        check=False,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        ({"audit_log_readable": True, "halted_today": True}, "haltActive"),
        ({"audit_log_readable": True, "halted_today": False}, "haltInactive"),
        ({"audit_log_readable": False, "halted_today": False}, "haltUnknown"),
        ({"audit_log_readable": False, "halted_today": True}, "haltUnknown"),
        ({"halted_today": True}, "haltUnknown"),
        ({"audit_log_readable": True}, "haltUnknown"),
        ({"audit_log_readable": True, "halted_today": "false"}, "haltUnknown"),
        (None, "haltUnknown"),
    ],
)
def test_health_states_are_fail_closed(health, expected):
    _run_node({"scenario": "health", "health": health, "expected": expected})


@pytest.mark.parametrize("failure", ["error", "httpError"])
def test_health_failure_keeps_stop_available(failure):
    _run_node({"scenario": "health", failure: True, "expected": "haltUnknown"})


def test_halt_polling_stale_response_and_explicit_reset():
    _run_node({"scenario": "halt"})


@pytest.mark.parametrize("outcome", ["inactive", "failed", "stopDuringRefresh"])
def test_reset_requires_fresh_health_and_stop_wins(outcome):
    _run_node({"scenario": "reset-refresh", outcome: True})


def test_external_reset_and_lost_health_are_reflected():
    _run_node({"scenario": "health-transitions"})


def test_stop_remains_available_while_health_refresh_waits():
    _run_node({"scenario": "health-after-post"})


def test_interval_dirty_polling_and_inflight_save():
    _run_node({"scenario": "dirty"})


def test_interval_draft_becomes_clean_when_server_matches():
    _run_node({"scenario": "draft-matches-server"})


def test_scheduler_failure_does_not_remove_stop_control():
    _run_node({"scenario": "scheduler-error"})


@pytest.mark.parametrize(("enabled", "mode"), [(True, True), (False, True), (False, False)])
def test_scheduler_status_is_independent_from_modes(enabled, mode):
    _run_node({"scenario": "scheduler-state", "enabled": enabled, "mode": mode})


def test_stale_scheduler_can_retry_without_losing_draft_or_posting():
    _run_node({"scenario": "scheduler-stale"})


def test_save_response_does_not_claim_scheduler_is_enabled():
    _run_node({"scenario": "save-inactive"})


@pytest.mark.parametrize("locale", ["en", "vi"])
def test_stop_scope_and_utc_copy(locale):
    _run_node({"scenario": "fallback-copy", "locale": locale})


@pytest.mark.parametrize("locale", ["en", "vi"])
def test_run_now_warns_about_real_orders(locale):
    _run_node({"scenario": "fallback-run-copy", "locale": locale})


def test_run_now_label_and_endpoint():
    _run_node({"scenario": "run-now-copy"})
