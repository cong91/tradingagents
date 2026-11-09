"""Exercise data-source cookie removal with mocked fetch and confirmation, offline."""

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
const root = process.cwd();
const ts = require(require.resolve("typescript", { paths: [root] }));
const React = require(require.resolve("react", { paths: [root] }));
const test = JSON.parse(fs.readFileSync(0, "utf8"));

function compile(module, filename) {
  const result = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    fileName: filename,
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022, esModuleInterop: true },
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
const copy = require(path.join(root, "lib/i18n/messages/data-sources.ts"))[test.language];
const Button = function Button() {};
const load = Module._load;
Module._load = function(request, parent, isMain) {
  if (request === "@/lib/i18n") return { useT: () => ({ dataSources: copy }) };
  if (request === "@/lib/api") return {
    networkHint: () => "Network unavailable",
    parseErrorResponse: async () => ({ code: "failed", message: "synthetic-cookie-secret" }),
  };
  if (request === "@/components/ui/button") return { Button };
  if (request.startsWith("@/components/ui/")) return new Proxy({}, { get: () => function Stub() {} });
  if (request === "lucide-react") return new Proxy({}, { get: () => function Icon() {} });
  return load.call(this, request, parent, isMain);
};

// Each mounted component has independent hook slots, as in the real React tree.
let active;
const depsChanged = (a, b) => !a || !b || a.length !== b.length ||
  a.some((value, index) => !Object.is(value, b[index]));
