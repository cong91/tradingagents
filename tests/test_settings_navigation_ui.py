"""Exercise settings navigation and scoped saves with the offline web renderer."""

import json
import os
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.support.node_renderer import require_web_renderer

DRIVER = Path(__file__).with_name("settings_ui_test_driver.cjs")
AREAS = ("automation", "ai", "risk", "connections", "system")
CLASSIFICATION = {
    "llm_provider": "ai",
    "temperature": "ai",
    "quick_think_fallback_models": "ai",
    "deep_think_fallback_models": "ai",
    "max_tool_rounds": "ai",
    "risk_max_daily_loss_pct": "risk",
    "risk_derivatives_max_hold_hours": "risk",
    "risk_future_limit": "risk",
    "exec_exchange_id": "connections",
    "exec_base_url": "connections",
    "exec_sandbox": "connections",
    "benchmark_ticker": "system",
    "checkpoint_enabled": "system",
    "future_server_setting": "system",
    "future_server_path": "system",
}
HASHES = {
    "#automation": "automation",
    "#ai": "ai",
    "#risk": "risk",
    "#connections": "connections",
    "#system": "system",
    "automation": "automation",
    "#operator-config": "automation",
    "#exec-gates": "automation",
    "operator-config": "automation",
    "exec-gates": "automation",
    "": None,
    "#": None,
    "#not-a-section": None,
    "#RISK": None,
    "##risk": None,
}
LABELS = {
    "vi": {
        "nav": "Nhóm cấu hình",
        "areas": ["Tự động", "Mô hình AI", "Rủi ro", "Sàn & nguồn dữ liệu", "Giao diện & nâng cao"],
        "clean": "Không có thay đổi chưa lưu trong nhóm này.",
        "one": "1 thay đổi chưa lưu.",
        "two": "2 thay đổi chưa lưu.",
        "saving": "Đang lưu thay đổi…",
        "saved": "Đã lưu lúc 12:34:56 — áp dụng cho phiên phân tích mới.",
        "buttons": ["Huỷ thay đổi", "Lưu cài đặt"],
    },
    "en": {
        "nav": "Settings sections",
        "areas": ["Automation", "AI models", "Risk", "Exchange & data", "Interface & advanced"],
        "clean": "No unsaved changes in this section.",
        "one": "1 unsaved change.",
        "two": "2 unsaved changes.",
        "saving": "Saving changes…",
        "saved": "Saved at 12:34:56 — applies to new analysis runs.",
        "buttons": ["Discard changes", "Save settings"],
    },
}
RISK_KEYS = ["risk_max_daily_loss_pct", "risk_future_limit"]
AI_KEY = "llm_provider"


def _row(key, value="example", *, row_type="str", editable=True):
    return {
        "key": key,
        "env_var": f"TEST_{key.upper()}",
        "value": value,
        "default": value,
        "type": row_type,
        "editable": editable,
        "source": "default",
    }


ROWS = [
    _row("risk_max_daily_loss_pct", 3.5, row_type="float"),
    _row("risk_future_limit", 5, row_type="int"),
    _row("risk_unmanaged_limit", 2, row_type="int"),
    _row("llm_provider", "provider-a"),
    _row("exec_base_url", "example", editable=False),
    _row("future_server_setting", "new setting"),
    _row("future_server_path", "example", editable=False),
]


def _bar(dirty, *, keys=RISK_KEYS, scope="risk", state=None):
    props = {}
    if keys is not None:
        props["keys"] = keys
    if scope is not None:
        props["scope"] = scope
    return {"dirtyKeys": dirty, "saveState": state or {"kind": "idle"}, "props": props}


