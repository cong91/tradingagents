"use strict";

const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");

const webRoot = path.resolve(__dirname, "../web");
const webRequire = Module.createRequire(path.join(webRoot, "package.json"));
const ts = webRequire("typescript");
const React = webRequire("react");
const { renderToStaticMarkup } = webRequire("react-dom/server");

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
  return resolveFilename.call(
    this,
    request.startsWith("@/") ? path.join(webRoot, request.slice(2)) : request,
    parent,
    isMain,
    options,
  );
};

globalThis.fetch = () => { throw new Error("Unexpected network request"); };
const layout = require(path.join(webRoot, "components/settings/settings-layout.ts"));
const { SettingsNavigation } = require(path.join(webRoot, "components/settings/settings-navigation.tsx"));
const { SaveBar } = require(path.join(webRoot, "components/settings/save-bar.tsx"));
const { useSettings } = require(path.join(webRoot, "components/settings/use-settings.ts"));
const locale = require(path.join(webRoot, "lib/i18n/locale.ts"));
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const clone = (value) => structuredClone(value);

function collectClickTargets(element, targets = []) {
  if (!React.isValidElement(element)) return targets;
  if (typeof element.props.onClick === "function") targets.push(element);
  React.Children.forEach(element.props.children, (child) => collectClickTargets(child, targets));
  return targets;
}

function renderComponents() {
  const output = {};
  for (const language of ["vi", "en"]) {
    locale.getServerLocale = () => language;
    locale.getLocale = () => language;
    const navigation = {};
    for (const selected of input.selectedAreas) {
      const events = [];
      const hashes = [];
      const modifiedClicks = [];
      const props = { selected, onSelect: (area) => events.push(area) };
      const markup = renderToStaticMarkup(React.createElement(SettingsNavigation, props));
      const original = React.useSyncExternalStore;
      const oldWindow = globalThis.window;
      const oldHistory = globalThis.history;
      globalThis.window = { location: { hash: "#initial" } };
      globalThis.history = { pushState: (_state, _title, hash) => {
        hashes.push(hash);
        window.location.hash = hash;
      } };
      try {
        React.useSyncExternalStore = (_subscribe, snapshot) => snapshot();
        const targets = collectClickTargets(SettingsNavigation(props));
        for (const target of targets) {
          target.props.onClick({ button: 0, preventDefault() {} });
        }
        for (const modifiers of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }]) {
          let prevented = false;
          const previousEvents = events.length;
          const previousHashes = hashes.length;
          targets[1].props.onClick({
            button: 0, ...modifiers, preventDefault() { prevented = true; },
          });
          modifiedClicks.push({
            modifiers, prevented,
            events: events.slice(previousEvents), hashes: hashes.slice(previousHashes),
          });
        }
      } finally {
        React.useSyncExternalStore = original;
        if (oldWindow === undefined) delete globalThis.window;
        else globalThis.window = oldWindow;
        if (oldHistory === undefined) delete globalThis.history;
        else globalThis.history = oldHistory;
      }
      navigation[selected] = { markup, events: events.slice(0, input.selectedAreas.length), hashes: hashes.slice(0, input.selectedAreas.length), modifiedClicks };
    }

    const bars = {};
    for (const [name, scenario] of Object.entries(input.bars)) {
      const calls = [];
      const controller = {
        dirtyKeys: scenario.dirtyKeys,
        saveState: scenario.saveState,
        resetChanges: (keys) => calls.push({ action: "reset", keys: keys ?? null }),
        save: (keys, scope) => calls.push({ action: "save", keys: keys ?? null, scope }),
      };
      const props = { controller, ...scenario.props };
      const markup = renderToStaticMarkup(React.createElement(SaveBar, props));
      const original = React.useSyncExternalStore;
      try {
        React.useSyncExternalStore = (_subscribe, snapshot) => snapshot();
        for (const target of collectClickTargets(SaveBar(props))) {
          if (!target.props.disabled) target.props.onClick();
        }
      } finally {
        React.useSyncExternalStore = original;
      }
      bars[name] = { markup, calls };
    }
    output[language] = { navigation, bars };
  }
  return output;
}

function renderScreens() {
  const settingsModule = require(path.join(webRoot, "components/settings/use-settings.ts"));
  const operatorModule = require(path.join(webRoot, "components/settings/operator-config-controller.ts"));
  const originals = { settings: settingsModule.useSettings, operator: operatorModule.useOperatorConfig };
  let settingsController;
  let operatorController;
  settingsModule.useSettings = () => settingsController;
  operatorModule.useOperatorConfig = () => operatorController;
  try {
    const { SettingsScreen } = require(path.join(webRoot, "components/settings/settings-screen.tsx"));
    const output = {};
    for (const language of ["vi", "en"]) {
      locale.getServerLocale = () => language;
      locale.getLocale = () => language;
      output[language] = {};
      for (const phase of ["ready", "loading", "error", "empty_allowlist", "missing_key"]) {
        const payload = clone(input.screenPayload);
        const operatorPayload = clone(input.operatorPayload);
        if (phase === "empty_allowlist") operatorPayload.allowlist = [];
        if (phase === "missing_key") {
          operatorPayload.settings.push({ ...operatorPayload.settings[0], key: "risk_future_limit" });
        }
        settingsController = {
          phase: "ready", payload,
          formValues: Object.fromEntries(payload.settings.map((row) => [row.key, String(row.value)])),
          fieldErrors: {}, dirtyCount: 0, dirtyKeys: [], saveState: { kind: "idle" },
          isStale: false, serverAhead: false,
          setValue() {}, resetChanges() {}, save() {}, reload() {},
        };
        operatorController = {
          phase: phase === "loading" || phase === "error" ? phase : "ready",
          errorMessage: "offline", payload: phase === "loading" || phase === "error" ? null : operatorPayload,
          draft: { risk_max_daily_loss_pct: "3.5" }, changed: [], dirtyCount: 0,
          invalidKeys: [], sectionChanges: { modes: [], risk: [] }, feedback: {},
          saving: false, refreshing: false, refreshFailed: false, serverAhead: false,
          confirmGate: false, reason: "",
          setValue() {}, setConfirmation() {}, setGateReason() {}, save() {}, undo() {},
          reset() {}, reviewLatest() {}, reload() {},
        };
        output[language][phase] = renderToStaticMarkup(React.createElement(SettingsScreen));
      }
    }
    return output;
  } finally {
    settingsModule.useSettings = originals.settings;
    operatorModule.useOperatorConfig = originals.operator;
  }
}

