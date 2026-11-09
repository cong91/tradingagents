"""Exercise LLM settings interactions with React hooks and mocked fetch, offline."""

import os
import subprocess

import pytest

from tests.support.node_renderer import require_web_renderer

NODE_TEST = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const root = process.cwd();
const ts = require(require.resolve("typescript", {paths: [root]}));
const React = require(require.resolve("react", {paths: [root]}));

let hooks = [];
let index = 0;
let effects = [];
function changed(a, b) {
  return !a || !b || a.length !== b.length || a.some((v, i) => !Object.is(v, b[i]));
}
React.useState = (initial) => {
  const slot = index++;
  if (!(slot in hooks)) hooks[slot] = typeof initial === "function" ? initial() : initial;
  return [hooks[slot], (next) => { hooks[slot] = typeof next === "function" ? next(hooks[slot]) : next; }];
};
React.useRef = (initial) => {
  const slot = index++;
  if (!(slot in hooks)) hooks[slot] = {current: initial};
  return hooks[slot];
};
React.useMemo = (fn, deps) => {
  const slot = index++;
  if (!hooks[slot] || changed(hooks[slot].deps, deps)) hooks[slot] = {deps, value: fn()};
  return hooks[slot].value;
};
React.useCallback = (fn, deps) => React.useMemo(() => fn, deps);
React.useEffect = (fn, deps) => {
  const slot = index++;
  if (!hooks[slot] || changed(hooks[slot].deps, deps)) {
    const previous = hooks[slot];
    hooks[slot] = {deps};
    effects.push(() => {
      if (previous?.cleanup) previous.cleanup();
      hooks[slot].cleanup = fn();
    });
  }
};
React.useSyncExternalStore = (_subscribe, snapshot) => snapshot();

function compile(module, filename) {
  const source = fs.readFileSync(filename, "utf8");
  const result = ts.transpileModule(source, {
    fileName: filename,
    compilerOptions: {jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022, esModuleInterop: true},
  });
  module._compile(result.outputText, filename);
}
Module._extensions[".ts"] = compile;
Module._extensions[".tsx"] = compile;
const resolve = Module._resolveFilename;
Module._resolveFilename = function(request, parent, isMain, options) {
  return resolve.call(this, request.startsWith("@/") ? path.join(root, request.slice(2)) : request,
    parent, isMain, options);
};
const load = Module._load;
const wrapper = (tag) => ({children, ...props}) => React.createElement(tag, props, children);
Module._load = function(request, parent, isMain) {
  if (request === "@/components/ui/button") return {Button: wrapper("button")};
  if (request === "@/components/ui/badge") return {Badge: wrapper("span")};
  if (request === "@/components/ui/card") return Object.fromEntries(
    ["Card", "CardContent", "CardDescription", "CardHeader", "CardTitle"].map((x) => [x, wrapper("div")])
  );
  if (request === "lucide-react") return new Proxy({}, {get: () => wrapper("svg")});
  return load.call(this, request, parent, isMain);
};

const {LlmConfigCard} = require(path.join(root, "components/settings/llm-config-card.tsx"));
const credential = (source = "none") => ({supported: true, required: true, configured: source !== "none",
  source, masked: source === "none" ? null : "****1234", last4: "1234", env_var: "EXAMPLE_API_KEY"});
const provider = (id, required = false) => ({id, label: id.toUpperCase(), auth_mode: "api_key",
  requires_backend_url: required, default_backend_url: null,
  credential: {quick: credential(), deep: credential()},
  models: {quick: [{id: `${id}-model`, label: `${id} model`}], deep: [{id: `${id}-model`, label: `${id} model`}]}});
const catalog = {generated_at: "now", providers: {openai: provider("openai"), google: provider("google"),
  openai_compatible: provider("openai_compatible", true)},
  tiers: {quick: {provider: "openai"}, deep: {provider: "openai"}}};
const values = {llm_provider: "openai", quick_think_provider: "", deep_think_provider: "",
  quick_think_llm: "openai-model", deep_think_llm: "openai-model", backend_url: "",
  quick_think_backend_url: "", deep_think_backend_url: "", openai_reasoning_effort: "",
  google_thinking_level: "", anthropic_effort: ""};
const controller = {formValues: values, fieldErrors: {}, saveState: {kind: "idle"},
  payload: {settings: [{key: "llm_provider", value: "openai"}]},
  setValue(key, value) { this.formValues[key] = value; delete this.fieldErrors[key]; }};
