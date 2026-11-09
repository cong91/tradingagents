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

const { useSettings } = require(path.join(webRoot, "components/settings/use-settings.ts"));
const { SaveBar } = require(path.join(webRoot, "components/settings/save-bar.tsx"));

function clone(value) {
  return structuredClone(value);
}

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

function row(key, value, type = "str") {
  return {
    key,
    env_var: `TEST_${key.toUpperCase()}`,
    value,
    default: value,
    type,
    editable: true,
    source: "default",
  };
}

function payload(values, generatedAt = "2026-11-09T12:00:00Z") {
  return {
    generated_at: generatedAt,
    settings: [
      row("clean_key", values.clean_key),
      row("sent_key", values.sent_key),
      row("concurrent_key", values.concurrent_key),
      row("other_key", values.other_key),
    ],
    credentials: [],
    gates_readonly: {},
    effective_exec_mode: "dry",
  };
}

function snapshot(controller) {
  return clone({
    formValues: controller.formValues,
    dirtyKeys: controller.dirtyKeys,
    saveState: controller.saveState,
    serverAhead: controller.serverAhead,
    conflictValues: controller.conflictValues,
    reviewing: controller.reviewing,
  });
}

async function runThreeWay() {
  let server = payload({ clean_key: "clean-1", sent_key: "sent-1", concurrent_key: "concurrent-1", other_key: "other-1" });
  const requests = [];
  let releasePut;
  let putStartedResolve;
  const putStarted = new Promise((resolve) => { putStartedResolve = resolve; });
  globalThis.fetch = async (_url, options = {}) => {
    if (options.method === "PUT") {
      const values = JSON.parse(options.body).settings;
      requests.push({ method: "PUT", values: clone(values) });
      if (requests.filter((request) => request.method === "PUT").length > 1) {
        return { ok: true, json: async () => clone(server) };
      }
      putStartedResolve();
      await new Promise((resolve) => { releasePut = resolve; });
      server = payload({ clean_key: "clean-2", sent_key: values.sent_key, concurrent_key: "remote-2", other_key: "other-2" }, "2026-11-09T12:01:00Z");
      server.applied_at = "2026-11-09T12:01:00Z";
      return { ok: true, json: async () => clone(server) };
    }
    requests.push({ method: "GET" });
    return { ok: true, json: async () => clone(server) };
  };

  const driver = createHookDriver(useSettings);
  try {
    let controller = driver.render();
    await controller.reload();
    controller = driver.render();
    controller.setValue("sent_key", "sent-2");
    controller.setValue("concurrent_key", "draft-1");
    controller = driver.render();
    const pending = controller.save(["sent_key"], "ai");
    await putStarted;
    controller = driver.render();
    controller.setValue("sent_key", "sent-3");
    releasePut();
    await pending;
    controller = driver.render();
    const afterSave = snapshot(controller);
    await controller.reload();
    controller = driver.render();
    const afterPoll = snapshot(controller);
    const blockedRequestCount = requests.length;
    await controller.save(["sent_key"], "ai");
    controller = driver.render();
    const afterBlockedRetry = snapshot(controller);
    await controller.reviewLatest();
    controller = driver.render();
    const afterReview = snapshot(controller);
    return { afterSave, afterPoll, afterBlockedRetry, afterReview, blockedRequestCount, requests };
  } finally {
    driver.restore();
  }
}

async function runUncertain() {
  let server = payload({ clean_key: "clean-1", sent_key: "sent-1", concurrent_key: "concurrent-1", other_key: "other-1" });
  const requests = [];
  let putAttempts = 0;
  globalThis.fetch = async (_url, options = {}) => {
    if (options.method === "PUT") {
      putAttempts += 1;
      requests.push({ method: "PUT", values: clone(JSON.parse(options.body).settings) });
      if (putAttempts === 1) throw new Error("connection reset after write");
      server = payload({ clean_key: "clean-1", sent_key: "sent-2", concurrent_key: "concurrent-1", other_key: "other-1" }, "2026-11-09T12:02:00Z");
      server.applied_at = "2026-11-09T12:02:00Z";
      return { ok: true, json: async () => clone(server) };
    }
    requests.push({ method: "GET" });
    return { ok: true, json: async () => clone(server) };
  };

  const driver = createHookDriver(useSettings);
  try {
    let controller = driver.render();
    await controller.reload();
    controller = driver.render();
    controller.setValue("sent_key", "sent-2");
    controller.setValue("other_key", "draft-other");
    controller = driver.render();
    await controller.save(["sent_key"], "ai");
    controller = driver.render();
    const uncertain = snapshot(controller);
    await controller.reload();
    controller = driver.render();
    const afterPoll = snapshot(controller);
    await controller.save(["sent_key"], "ai");
    controller = driver.render();
    const blockedRetry = snapshot(controller);
    await controller.reviewLatest();
    controller = driver.render();
    const afterReview = snapshot(controller);
    await controller.save(["sent_key"], "ai");
    controller = driver.render();
    const afterRetry = snapshot(controller);
    return { uncertain, afterPoll, blockedRetry, afterReview, afterRetry, requests };
  } finally {
    driver.restore();
  }
}