// Drive the real hook without timers or a DOM. Only the API is substituted.
function createHookDriver(hook) {
  const originals = {};
  const slots = [];
  let index = 0;
  const changed = (oldDeps, newDeps) => !oldDeps || !newDeps ||
    oldDeps.length !== newDeps.length || newDeps.some((value, i) => !Object.is(value, oldDeps[i]));
  for (const name of ["useState", "useRef", "useMemo", "useCallback", "useEffect"]) {
    originals[name] = React[name];
  }
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
    if (!slots[i] || changed(slots[i].deps, deps)) slots[i] = { deps, value: make() };
    return slots[i].value;
  };
  React.useCallback = (callback, deps) => React.useMemo(() => callback, deps);
  React.useEffect = () => { index++; };
  return {
    render() { index = 0; return hook(); },
    restore() { Object.assign(React, originals); },
  };
}

async function exerciseSettingsHook() {
  const payload = clone(input.payload);
  const requests = [];
  let releaseFirstSave;
  let firstSaveWaiting = false;
  globalThis.fetch = async (url, options = {}) => {
    if (url !== "/api/settings") throw new Error(`Unexpected request: ${url}`);
    if (options.method === "PUT") {
      const values = JSON.parse(options.body).settings;
      requests.push({ method: options.method, url, headers: options.headers, values: clone(values) });
      if (!firstSaveWaiting) {
        firstSaveWaiting = true;
        await new Promise((resolve) => { releaseFirstSave = resolve; });
      }
      for (const row of payload.settings) {
        if (Object.hasOwn(values, row.key)) row.value = values[row.key];
      }
      payload.applied_at = "2026-11-09T12:01:00Z";
    } else if (options.method !== undefined || options.cache !== "no-store") {
      throw new Error("Unexpected settings request options");
    }
    return { ok: true, json: async () => clone(payload) };
  };

  const driver = createHookDriver(useSettings);
  const snapshot = (controller) => clone({
    phase: controller.phase,
    formValues: controller.formValues,
    dirtyKeys: controller.dirtyKeys,
    fieldErrors: controller.fieldErrors,
    saveState: controller.saveState,
  });
  try {
    let controller = driver.render();
    await controller.reload();
    controller = driver.render();
    const loaded = snapshot(controller);

    controller.setValue("llm_provider", "provider-b");
    controller.setValue("risk_max_daily_loss_pct", "8.5");
    controller.setValue("checkpoint_enabled", true);
    controller = driver.render();
    const pending = controller.save(["llm_provider", "max_debate_rounds"], "ai");
    controller = driver.render();
    const saving = snapshot(controller);
    controller.setValue("llm_provider", "provider-c");
    controller.setValue("risk_max_daily_loss_pct", "9.5");
    releaseFirstSave();
    await pending;
    controller = driver.render();
    const afterFirstSave = snapshot(controller);

    controller.resetChanges(["llm_provider"]);
    controller = driver.render();
    const afterReset = snapshot(controller);

    controller.setValue("max_debate_rounds", "not-a-number");
    controller = driver.render();
    await controller.save(["risk_max_daily_loss_pct"], "risk");
    controller = driver.render();
    const afterRiskSave = snapshot(controller);

    await controller.save(["max_debate_rounds"], "ai");
    controller = driver.render();
    const invalidAi = snapshot(controller);
    controller.resetChanges(["max_debate_rounds"]);
    controller = driver.render();
    const afterErrorReset = snapshot(controller);
    return { loaded, saving, afterFirstSave, afterReset, afterRiskSave, invalidAi, afterErrorReset, requests };
  } finally {
    driver.restore();
    globalThis.fetch = () => { throw new Error("Unexpected network request"); };
  }
}

(async () => {
  const visible = Object.fromEntries(Object.entries(input.allowlists).map(([name, allowlist]) => [
    name, layout.visibleSettingsRows(input.rows, allowlist),
  ]));
  const result = {
    areas: input.settingKeys.map((key) => [key, layout.areaForSetting(key)]),
    hashes: input.hashes.map((hash) => [hash, layout.areaForHash(hash)]),
    visible,
    ownership: Object.fromEntries(Object.entries(input.ownershipCases).map(([name, item]) => [
      name, layout.operatorOwnershipConsistent(item.keys.map((key) => ({ key })), item.allowlist),
    ])),
    filteredOwnership: Object.fromEntries(Object.entries(input.filterCases).map(([name, item]) => [
      name, layout.visibleSettingsRows(input.rows, item.allowlist, item.operatorKeys),
    ])),
    rendered: renderComponents(),
    screens: renderScreens(),
    hook: await exerciseSettingsHook(),
  };
  process.stdout.write(JSON.stringify(result));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