// The production controller passes an unbound useCallback, so mirror that contract.
controller.setValue = (key, value) => {values[key] = value; delete controller.fieldErrors[key];};
let requests = [];
let dirty = [];
let holdPut = false;
let resolvePut;
let failGet = false;
let failPut = false;
let holdNextGet = false;
let heldGet;
let resolveGet;
let removeResult = {removed: true, configured: false, source: "none"};
let confirmResult = true;
global.window = {confirm: () => confirmResult};
global.fetch = async (url, init = {}) => {
  requests.push({url, method: init.method || "GET", body: init.body});
  if (url === "/api/llm-config") {
    if (failGet) throw new Error("offline");
    const snapshot = structuredClone(catalog);
    if (holdNextGet) {
      holdNextGet = false;
      heldGet = true;
      await new Promise((resolve) => { resolveGet = resolve; });
    }
    return {ok: true, json: async () => snapshot};
  }
  if (init.method === "PUT" && failPut) {
    return {ok: false, status: 422, json: async () => ({detail: "secret-server-error"})};
  }
  if (init.method === "PUT" && holdPut) {
    await new Promise((resolve) => { resolvePut = resolve; });
  }
  return {ok: true, json: async () => init.method === "DELETE" ? removeResult : {}};
};
function render() {
  index = 0;
  effects = [];
  const tree = LlmConfigCard({controller, onDirtyChange: (value) => dirty.push(value)});
  for (const effect of effects) effect();
  return tree;
}
function walk(node, out = []) {
  if (node == null || typeof node === "boolean") return out;
  if (Array.isArray(node)) { for (const child of node) walk(child, out); return out; }
  if (typeof node === "string" || typeof node === "number") return out;
  if (typeof node.type === "function") return walk(node.type(node.props), out);
  out.push(node);
  walk(node.props?.children, out);
  return out;
}
function text(node) {
  if (node == null || typeof node === "boolean") return "";
  if (Array.isArray(node)) return node.map(text).join("");
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (typeof node.type === "function") return text(node.type(node.props));
  return text(node.props?.children);
}
function nodes() { return walk(render()); }
function byId(id) { const node = nodes().find((n) => n.props?.id === id); assert.ok(node, id); return node; }
function button(label) { const node = nodes().find((n) => n.type === "button" && text(n).includes(label)); assert.ok(node, label); return node; }
function change(id, value) { byId(id).props.onChange({target: {value}}); }
function click(label) { const node = button(label); assert.ok(!node.props.disabled, `${label} disabled`); node.props.onClick(); }
async function settle() { await new Promise((resolve) => setTimeout(resolve, 0)); await Promise.resolve(); render(); }
async function start() { render(); await settle(); }