def _input():
    risk_and_ai = [RISK_KEYS[0], AI_KEY]
    saved = {"kind": "saved", "scope": "risk", "appliedAt": "2026-11-09T12:34:56Z"}
    return {
        "settingKeys": list(CLASSIFICATION),
        "hashes": list(HASHES),
        "rows": ROWS,
        "allowlists": {
            "populated": [RISK_KEYS[0], "llm_provider", "not_in_settings"],
            "empty": [],
            "unavailable": None,
        },
        "ownershipCases": {
            "matched": {"keys": [RISK_KEYS[0], RISK_KEYS[1]], "allowlist": [RISK_KEYS[1], RISK_KEYS[0]]},
            "empty": {"keys": [], "allowlist": []},
            "empty_allowlist": {"keys": [RISK_KEYS[0]], "allowlist": []},
            "missing_key": {"keys": RISK_KEYS, "allowlist": [RISK_KEYS[0]]},
            "extra_key": {"keys": [RISK_KEYS[0]], "allowlist": RISK_KEYS},
            "duplicate_rows": {"keys": [RISK_KEYS[0], RISK_KEYS[0]], "allowlist": [RISK_KEYS[0]]},
        },
        "filterCases": {
            "empty_allowlist": {"allowlist": [], "operatorKeys": [RISK_KEYS[0]]},
            "union": {"allowlist": [RISK_KEYS[1]], "operatorKeys": [RISK_KEYS[0]]},
            "unavailable_owned_nonrisk": {"allowlist": None, "operatorKeys": ["future_server_setting"]},
        },
        "selectedAreas": list(AREAS),
        "screenPayload": {
            "generated_at": "2026-11-09T12:00:00Z",
            "settings": ROWS,
            "credentials": [],
            "gates_readonly": {},
            "effective_exec_mode": "dry",
        },
        "operatorPayload": {
            "generated_at": "2026-11-09T12:00:00Z",
            "settings": [{
                "key": RISK_KEYS[0], "type": "float", "group": "risk", "label": "Risk",
                "hint": "Risk limit", "gate": False, "value": 3.5, "default": 3.5,
                "override": False, "stored_value": None, "updated_at": None, "updated_by": None,
            }],
            "allowlist": [RISK_KEYS[0]],
            "gates": {"auto_spot": False, "auto_futures": False, "any_auto_enabled": False},
            "effective_exec_mode": "dry",
        },
        "bars": {
            "clean_other": _bar([AI_KEY]),
            "dirty_other": _bar(risk_and_ai),
            "dirty_two": _bar([*RISK_KEYS, AI_KEY]),
            "empty_scope": _bar(risk_and_ai, keys=[]),
            "unscoped": _bar(risk_and_ai, keys=None, scope=None),
            "failed_other": _bar(risk_and_ai, state={"kind": "failed", "scope": "ai", "message": "AI failed"}),
            "failed_same": _bar(risk_and_ai, state={"kind": "failed", "scope": "risk", "message": "Risk failed"}),
            "saved_other": _bar(risk_and_ai, state={**saved, "scope": "ai"}),
            "saved_same": _bar([AI_KEY], state=saved),
            "saving_other": _bar(risk_and_ai, state={"kind": "saving", "scope": "ai"}),
            "saving_same": _bar(risk_and_ai, state={"kind": "saving", "scope": "risk"}),
        },
        "payload": {
            "generated_at": "2026-11-09T12:00:00Z",
            "settings": [
                _row("llm_provider", "provider-a"),
                _row("risk_max_daily_loss_pct", 3.5, row_type="float"),
                _row("checkpoint_enabled", False, row_type="bool"),
                _row("max_debate_rounds", 2, row_type="int"),
            ],
            "credentials": [],
            "gates_readonly": {},
            "effective_exec_mode": "dry",
        },
    }


class _RenderedHtml(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.navs = []
        self.links = []
        self.buttons = []
        self.statuses = []
        self.text_parts = []
        self.ids = []
        self._text_target = None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if tag == "nav":
            self.navs.append(attributes)
        elif tag in {"a", "button"} or (tag == "p" and "aria-live" in attributes):
            element = {"attrs": attributes, "text": ""}
            collection = {"a": self.links, "button": self.buttons, "p": self.statuses}[tag]
            collection.append(element)
            self._text_target = element

    def handle_data(self, data):
        self.text_parts.append(data)
        if self._text_target is not None:
            self._text_target["text"] += data

    def handle_endtag(self, tag):
        if tag in {"a", "button", "p"}:
            self._text_target = None


@pytest.fixture(scope="module")
def results():
    web_root = require_web_renderer()
    env = {
        name: os.environ[name]
        for name in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if name in os.environ
    }
    env.update(TZ="UTC", NODE_ENV="production")
    result = subprocess.run(
        ["node", str(DRIVER)],
        cwd=web_root,
        input=json.dumps(_input()),
        capture_output=True,
        check=False,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.unit
@pytest.mark.parametrize(("key", "expected"), CLASSIFICATION.items())
def test_settings_fields_are_assigned_to_expected_sections(results, key, expected):
    assert dict(results["areas"])[key] == expected


@pytest.mark.unit
@pytest.mark.parametrize(("hash_value", "expected"), HASHES.items())
def test_old_and_new_hashes_select_the_expected_section(results, hash_value, expected):
    assert dict(results["hashes"])[hash_value] == expected


@pytest.mark.unit
def test_allowlist_removes_only_owned_rows_and_keeps_unknown_fields(results):
    assert results["visible"]["populated"] == [
        row for row in ROWS if row["key"] not in {RISK_KEYS[0], AI_KEY}
    ]
    assert results["visible"]["empty"] == ROWS


@pytest.mark.unit
def test_unavailable_allowlist_hides_all_risk_rows_but_keeps_unknown_fields(results):
    assert results["visible"]["unavailable"] == [
        row for row in ROWS if not row["key"].startswith("risk_")
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("matched", True),
        ("empty", True),
        ("empty_allowlist", False),
        ("missing_key", False),
        ("extra_key", False),
        ("duplicate_rows", False),
    ],
)
def test_operator_ownership_requires_matching_unique_rows(results, case, expected):
    assert results["ownership"][case] is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "hidden_keys"),
    [
        ("empty_allowlist", {RISK_KEYS[0]}),
        ("union", set(RISK_KEYS)),
    ],
)
def test_visible_rows_exclude_operator_keys_even_when_allowlist_is_incomplete(results, case, hidden_keys):
    assert results["filteredOwnership"][case] == [
        row for row in ROWS if row["key"] not in hidden_keys
    ]