React.useState = (initial) => {
  const owner = active;
  const index = owner.index++;
  if (!(index in owner.slots)) {
    const slot = { value: typeof initial === "function" ? initial() : initial };
    slot.set = (next) => { slot.value = typeof next === "function" ? next(slot.value) : next; };
    owner.slots[index] = slot;
  }
  const slot = owner.slots[index];
  return [slot.value, slot.set];
};
React.useCallback = (callback, deps) => {
  const owner = active;
  const index = owner.index++;
  if (!owner.slots[index] || depsChanged(owner.slots[index].deps, deps)) {
    owner.slots[index] = { value: callback, deps };
  }
  return owner.slots[index].value;
};
React.useEffect = (effect, deps) => {
  const owner = active;
  const index = owner.index++;
  if (!owner.slots[index] || depsChanged(owner.slots[index].deps, deps)) {
    const previous = owner.slots[index];
    const slot = { deps };
    owner.slots[index] = slot;
    owner.effects.push(() => {
      previous?.cleanup?.();
      slot.cleanup = effect();
    });
  }
};
function driver(component, props) {
  return {
    component, props, slots: [], index: 0, effects: [],
    render() {
      this.index = 0;
      this.effects = [];
      active = this;
      const tree = this.component(this.props);
      active = undefined;
      for (const effect of this.effects) effect();
      return tree;
    },
  };
}
function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!node || typeof node !== "object" || !node.props) return [];
  return [node, ...elements(node.props.children)];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join("");
  if (typeof node === "string" || typeof node === "number") return String(node);
  return node?.props ? text(node.props.children) : "";
}
const settle = () => new Promise((done) => setTimeout(done, 10));
const initial = {
  generated_at: "now", key_path: "test-key-path", providers: ["reddit", "x"].map((provider) => ({
    provider, configured: true, decryptable: true, valid: true, cookie_count: 1,
    cookie_names: ["session"], updated_at: null, updated_by: null,
  })),
};
const requests = [];
const prompts = [];
const logs = [];
const oldError = console.error;
for (const name of ["log", "info", "warn", "error", "debug"]) {
  console[name] = (...args) => logs.push(args.map(String).join(" "));
}
global.window = { confirm(message) { prompts.push(message); return test.confirm !== false; } };
let releaseDelete;
global.fetch = async (url, init = {}) => {
  const method = init.method || "GET";
  requests.push({ url, method, body: init.body });
  if (url === "/api/data-sources" && method === "GET") {
    return { ok: true, json: async () => structuredClone(initial) };
  }
  assert.equal(url, `/api/data-sources/${test.provider}`);
  assert.equal(method, "DELETE");
  assert.equal(prompts.length, 1, "confirmation must precede DELETE");
  assert.equal(init.body, "{}");
  if (test.hold) await new Promise((resolve) => { releaseDelete = resolve; });
  if (test.networkError) throw new Error("synthetic-cookie-secret");
  if (test.httpError) return { ok: false, status: 500 };
  return { ok: true, json: async () => structuredClone(test.response) };
};
const { DataSourceCard } = require(path.join(root, "components/settings/data-source-card.tsx"));
const dirty = [];
const parent = driver(DataSourceCard, { onDirtyChange: (value) => dirty.push(value) });
function providerElement(provider) {
  const tree = parent.render();
  const element = elements(tree).find((node) => node.type?.name === "ProviderCard" &&
    node.props.summary.provider === provider);
  assert.ok(element, `missing provider ${provider}`);
  return element;
}
function renderCard(card, provider) {
  card.props = providerElement(provider).props;
  return card.render();
}
function field(tree) { return elements(tree).find((node) => node.type === "textarea"); }
function removeButton(tree) {
  const button = elements(tree).find((node) => node.type === Button && text(node) === copy.remove);
  assert.ok(button, "missing remove button");
  return button;
}
function feedback(tree, role) {
  return elements(tree).filter((node) => node.props.role === role).map(text);
}
async function run() {
  parent.render();
  await settle();
  const element = providerElement(test.provider);
  const originalSummary = element.props.summary;
  const card = driver(element.type, element.props);
  let tree = renderCard(card, test.provider);
  const other = test.provider === "reddit" ? "x" : "reddit";
  let otherCard;
  let otherSummary;
  if (test.otherDraft) {
    const otherElement = providerElement(other);
    otherSummary = otherElement.props.summary;
    otherCard = driver(otherElement.type, otherElement.props);
    const otherTree = renderCard(otherCard, other);
    field(otherTree).props.onChange({ target: { value: "other-draft" } });
  }
  if (test.draft) {
    field(tree).props.onChange({ target: { value: test.draft } });
    tree = renderCard(card, test.provider);
  }
  const button = removeButton(tree);
  assert.notEqual(button.props.disabled, true);
  button.props.onClick();
  assert.equal(prompts.length, 1);
  assert.ok(prompts[0].includes(copy.removeConfirm(copy.providers[test.provider].title)));
  assert.equal(prompts[0].includes(copy.discardBeforeRemove), Boolean(test.draft));
  if (test.confirm === false) {
    assert.deepEqual(requests.map((request) => request.method), ["GET"]);
  } else if (test.hold) {
    tree = renderCard(card, test.provider);
    assert.equal(removeButton(tree).props.disabled, true);
    assert.equal(field(tree).props.disabled, true);
    releaseDelete();
  }
  await settle();
  tree = renderCard(card, test.provider);
  const changedSummary = providerElement(test.provider).props.summary;
  assert.equal(requests.filter((request) => request.method === "DELETE").length, test.confirm === false ? 0 : 1);
  if (test.expected === "removed") {
    assert.equal(field(tree).props.value, "");
    assert.deepEqual(feedback(tree, "status"), [copy.removed(copy.providers[test.provider].title)]);
    assert.deepEqual(feedback(tree, "alert"), []);
    if (test.keepSnapshot) {
      assert.notEqual(changedSummary, originalSummary);
      assert.equal(changedSummary.configured, false, "confirmed removal updates target status");
      assert.equal(changedSummary.cookie_count, 0);
      assert.deepEqual(changedSummary.cookie_names, []);
    } else {
      assert.notEqual(changedSummary, originalSummary);
    }
  } else {
    assert.equal(field(tree).props.value, test.draft || "");
    assert.equal(changedSummary, originalSummary, "uncertain results must not replace the snapshot");
    assert.deepEqual(feedback(tree, "status"), []);
    if (test.expected === "uncertain") {
      assert.deepEqual(feedback(tree, "alert"), [copy.removeUnconfirmed(copy.providers[test.provider].title)]);
    } else if (test.expected === "network" || test.expected === "http") {
      assert.deepEqual(feedback(tree, "alert"), [copy.removeUnconfirmed(copy.providers[test.provider].title)]);
    } else {
      assert.deepEqual(feedback(tree, "alert"), []);
    }
  }
  if (otherCard) {
    assert.equal(field(renderCard(otherCard, other)).props.value, "other-draft");
    if (test.keepSnapshot) {
      assert.equal(providerElement(other).props.summary, otherSummary,
        "incomplete snapshots must not overwrite other providers");
    }
  }
  parent.render();
  const expectedDirty = Boolean(test.otherDraft || (test.expected !== "removed" && test.draft?.trim()));
  assert.equal(dirty.at(-1), expectedDirty);
  assert.ok(logs.every((entry) => !entry.includes("synthetic-cookie-secret")), "secret was logged");
  assert.ok(![...feedback(tree, "status"), ...feedback(tree, "alert")]
    .some((entry) => entry.includes("synthetic-cookie-secret")), "secret was displayed in feedback");
  process.stdout.write("ok\n");
}
run().catch((error) => { oldError(error); process.exitCode = 1; });
"""


def _payload(providers, **extra):
    return {
        "generated_at": "now",
        "key_path": "test-key-path",
        "providers": [
            {
                "provider": provider,
                "configured": configured,
                "decryptable": True,
                "valid": configured,
                "cookie_count": int(configured),
                "cookie_names": ["session"] if configured else [],
                "updated_at": None,
                "updated_by": None,
            }
            for provider, configured in providers
        ],
        **extra,
    }


def _run_node(**case):
    web_root = require_web_renderer()
    result = subprocess.run(
        ["node", "-e", NODE_TEST],
        cwd=web_root,
        input=json.dumps({"language": "en", "provider": "reddit", **case}),
        env={**os.environ, "NODE_ENV": "test", "TZ": "UTC"},
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"


@pytest.mark.parametrize("provider", ["reddit", "x"])
@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize("draft", ["", "synthetic-cookie-secret", "  "])
def test_cancel_requires_named_confirmation_and_preserves_draft(provider, language, draft):
    _run_node(provider=provider, language=language, draft=draft, confirm=False, expected="cancel")


@pytest.mark.parametrize("provider", ["reddit", "x"])
@pytest.mark.parametrize("draft", ["", "synthetic-cookie-secret"])
def test_confirmed_removal_clears_only_target_draft(provider, draft):
    other = "x" if provider == "reddit" else "reddit"
    _run_node(
        provider=provider,
        draft=draft,
        otherDraft=True,
        hold=True,
        response=_payload([(provider, False), (other, True)], removed=True),
        expected="removed",
    )


@pytest.mark.parametrize("provider", ["reddit", "x"])
def test_payload_without_removed_confirms_exact_provider(provider):
    other = "x" if provider == "reddit" else "reddit"
    _run_node(
        provider=provider,
        draft="synthetic-cookie-secret",
        response=_payload([(other, True), (provider, False)]),
        expected="removed",
    )


@pytest.mark.parametrize(
    "response",
    [
        {"removed": True},
        {"removed": True, "providers": None},
        {"removed": True, "providers": [None]},
        _payload([("reddit", False)], removed=True),
        _payload([("reddit", False), ("reddit", False), ("x", True)], removed=True),
        {"removed": True, "providers": [{"provider": "reddit", "configured": False}]},
        {"providers": [{"provider": "reddit", "configured": False}]},
        _payload([("reddit", True), ("x", True)], removed=True),
    ],
    ids=[
        "missing", "null", "null-entry", "partial", "duplicate", "incomplete",
        "fallback-partial", "contradictory-target",
    ],
)
def test_confirmed_removal_keeps_other_drafts_on_incomplete_snapshot(response):
    _run_node(
        draft="synthetic-cookie-secret",
        otherDraft=True,
        response=response,
        keepSnapshot=True,
        expected="removed",
    )


@pytest.mark.parametrize(
    "response",
    [
        _payload([("reddit", False), ("x", True)], removed=False),
        _payload([("reddit", False), ("x", True)], removed=None),
        _payload([("reddit", False), ("x", True)], removed="true"),
        _payload([("reddit", True), ("x", False)]),
        _payload([("x", False)]),
        _payload([("reddit", False), ("reddit", True), ("x", True)]),
        {"providers": [{"provider": "reddit", "configured": "false"}]},
        {"providers": [{"provider": "reddit"}]},
        {"providers": [None]},
        {"providers": None},
        {},
        None,
    ],
    ids=[
        "removed-false", "removed-null", "removed-string", "still-configured",
        "wrong-provider", "duplicate-provider", "configured-string", "configured-missing",
        "null-provider", "null-providers", "missing-providers", "null-payload",
    ],
)
def test_unconfirmed_removal_preserves_draft_and_snapshot(response):
    _run_node(draft="synthetic-cookie-secret", response=response, expected="uncertain")


@pytest.mark.parametrize("failure", ["network", "http"])
def test_failed_removal_preserves_draft_without_logging_secret(failure):
    _run_node(
        draft="synthetic-cookie-secret",
        networkError=failure == "network",
        httpError=failure == "http",
        expected=failure,
    )
