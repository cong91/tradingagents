"""Field descriptions expose Kronos limits, units and server errors together."""

import json
import os
import subprocess
from html.parser import HTMLParser

import pytest

from tests.support.node_renderer import require_web_renderer

pytestmark = pytest.mark.unit

_RENDER = r"""
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const root = process.cwd();
function compile(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const result = ts.transpileModule(source, {fileName: filename, compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022, esModuleInterop: true,
  }});
  module._compile(result.outputText, filename);
}
Module._extensions['.ts'] = compile;
Module._extensions['.tsx'] = compile;
const resolve = Module._resolveFilename;
Module._resolveFilename = function(request, parent, isMain, options) {
  return resolve.call(this, request.startsWith('@/') ? path.join(root, request.slice(2)) : request,
    parent, isMain, options);
};
const locale = require(path.join(root, 'lib/i18n/locale.ts'));
const language = process.env.TEST_LOCALE || 'en';
locale.getServerLocale = () => language;
locale.getLocale = () => language;
const {SettingField} = require(path.join(root, 'components/settings/setting-field.tsx'));
const items = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(items.map(item => renderToStaticMarkup(
  React.createElement(SettingField, {...item, onChange: () => {}})
))));
"""


class _Descriptions(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.inputs = []
        self.selects = []
        self.options = []
        self.ids = set()
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "input":
            self.inputs.append(values)
        if tag == "select":
            self.selects.append(values)
        if tag == "option":
            self.options.append(values)


def test_kronos_profile_select_has_closed_options_and_accessible_descriptions():
    web = require_web_renderer()
    cases = [{
        "row": {
            "key": "kronos_active_profile", "type": "str", "editable": True,
            "value": "small_base", "default": "small_base", "source": "default",
            "env_var": "TRADINGAGENTS_KRONOS_ACTIVE_PROFILE",
        },
        "value": "small_base", "error": "Profile unavailable",
    }]
    labels = {
        "en": ("Kronos model profile", "Kronos-small + Kronos-Tokenizer-base", "Kronos-mini + Kronos-Tokenizer-2k"),
        "vi": ("Hồ sơ model Kronos", "Kronos-small + Kronos-Tokenizer-base", "Kronos-mini + Kronos-Tokenizer-2k"),
    }
    for language, (label, small, mini) in labels.items():
        result = subprocess.run(
            ["node", "-e", _RENDER], cwd=web,
            env={**os.environ, "NODE_ENV": "production", "TEST_LOCALE": language},
            input=json.dumps(cases), encoding="utf-8", capture_output=True, timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        rendered = _Descriptions(json.loads(result.stdout)[0])
        select = rendered.selects[0]
        assert select["id"] == "setting-kronos_active_profile"
        assert select["aria-invalid"] == "true"
        assert select["aria-describedby"].split() == [
            "setting-kronos_active_profile-hint",
            "setting-kronos_active_profile-error",
        ]
        assert set(select["aria-describedby"].split()) <= rendered.ids
        assert rendered.options == [
            {"value": "small_base", "selected": ""},
            {"value": "mini_2k"},
        ]
        markup = json.loads(result.stdout)[0]
        assert label in markup
        assert small in markup and mini in markup
        assert "<input" not in markup


def test_kronos_field_keeps_limit_unit_and_error_descriptions():
    web = require_web_renderer()
    cases = [
        {
            "row": {
                "key": "kronos_active_horizon_hours", "type": "int", "editable": True,
                "value": 24, "default": 24, "source": "default", "env_var": "TEST_HORIZON",
            },
            "value": "25", "error": "Must be a whole number of 4h candles",
        },
        {
            "row": {
                "key": "future_setting", "type": "str", "editable": True,
                "value": "", "default": "", "source": "default", "env_var": "TEST_FUTURE",
            },
            "value": "",
        },
    ]
    result = subprocess.run(
        ["node", "-e", _RENDER], cwd=web,
        env={**os.environ, "NODE_ENV": "production"},
        input=json.dumps(cases), encoding="utf-8", capture_output=True, timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    horizon, unknown = map(_Descriptions, json.loads(result.stdout))
    field = horizon.inputs[0]
    assert field["aria-invalid"] == "true"
    assert field["aria-describedby"].split() == [
        "setting-kronos_active_horizon_hours-hint",
        "setting-kronos_active_horizon_hours-unit",
        "setting-kronos_active_horizon_hours-error",
    ]
    assert set(field["aria-describedby"].split()) <= horizon.ids
    assert "aria-describedby" not in unknown.inputs[0]