@pytest.mark.unit
def test_unavailable_allowlist_also_excludes_owned_nonrisk_fields(results):
    assert results["filteredOwnership"]["unavailable_owned_nonrisk"] == [
        row for row in ROWS
        if not row["key"].startswith("risk_") and row["key"] != "future_server_setting"
    ]


@pytest.mark.unit
@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize("scenario", ["empty_allowlist", "missing_key"])
def test_screen_blocks_risk_controls_when_operator_ownership_is_inconsistent(results, language, scenario):
    page = _RenderedHtml(results["screens"][language][scenario])
    assert not any(element.startswith("setting-risk_") for element in page.ids)
    assert "operator-risk_max_daily_loss_pct" not in page.ids
    assert page.ids.count("setting-future_server_setting") == 1
    notice = {
        "vi": "Không thể xác định các giới hạn do máy chủ quản lý.",
        "en": "Managed risk limits could not be loaded.",
    }[language]
    assert notice in " ".join(page.text_parts)


@pytest.mark.unit
@pytest.mark.parametrize("language", ["vi", "en"])
def test_screen_keeps_unmanaged_and_unknown_fields_without_duplicate_operator_fields(results, language):
    page = _RenderedHtml(results["screens"][language]["ready"])
    assert page.ids.count("operator-risk_max_daily_loss_pct") == 1
    assert "setting-risk_max_daily_loss_pct" not in page.ids
    assert page.ids.count("setting-risk_future_limit") == 1
    assert page.ids.count("setting-risk_unmanaged_limit") == 1
    assert page.ids.count("setting-future_server_setting") == 1
    assert "future_server_path" in " ".join(page.text_parts)


@pytest.mark.unit
@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize("phase", ["loading", "error"])
def test_screen_fails_closed_on_missing_operator_allowlist(results, language, phase):
    page = _RenderedHtml(results["screens"][language][phase])
    assert not any(element.startswith("setting-risk_") for element in page.ids)
    assert "operator-risk_max_daily_loss_pct" not in page.ids
    assert page.ids.count("setting-future_server_setting") == 1
    assert "future_server_path" in " ".join(page.text_parts)
    expected_notice = {
        ("vi", "loading"): "Đang kiểm tra các giới hạn rủi ro…",
        ("en", "loading"): "Checking managed risk limits…",
        ("vi", "error"): "Không thể xác định các giới hạn do máy chủ quản lý.",
        ("en", "error"): "Managed risk limits could not be loaded.",
    }[language, phase]
    assert expected_notice in " ".join(page.text_parts)


@pytest.mark.unit
@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize("selected", AREAS)
def test_navigation_has_localized_links_active_page_and_selection_callback(results, language, selected):
    rendered = results["rendered"][language]["navigation"][selected]
    page = _RenderedHtml(rendered["markup"])
    assert len(page.navs) == 1
    assert page.navs[0]["aria-label"] == LABELS[language]["nav"]
    assert [link["text"].strip() for link in page.links] == LABELS[language]["areas"]
    assert [link["attrs"]["href"] for link in page.links] == [f"#{area}" for area in AREAS]
    assert [link["attrs"].get("aria-current") for link in page.links] == [
        "page" if area == selected else None for area in AREAS
    ]
    assert rendered["events"] == list(AREAS)
    assert rendered["hashes"] == [f"#{area}" for area in AREAS]