async function runReviewFailure() {
  const server = payload({ clean_key: "clean-1", sent_key: "sent-1", concurrent_key: "concurrent-1", other_key: "other-1" });
  const requests = [];
  let failNextGet = false;
  globalThis.fetch = async (_url, options = {}) => {
    requests.push(options.method === "PUT" ? "PUT" : "GET");
    if (failNextGet) {
      failNextGet = false;
      throw new Error("offline");
    }
    return { ok: true, json: async () => clone(server) };
  };

  const driver = createHookDriver(useSettings);
  try {
    let controller = driver.render();
    await controller.reload();
    controller = driver.render();
    controller.setValue("sent_key", "draft");
    failNextGet = true;
    await controller.reviewLatest();
    controller = driver.render();
    const failedReview = snapshot(controller);
    controller.resetChanges();
    await controller.reload();
    controller = driver.render();
    controller.setValue("sent_key", "new-draft");
    controller = driver.render();
    await controller.save(["sent_key"], "ai");
    controller = driver.render();
    const afterPollAndBlockedSave = snapshot(controller);
    await controller.reviewLatest();
    controller = driver.render();
    const afterSuccessfulReview = snapshot(controller);
    return { failedReview, afterPollAndBlockedSave, afterSuccessfulReview, requests };
  } finally {
    driver.restore();
  }
}

async function runReviewRace() {
  let server = payload({ clean_key: "clean-1", sent_key: "sent-1", concurrent_key: "concurrent-1", other_key: "other-1" });
  const requests = [];
  let releaseOldPoll;
  let releaseReview;
  globalThis.fetch = async (_url, options = {}) => {
    requests.push(options.method === "PUT" ? "PUT" : "GET");
    if (requests.length === 2) {
      const old = clone(server);
      await new Promise((resolve) => { releaseOldPoll = resolve; });
      return { ok: true, json: async () => old };
    }
    if (requests.length === 3) {
      await new Promise((resolve) => { releaseReview = resolve; });
    }
    return { ok: true, json: async () => clone(server) };
  };

  const driver = createHookDriver(useSettings);
  try {
    let controller = driver.render();
    await controller.reload();
    controller = driver.render();
    controller.setValue("sent_key", "draft");
    const oldPoll = controller.reload();
    server = payload({ clean_key: "clean-2", sent_key: "server-2", concurrent_key: "concurrent-1", other_key: "other-1" });
    const pendingReview = controller.reviewLatest();
    controller = driver.render();
    const duringReview = snapshot(controller);
    await controller.reload();
    await controller.save(["sent_key"], "ai");
    releaseReview();
    await pendingReview;
    controller = driver.render();
    const afterReview = snapshot(controller);
    releaseOldPoll();
    await oldPoll;
    controller = driver.render();
    const afterOldPoll = snapshot(controller);
    return { duringReview, afterReview, afterOldPoll, requests };
  } finally {
    driver.restore();
  }
}

async function runDiscard() {
  let server = payload({ clean_key: "clean-1", sent_key: "sent-1", concurrent_key: "concurrent-1", other_key: "other-1" });
  let failGet = false;
  const requests = [];
  globalThis.fetch = async (_url, options = {}) => {
    requests.push(options.method === "PUT" ? "PUT" : "GET");
    if (failGet) {
      failGet = false;
      throw new Error("offline");
    }
    return { ok: true, json: async () => clone(server) };
  };
  const driver = createHookDriver(useSettings);
  try {
    let controller = driver.render();
    await controller.reload();
    controller = driver.render();
    controller.setValue("sent_key", "draft");
    server = payload({ clean_key: "clean-2", sent_key: "server-2", concurrent_key: "concurrent-1", other_key: "other-1" });
    await controller.reload();
    controller = driver.render();
    const before = snapshot(controller);
    failGet = true;
    const failed = await controller.discardAndReload();
    controller = driver.render();
    const afterFailure = snapshot(controller);
    await controller.save(["sent_key"], "ai");
    controller = driver.render();
    const afterBlockedSave = snapshot(controller);
    const succeeded = await controller.discardAndReload();
    controller = driver.render();
    const afterSuccess = snapshot(controller);
    return { before, failed, afterFailure, afterBlockedSave, succeeded, afterSuccess, requests };
  } finally {
    driver.restore();
  }
}

function renderSaveBars() {
  const controller = {
    dirtyKeys: ["sent_key"],
    saveState: { kind: "uncertain", scope: "ai" },
    serverAhead: true,
    reviewing: false,
    resetChanges() {},
    save() {},
    reviewLatest() {},
  };
  const conflict = renderToStaticMarkup(React.createElement(SaveBar, { controller, keys: ["sent_key"], scope: "ai" }));
  const uncertain = renderToStaticMarkup(React.createElement(SaveBar, {
    controller: { ...controller, serverAhead: false }, keys: ["sent_key"], scope: "ai",
  }));
  return { conflict, uncertain };
}

(async () => {
  const result = {
    threeWay: await runThreeWay(),
    uncertain: await runUncertain(),
    reviewFailure: await runReviewFailure(),
    reviewRace: await runReviewRace(),
    discard: await runDiscard(),
    bars: renderSaveBars(),
  };
  process.stdout.write(JSON.stringify(result));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
