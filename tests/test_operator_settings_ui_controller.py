"""Exercise the operator controller with mocked API calls."""

import os
import subprocess

import pytest

from tests.support.node_renderer import require_web_renderer

NODE_TEST = r"""
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const assert = require("node:assert/strict");
const webRoot = process.cwd();
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

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

const types = require("./components/settings/operator-config-types.ts");
const api = require("./components/settings/operator-config-api.ts");
const { useOperatorConfig } = require("./components/settings/operator-config-controller.ts");
const { OperatorModes, OperatorRisk } = require("./components/settings/operator-config-card.tsx");
const locale = require("./lib/i18n/locale.ts");

const rows = [
  { key: "auto_spot_enabled", type: "bool", group: "gates", label: "Auto Spot", hint: "Spot", gate: true, value: false, default: false, override: false },
  { key: "auto_futures_enabled", type: "bool", group: "gates", label: "Auto Futures", hint: "Futures", gate: true, value: false, default: false, override: false },
  { key: "risk_max_open_heat_pct", type: "float", group: "risk", label: "Heat", hint: "Heat hint", gate: false, value: 6, default: 5, override: true },
  { key: "risk_max_derivatives_leverage", type: "float", group: "risk", label: "Leverage", hint: "Leverage hint", gate: false, value: 3, default: 2, override: true },
  { key: "risk_derivatives_break_even_r", type: "float", group: "risk", label: "Break even", hint: "Break even hint", gate: false, value: 1, default: 1, override: false },
  { key: "risk_derivatives_lock_profit_r", type: "float", group: "risk", label: "Lock profit", hint: "Lock hint", gate: false, value: 2, default: 2, override: false },
  { key: "risk_derivatives_trail_start_r", type: "float", group: "risk", label: "Trail start", hint: "Trail hint", gate: false, value: 3, default: 3, override: false },
  { key: "risk_derivatives_trail_distance_r", type: "float", group: "risk", label: "Trail distance", hint: "Distance hint", gate: false, value: 0.5, default: 0.5, override: false },
  { key: "risk_derivatives_time_stop_min_r", type: "float", group: "risk", label: "Time stop min R", hint: "Time stop hint", gate: false, value: 1, default: 1, override: false },
  { key: "risk_derivatives_scale_out_enabled", type: "bool", group: "risk", label: "Scale out", hint: "Scale hint", gate: false, value: true, default: true, override: false },
  { key: "risk_derivatives_override_min_r", type: "float", group: "risk", label: "Override", hint: "Override hint", gate: false, value: 2, default: 2, override: false },
  { key: "future_unknown", type: "float", group: "risk", label: "Unknown", hint: "Unknown hint", gate: false, value: 4, default: 4, override: false },
].map((row) => ({ stored_value: null, updated_at: null, updated_by: null, ...row }));
const payload = {
  generated_at: "2026-11-09T12:00:00Z", settings: rows,
  gates: { auto_spot: false, auto_futures: false, any_auto_enabled: false },
  effective_exec_mode: "dry", allowlist: rows.map((row) => row.key),
};
const clone = (value) => structuredClone(value);
const nextWith = (settings) => ({
  ...clone(payload),
  settings: rows.map((row) => ({ ...row, value: settings[row.key] ?? row.value })),
});

// Exercise the real hook with a small deterministic hook driver, no DOM or network.
const original = {};
let slots = [];
let index = 0;
let effects = [];
let controller;
const changedNotifications = [];
function depsChanged(previous, next) {
  return !previous || !next || previous.length !== next.length ||
    next.some((value, i) => !Object.is(value, previous[i]));
}
function hookDriver() {
  original.useState = React.useState;
  original.useRef = React.useRef;
  original.useMemo = React.useMemo;
  original.useCallback = React.useCallback;
  original.useEffect = React.useEffect;
  original.useSyncExternalStore = React.useSyncExternalStore;
  React.useState = (initial) => {
    const i = index++;
    if (!slots[i]) slots[i] = { value: typeof initial === "function" ? initial() : initial };
    return [slots[i].value, (next) => {
      slots[i].value = typeof next === "function" ? next(slots[i].value) : next;
    }];
  };
  React.useRef = (initial) => {
    const i = index++;
    if (!slots[i]) slots[i] = { current: initial };
    return slots[i];
  };
  React.useMemo = (make, deps) => {
    const i = index++;
    if (!slots[i] || depsChanged(slots[i].deps, deps)) slots[i] = { deps, value: make() };
    return slots[i].value;
  };
  React.useCallback = (callback, deps) => React.useMemo(() => callback, deps);
  React.useEffect = (effect, deps) => {
    const i = index++;
    if (!slots[i] || depsChanged(slots[i].deps, deps)) {
      const previous = slots[i];
      slots[i] = { deps, cleanup: previous?.cleanup };
      effects.push(() => {
        previous?.cleanup?.();
        slots[i].cleanup = effect();
      });
    }
  };
  React.useSyncExternalStore = (_subscribe, snapshot) => snapshot();
}
function render() {
  index = 0;
  effects = [];
  controller = useOperatorConfig({ onDraftChange: notify });
  for (const effect of effects) effect();
  return controller;
}
function notify(state) { changedNotifications.push(clone(state)); }
function restoreReact() {
  for (const slot of slots) slot?.cleanup?.();
  Object.assign(React, original);
}
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function renderChecks(snapshot) {
  restoreReact();
  for (const lang of ["en", "vi"]) {
    locale.getServerLocale = () => lang;
    locale.getLocale = () => lang;
    const modes = renderToStaticMarkup(React.createElement(OperatorModes, { controller: snapshot }));
    const risk = renderToStaticMarkup(React.createElement(OperatorRisk, { controller: snapshot }));
    assert.match(modes, /id="operator-auto_spot_enabled"/);
    assert.doesNotMatch(modes, /id="operator-risk_max_open_heat_pct"/);
    assert.doesNotMatch(risk, /id="operator-auto_spot_enabled"/);
    for (const row of rows.filter((item) => !item.gate)) {
      assert.equal((risk.match(new RegExp(`id="operator-${row.key}"`, "g")) || []).length, 1, row.key);
      assert.match(risk, new RegExp(`for="operator-${row.key}"`));
    }
    assert.match(risk, /aria-invalid="true"/);
    assert.match(risk, /aria-describedby="[^"]*operator-risk_max_open_heat_pct-error[^"]*"/);
    assert.match(risk, /id="operator-risk_max_open_heat_pct-error" role="alert"/);
    assert.match(risk, /disabled=""/);
    assert.match(risk, lang === "en" ? /General risk/ : /Rủi ro chung/);
  }
}

(async () => {
  let getCount = 0;
  let updates = [];
  let resets = [];
  api.operatorConfigApi.get = async () => { getCount++; return clone(payload); };
  api.operatorConfigApi.update = async (settings, options) => {
    updates.push({ settings, options });
    return nextWith(settings);
  };
  api.operatorConfigApi.reset = async (key) => {
    resets.push(key);
    return nextWith({ [key]: 5 });
  };
  hookDriver();
  render();
  await controller.reload();
  render();
  assert.equal(getCount, 1);
  assert.equal(controller.phase, "ready");
  assert.equal(controller.dirtyCount, 0);
  assert.deepEqual(changedNotifications.at(-1).changed, []);

  for (const bad of ["", " ", "-1", "Infinity", "NaN", "1e999", "0xzz"]) {
    controller.setValue("risk_max_open_heat_pct", bad);
    render();
    assert.ok(controller.invalidKeys.includes("risk_max_open_heat_pct"), bad);
    await controller.save("risk");
    assert.equal(updates.length, 0);
  }
  controller.setValue("risk_derivatives_time_stop_min_r", "0");
  render();
  assert.equal(controller.invalidMessages.risk_derivatives_time_stop_min_r, "Enter a finite number greater than 0.");
  await controller.save("risk");
  assert.equal(updates.length, 0);
  controller.setValue("risk_derivatives_time_stop_min_r", "1");
  render();
  controller.setValue("risk_derivatives_break_even_r", "0");
  render();
  assert.equal(controller.invalidMessages.risk_derivatives_break_even_r, "Enter a finite number greater than 0.");
  controller.setValue("risk_derivatives_break_even_r", "2");
  render();
  assert.equal(controller.invalidMessages.risk_derivatives_break_even_r, "Required order: break-even < profit lock < trailing start.");
  assert.equal(controller.invalidMessages.risk_derivatives_lock_profit_r, undefined);
  assert.equal(controller.invalidKeys.includes("risk_derivatives_break_even_r"), true);
  controller.setValue("risk_max_open_heat_pct", "6");
  controller.setValue("risk_derivatives_break_even_r", "1");
  render();
  assert.equal(controller.invalidKeys.some((key) => key.startsWith("risk_derivatives_")), false);
  controller.setValue("risk_derivatives_break_even_r", "2");
  render();
  assert.equal(controller.invalidMessages.risk_derivatives_break_even_r, "Required order: break-even < profit lock < trailing start.");
  controller.setValue("risk_derivatives_break_even_r", "1");
  render();
  controller.setValue("risk_derivatives_lock_profit_r", "3");
  render();
  assert.equal(controller.invalidMessages.risk_derivatives_lock_profit_r, "Required order: break-even < profit lock < trailing start.");
  assert.equal(controller.invalidMessages.risk_derivatives_trail_start_r, undefined);
  controller.setValue("risk_derivatives_lock_profit_r", "2");
  render();
  assert.deepEqual(controller.invalidKeys, []);
  controller.undo("risk");
  render();
  for (const key of ["risk_derivatives_break_even_r", "risk_derivatives_lock_profit_r", "risk_derivatives_trail_start_r", "risk_derivatives_trail_distance_r", "risk_derivatives_time_stop_min_r"]) {
    controller.setValue(key, "0");
    render();
    assert.ok(controller.invalidKeys.includes(key), key);
  }
  controller.undo("risk");
  render();
  for (const [text, expected] of [["0", 0], [" 2.5 ", 2.5], ["2,75", 2.75], ["1e2", 100]]) {
    assert.equal(types.parseOperatorNumber(text), expected);
  }

  controller.setValue("risk_max_open_heat_pct", "2,75");
  render();
  controller.setValue("auto_spot_enabled", true);
  render();
  assert.equal(controller.dirtyCount, 2);
  assert.deepEqual(changedNotifications.at(-1).changed.sort(), ["auto_spot_enabled", "risk_max_open_heat_pct"]);
  controller.setConfirmation(true);
  controller.setGateReason("demo");
  render();
  assert.equal(controller.confirmGate, true);
  controller.setValue("auto_futures_enabled", true);
  render();
  assert.equal(controller.confirmGate, false);
  assert.equal(controller.reason, "");
  controller.setConfirmation(true);
  controller.setGateReason("old reason");
  render();
  controller.setValue("auto_futures_enabled", false);
  render();
  assert.equal(controller.confirmGate, false);
  assert.equal(controller.reason, "");
  await controller.save("modes");
  assert.equal(updates.length, 0);
  await controller.reset("risk_max_derivatives_leverage");
  assert.deepEqual(resets, []);

  // Saving risk neither sends nor discards the staged gate; no edit can race it.
  controller.setConfirmation(true);
  controller.setGateReason("gate reason");
  render();
  const waiting = deferred();
  api.operatorConfigApi.update = (settings, options) => {
    updates.push({ settings, options });
    return waiting.promise;
  };
  const saveRisk = controller.save("risk");
  render();
  assert.equal(controller.saving, true);
  controller.setValue("risk_max_derivatives_leverage", "10");
  controller.setValue("auto_spot_enabled", false);
  controller.undo("modes");
  controller.setConfirmation(true);
  controller.setGateReason("late reason");
  await controller.reset("risk_max_derivatives_leverage");
  render();
  assert.equal(controller.draft.risk_max_derivatives_leverage, "3");
  assert.equal(controller.draft.auto_spot_enabled, true);
  assert.equal(controller.confirmGate, true);
  assert.equal(controller.reason, "gate reason");
  assert.deepEqual(updates[0].settings, { risk_max_open_heat_pct: 2.75 });
  assert.equal(typeof updates[0].settings.risk_max_open_heat_pct, "number");
  waiting.resolve(nextWith(updates[0].settings));
  await saveRisk;
  render();
  assert.deepEqual(controller.changed, ["auto_spot_enabled"]);
  assert.equal(controller.draft.auto_spot_enabled, true);
  assert.equal(controller.draft.risk_max_open_heat_pct, "2.75");
  assert.equal(controller.confirmGate, true);
  assert.equal(controller.reason, "gate reason");

  controller.setConfirmation(true);
  controller.setGateReason("manual test");
  render();
  const autoWaiting = deferred();
  api.operatorConfigApi.update = (settings, options) => {
    updates.push({ settings, options });
    return autoWaiting.promise;
  };
  const saveModes = controller.save("modes");
  render();
  assert.deepEqual(updates[1], { settings: { auto_spot_enabled: true }, options: { confirm: true, reason: "manual test" } });
  autoWaiting.resolve({ ...nextWith({ auto_spot_enabled: true, risk_max_open_heat_pct: 2.75 }) });
  await saveModes;
  render();
  assert.equal(controller.dirtyCount, 0);
  assert.equal(controller.confirmGate, false);
  assert.equal(controller.reason, "");

  // Undo in either pane invalidates an existing gate confirmation.
  controller.setValue("risk_max_open_heat_pct", "3");
  render();
  controller.setValue("auto_futures_enabled", true);
  render();
  controller.setConfirmation(true);
  controller.setGateReason("undo risk");
  render();
  controller.undo("risk");
  render();
  assert.equal(controller.confirmGate, false);
  assert.equal(controller.reason, "");
  assert.equal(controller.draft.auto_futures_enabled, true);
  controller.undo("modes");
  render();

  controller.setValue("auto_futures_enabled", true);
  render();
  controller.setConfirmation(true);
  controller.setGateReason("undo me");
  render();
  controller.undo("modes");
  render();
  assert.equal(controller.confirmGate, false);
  assert.equal(controller.reason, "");
  assert.equal(controller.dirtyCount, 0);
  await controller.reset("risk_max_derivatives_leverage");
  render();
  assert.deepEqual(resets, ["risk_max_derivatives_leverage"]);
  assert.equal(controller.draft.risk_max_derivatives_leverage, "5");

  // Save modes while another pane has a dirty numeric value.
  controller.setValue("risk_max_open_heat_pct", "8");
  render();
  controller.setValue("auto_futures_enabled", true);
  render();
  controller.setConfirmation(true);
  controller.setGateReason("second mode");
  render();
  api.operatorConfigApi.update = async (settings) => nextWith({ ...settings, auto_spot_enabled: true });
  await controller.save("modes");
  render();
  assert.equal(controller.draft.risk_max_open_heat_pct, "8");
  assert.deepEqual(controller.sectionChanges.risk, ["risk_max_open_heat_pct"]);

  // The mode response also changed another gate; review it before a risk write.
  assert.equal(controller.serverAhead, true);
  assert.equal(controller.draft.risk_max_open_heat_pct, "8");
  controller.reviewLatest();
  render();
  assert.equal(controller.serverAhead, false);
  assert.equal(controller.draft.risk_max_open_heat_pct, "8");

  // Failures leave the user's draft and error feedback intact.
  controller.setValue("risk_max_open_heat_pct", "9");
  render();
  api.operatorConfigApi.update = async () => { throw new api.OperatorConfigApiError(422, "coercion_error", "rejected"); };
  await controller.save("risk");
  render();
  assert.equal(controller.draft.risk_max_open_heat_pct, "9");
  assert.deepEqual(controller.feedback.risk, { role: "alert", text: "rejected" });
  controller.undo("risk");
  render();

  // Set a clean, false gate baseline before reproducing the queued-refresh race.
  controller.setValue("auto_spot_enabled", false);
  render();
  controller.setValue("auto_futures_enabled", false);
  render();
  controller.setConfirmation(true);
  controller.setGateReason("reset baseline");
  render();
  api.operatorConfigApi.update = async (settings) => nextWith(settings);
  await controller.save("modes");
  render();
  assert.equal(controller.draft.auto_spot_enabled, false);
  assert.equal(controller.draft.auto_futures_enabled, false);

  controller.setValue("auto_spot_enabled", true);
  render();
  controller.setValue("risk_max_open_heat_pct", "11");
  render();
  controller.setValue("risk_max_derivatives_leverage", "4");
  render();
  controller.setConfirmation(true);
  controller.setGateReason("queued sequence");
  render();
  const sequenceWrite = deferred();
  const sequencePuts = [];
  api.operatorConfigApi.update = (settings, options) => {
    sequencePuts.push({ settings, options });
    return sequenceWrite.promise;
  };
  const sequenceSave = controller.save("risk");
  render();
  assert.equal(controller.saving, true);
  const sequenceRemote = nextWith({ auto_futures_enabled: true, risk_max_open_heat_pct: 11, risk_max_derivatives_leverage: 4 });
  api.operatorConfigApi.get = async () => clone(sequenceRemote);
  await controller.reload();
  sequenceWrite.resolve(sequenceRemote);
  await sequenceSave;
  await new Promise((resolve) => setImmediate(resolve));
  render();
  assert.deepEqual(sequencePuts, [{
    settings: { risk_max_open_heat_pct: 11, risk_max_derivatives_leverage: 4 },
    options: { confirm: undefined, reason: undefined },
  }]);
  assert.equal(controller.serverAhead, true);
  assert.equal(controller.draft.auto_spot_enabled, true);
  assert.equal(controller.confirmGate, false);
  assert.equal(controller.reason, "");
  const sequenceWritesBefore = sequencePuts.length;
  await controller.save("modes");
  assert.equal(sequencePuts.length, sequenceWritesBefore);
  controller.reviewLatest();
  render();
  assert.equal(controller.serverAhead, false);
  assert.equal(controller.confirmGate, false);
  assert.equal(controller.reason, "");
  controller.setConfirmation(true);
  controller.setGateReason("reconfirmed after review");
  render();
  api.operatorConfigApi.update = (settings, options) => {
    sequencePuts.push({ settings, options });
    return Promise.resolve(nextWith({ ...settings, auto_futures_enabled: true, risk_max_open_heat_pct: 11, risk_max_derivatives_leverage: 4 }));
  };
  await controller.save("modes");
  assert.deepEqual(sequencePuts.at(-1), {
    settings: { auto_spot_enabled: true },
    options: { confirm: true, reason: "reconfirmed after review" },
  });
  controller.undo("modes");
  controller.undo("risk");
  render();

  // Refresh never overwrites dirty drafts; stale snapshots block all writes.
  controller.setValue("auto_futures_enabled", false);
  render();
  controller.setConfirmation(true);
  controller.setGateReason("refresh test");
  render();
  const remote = nextWith({ auto_spot_enabled: true, auto_futures_enabled: true, risk_max_open_heat_pct: 12 });
  api.operatorConfigApi.get = async () => clone(remote);
  const staleActions = controller;
  const updatesBeforeRefresh = updates.length;
  await controller.reload();
  await staleActions.save("modes");
  await staleActions.reset("risk_max_derivatives_leverage");
  assert.equal(updates.length, updatesBeforeRefresh);
  assert.deepEqual(resets, ["risk_max_derivatives_leverage"]);
  render();
  assert.equal(controller.serverAhead, true);
  assert.equal(controller.draft.auto_futures_enabled, false);
  assert.equal(controller.payload.settings.find((row) => row.key === "risk_max_open_heat_pct").value, 11);
  const beforeRefreshSave = updates.length;
  await controller.save("modes");
  await controller.reset("risk_max_derivatives_leverage");
  assert.equal(updates.length, beforeRefreshSave);
  assert.deepEqual(resets, ["risk_max_derivatives_leverage"]);
  controller.reviewLatest();
  render();
  assert.equal(controller.serverAhead, false);
  assert.equal(controller.draft.auto_futures_enabled, false);
  assert.equal(controller.draft.risk_max_open_heat_pct, "12");
  assert.equal(controller.confirmGate, false);
  assert.equal(controller.reason, "");

  // A failed refresh is visibly stale and cannot be acknowledged with an older snapshot.
  const newer = nextWith({ auto_spot_enabled: true, auto_futures_enabled: true, risk_max_open_heat_pct: 14 });
  api.operatorConfigApi.get = async () => clone(newer);
  await controller.reload();
  render();
  assert.equal(controller.serverAhead, true);
  api.operatorConfigApi.get = async () => { throw new Error("offline"); };
  await controller.reload();
  render();
  assert.equal(controller.refreshFailed, true);
  controller.reviewLatest();
  render();
  assert.equal(controller.serverAhead, true);
  assert.equal(controller.draft.risk_max_open_heat_pct, "12");
  api.operatorConfigApi.get = async () => clone(newer);
  await controller.reload();
  render();
  controller.undo("modes");
  render();
  assert.equal(controller.serverAhead, false);
  assert.equal(controller.draft.risk_max_open_heat_pct, "14");
  assert.equal(controller.refreshFailed, false);

  // A refresh already in flight prevents writes and keeps edits made while it waits.
  const pendingGet = deferred();
  api.operatorConfigApi.get = () => pendingGet.promise;
  const refresh = controller.reload();
  render();
  assert.equal(controller.refreshing, true);
  controller.setValue("risk_max_open_heat_pct", "20");
  render();
  await controller.save("risk");
  assert.equal(updates.length, beforeRefreshSave);
  pendingGet.resolve(nextWith({ auto_spot_enabled: true, risk_max_open_heat_pct: 15 }));
  await refresh;
  render();
  assert.equal(controller.draft.risk_max_open_heat_pct, "20");
  assert.equal(controller.serverAhead, true);
  controller.reviewLatest();
  render();
  assert.equal(controller.draft.risk_max_open_heat_pct, "20");
  assert.equal(controller.payload.settings.find((row) => row.key === "risk_max_open_heat_pct").value, 15);

  // An invalidation arriving during a write is refetched after the write settles.
  controller.undo("risk");
  render();
  controller.setValue("risk_max_open_heat_pct", "21");
  render();
  const delayedWrite = deferred();
  let refreshesAfterWrite = 0;
  api.operatorConfigApi.update = () => delayedWrite.promise;
  api.operatorConfigApi.get = async () => {
    refreshesAfterWrite++;
    return nextWith({ auto_spot_enabled: true, auto_futures_enabled: true, risk_max_open_heat_pct: 21 });
  };
  const write = controller.save("risk");
  render();
  await controller.reload();
  assert.equal(refreshesAfterWrite, 0);
  delayedWrite.resolve(nextWith({ auto_spot_enabled: true, risk_max_open_heat_pct: 21 }));
  await write;
  await new Promise((resolve) => setImmediate(resolve));
  render();
  assert.ok(refreshesAfterWrite >= 1);
  assert.equal(controller.payload.settings.find((row) => row.key === "auto_futures_enabled").value, true);
  assert.equal(controller.dirtyCount, 0);

  // Render both panes with the same controller; each row occurs only once.
  controller.setValue("risk_max_open_heat_pct", "-1");
  render();
  await renderChecks(controller);
  process.stdout.write("operator controller and section checks passed\n");
})().catch((error) => {
  for (const slot of slots) slot?.cleanup?.();
  console.error(error);
  process.exitCode = 1;
});
"""


@pytest.mark.unit
def test_operator_controller_and_section_rendering():
    web_root = require_web_renderer()
    env = os.environ.copy()
    env["TZ"] = "UTC"
    result = subprocess.run(
        ["node", "-e", NODE_TEST],
        cwd=web_root,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "operator controller and section checks passed" in result.stdout