@pytest.mark.unit
@pytest.mark.parametrize("language", ["vi", "en"])
def test_modified_navigation_clicks_keep_browser_link_behavior(results, language):
    clicks = results["rendered"][language]["navigation"]["automation"]["modifiedClicks"]
    assert [click["modifiers"] for click in clicks] == [
        {"ctrlKey": True}, {"metaKey": True}, {"shiftKey": True}, {"altKey": True},
    ]
    for click in clicks:
        assert click["prevented"] is False, click["modifiers"]
        assert click["events"] == []
        assert click["hashes"] == []


@pytest.mark.unit
@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize(
    ("scenario", "status", "disabled", "keys", "scope"),
    [
        ("clean_other", "clean", True, RISK_KEYS, "risk"),
        ("dirty_other", "one", False, RISK_KEYS, "risk"),
        ("dirty_two", "two", False, RISK_KEYS, "risk"),
        ("empty_scope", "clean", True, [], "risk"),
        ("unscoped", "two", False, None, "settings"),
        ("failed_other", "one", False, RISK_KEYS, "risk"),
        ("failed_same", "Risk failed", False, RISK_KEYS, "risk"),
        ("saved_other", "one", False, RISK_KEYS, "risk"),
        ("saved_same", "saved", True, RISK_KEYS, "risk"),
        ("saving_other", "one", True, RISK_KEYS, "risk"),
        ("saving_same", "saving", True, RISK_KEYS, "risk"),
    ],
)
def test_save_bar_shows_scope_specific_status_and_actions(
    results, language, scenario, status, disabled, keys, scope,
):
    rendered = results["rendered"][language]["bars"][scenario]
    page = _RenderedHtml(rendered["markup"])
    assert len(page.statuses) == 1
    assert page.statuses[0]["attrs"]["aria-live"] == "polite"
    assert page.statuses[0]["text"].strip() == LABELS[language].get(status, status)
    assert [button["text"].strip() for button in page.buttons] == LABELS[language]["buttons"]
    assert [("disabled" in button["attrs"]) for button in page.buttons] == [disabled, disabled]
    assert rendered["calls"] == ([] if disabled else [
        {"action": "reset", "keys": keys},
        {"action": "save", "keys": keys, "scope": scope},
    ])


@pytest.mark.unit
def test_scoped_save_sends_only_selected_dirty_values_and_preserves_concurrent_edits(results):
    hook = results["hook"]
    assert hook["loaded"]["phase"] == "ready"
    assert hook["loaded"]["dirtyKeys"] == []
    assert hook["saving"]["saveState"] == {"kind": "saving", "scope": "ai"}
    assert hook["requests"] == [
        {
            "method": "PUT",
            "url": "/api/settings",
            "headers": {"Content-Type": "application/json"},
            "values": {"llm_provider": "provider-b"},
        },
        {
            "method": "PUT",
            "url": "/api/settings",
            "headers": {"Content-Type": "application/json"},
            "values": {"risk_max_daily_loss_pct": 9.5},
        },
    ]
    saved = hook["afterFirstSave"]
    assert saved["saveState"] == {
        "kind": "saved", "scope": "ai", "appliedAt": "2026-11-09T12:01:00Z",
    }
    assert saved["formValues"]["llm_provider"] == "provider-c"
    assert saved["formValues"]["risk_max_daily_loss_pct"] == "9.5"
    assert saved["formValues"]["checkpoint_enabled"] is True
    assert set(saved["dirtyKeys"]) == {"llm_provider", "risk_max_daily_loss_pct", "checkpoint_enabled"}


@pytest.mark.unit
def test_scoped_reset_and_validation_leave_other_sections_dirty(results):
    hook = results["hook"]
    reset = hook["afterReset"]
    assert reset["formValues"]["llm_provider"] == "provider-b"
    assert set(reset["dirtyKeys"]) == {"risk_max_daily_loss_pct", "checkpoint_enabled"}

    saved = hook["afterRiskSave"]
    assert saved["saveState"]["kind"] == "saved"
    assert saved["saveState"]["scope"] == "risk"
    assert saved["formValues"]["risk_max_daily_loss_pct"] == "9.5"
    assert set(saved["dirtyKeys"]) == {"max_debate_rounds", "checkpoint_enabled"}
    assert saved["fieldErrors"] == {}

    invalid = hook["invalidAi"]
    assert invalid["saveState"]["kind"] == "failed"
    assert invalid["saveState"]["scope"] == "ai"
    assert set(invalid["fieldErrors"]) == {"max_debate_rounds"}
    assert invalid["formValues"]["max_debate_rounds"] == "not-a-number"
    assert set(invalid["dirtyKeys"]) == {"max_debate_rounds", "checkpoint_enabled"}
    assert hook["afterErrorReset"]["fieldErrors"] == {}
    assert hook["afterErrorReset"]["dirtyKeys"] == ["checkpoint_enabled"]