async function drafts() {
  await start();
  change("llm-quick-api-key", "secret-openai-quick");
  assert.equal(dirty.at(-1), false); render(); assert.equal(dirty.at(-1), true);
  change("llm-quick-provider", "google");
  assert.equal(byId("llm-quick-api-key").props.value, "");
  assert.match(text(render()), /provider selection is not saved yet/);
  change("llm-quick-api-key", "secret-google-quick");
  click("Deep");
  assert.equal(byId("llm-deep-api-key").props.value, "");
  change("llm-deep-api-key", "secret-openai-deep");
  click("Quick");
  assert.equal(byId("llm-quick-api-key").props.value, "secret-google-quick");
  change("llm-quick-provider", "");
  assert.equal(byId("llm-quick-api-key").props.value, "secret-openai-quick");
  holdPut = true;
  click("Save key");
  assert.equal(button("Deep").props.disabled, true);
  assert.equal(byId("llm-quick-provider").props.disabled, true);
  assert.equal(byId("llm-default-provider").props.disabled, true);
  assert.equal(byId("llm-quick-api-key").props.disabled, true);
  byId("llm-quick-api-key").props.onChange({target: {value: "late-edit"}});
  const put = requests.find((item) => item.method === "PUT");
  assert.equal(put.url, "/api/llm-credentials/quick/openai");
  assert.deepEqual(JSON.parse(put.body), {api_key: "secret-openai-quick"});
  resolvePut(); await settle();
  assert.equal(byId("llm-quick-api-key").props.value, "");
  assert.equal(dirty.at(-1), true);
  change("llm-quick-provider", "google");
  assert.equal(byId("llm-quick-api-key").props.value, "secret-google-quick");
  click("Deep"); assert.equal(byId("llm-deep-api-key").props.value, "secret-openai-deep");
  controller.saveState.kind = "saving";
  assert.equal(button("Save key").props.disabled, true);
  assert.equal(button("Quick").props.disabled, true);
}
async function status() {
  catalog.providers.openai.credential.quick = credential("env");
  await start();
  assert.match(text(render()), /comes from the environment/);
  assert.ok(!nodes().some((n) => n.type === "button" && text(n).includes("Remove key")));
  catalog.providers.openai.credential.quick = credential("vault");
  // Refresh through the component's normal saved-settings path.
  controller.saveState.kind = "saved"; render(); await settle();
  assert.match(text(render()), /stored in the vault/);
  removeResult = {removed: false, configured: true, source: "env"};
  catalog.providers.openai.credential.quick = credential("env");
  click("Remove key"); await settle();
  assert.match(text(render()), /No stored key was removed/);
  assert.doesNotMatch(text(render()), /Stored API key removed/);
  assert.match(text(render()), /comes from the environment/);
  assert.ok(!nodes().some((n) => n.type === "button" && text(n).includes("Remove key")));
}
async function refresh() {
  catalog.providers.openai.credential.quick = credential("vault");
  await start();
  removeResult = {removed: true, configured: true, source: "env"};
  failGet = true;
  click("Remove key"); await settle();
  assert.match(text(render()), /environment key is now active/);
  assert.match(text(render()), /Could not refresh credential status/);
  assert.equal(button("Remove key").props.disabled, true);
  change("llm-quick-api-key", "draft");
  assert.equal(button("Save key").props.disabled, true);
  failGet = false; catalog.providers.openai.credential.quick = credential("env");
  click("Retry"); await settle();
  assert.doesNotMatch(text(render()), /Could not refresh credential status/);
  assert.equal(button("Save key").props.disabled, false);
}
async function fields() {
  controller.fieldErrors = {quick_think_llm: "Model error", deep_think_backend_url: "Deep URL error",
    llm_provider: "Default provider error", anthropic_effort: "Anthropic error"};
  await start();
  assert.equal(byId("llm-quick-model").props["aria-invalid"], true);
  assert.match(text(button("Deep")), /Has errors/);
  assert.match(text(render()), /Anthropic error/);
  assert.equal(byId("llm-default-provider").props["aria-invalid"], true);
  const details = nodes().find((n) => n.type === "details");
  assert.equal(details.props.open, true);
  assert.ok(nodes().some((n) => n.type === "summary" && text(n).includes("Advanced")));
  click("Deep");
  assert.equal(byId("llm-deep-backend-url").props["aria-invalid"], true);
  assert.equal(byId("llm-deep-backend-url").props["aria-describedby"], "llm-deep-backend-url-hint llm-deep-backend-url-error");
  assert.match(text(render()), /default provider URL when this tier uses the default provider/);
  click("Quick");
  values.quick_think_llm = "my-custom-model";
  values.quick_think_backend_url = "https://example.invalid";
  change("llm-quick-provider", "google");
  assert.equal(values.quick_think_llm, "my-custom-model");
  assert.equal(values.quick_think_backend_url, "https://example.invalid");
  assert.equal(byId("llm-quick-custom-model").props.value, "my-custom-model");
  assert.match(text(render()), /Changing provider keeps the current model and URL/);
  assert.match(text(render()), /default provider URL does not apply/);
  assert.match(text(render()), /shared across tiers/);
  assert.ok(byId("llm-default-backend-url"));
  assert.ok(byId("llm-quick-backend-url"));
  change("llm-quick-provider", "openai_compatible");
  assert.ok(nodes().some((n) => n.type === "option" && n.props.value === "openai_compatible"));
  assert.equal(byId("llm-quick-custom-model").props.value, "my-custom-model");
}
async function failure() {
  await start();
  change("llm-quick-api-key", "secret-server-error");
  failPut = true;
  click("Save key"); await settle();
  assert.equal(byId("llm-quick-api-key").props.value, "secret-server-error");
  assert.match(text(render()), /Could not update the API key/);
  assert.doesNotMatch(text(render()), /secret-server-error/);
  assert.equal(dirty.at(-1), true);
}
async function stale() {
  await start();
  holdNextGet = true;
  controller.saveState.kind = "saved"; render(); await settle();
  assert.equal(heldGet, true);
  catalog.providers.openai.credential.quick = credential("vault");
  change("llm-quick-api-key", "secret");
  click("Save key"); await settle();
  assert.match(text(render()), /stored in the vault/);
  resolveGet(); await settle();
  assert.match(text(render()), /stored in the vault/);
}
const scenarios = {drafts, status, refresh, fields, failure, stale};
scenarios[process.argv[1]]().then(() => process.stdout.write("ok\n"), (error) => {
  console.error(error); process.exitCode = 1;
});
"""


@pytest.mark.unit
@pytest.mark.parametrize("scenario", ["drafts", "status", "refresh", "fields", "failure", "stale"])
def test_llm_settings_ui_offline(scenario):
    web_root = require_web_renderer()
    result = subprocess.run(
        ["node", "-e", NODE_TEST, scenario],
        cwd=web_root,
        env={**os.environ, "NODE_ENV": "test"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
