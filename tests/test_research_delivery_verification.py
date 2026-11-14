"""Required browser acceptance must include isolated Kronos Settings flows."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_research_delivery.py"
_BROWSER_TESTS = [
    "tests/browser/test_dashboard_brief_offline.py",
    "tests/test_operator_settings_kronos_browser.py",
]


def _runner():
    spec = importlib.util.spec_from_file_location("research_delivery_verification", _RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_browser_acceptance_requires_dashboard_and_kronos_settings(tmp_path, monkeypatch):
    runner = _runner()
    for relative in _BROWSER_TESTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(runner, "_REPO", tmp_path)
    monkeypatch.setattr(runner, "_next_dist_dir_wired", lambda: True)
    calls = []

    def run(args, *, reject_skips, label):
        calls.append((args, reject_skips, label))
        return 1

    monkeypatch.setattr(runner, "_run_pytest", run)
    assert runner._browser_offline_check() == 1
    assert calls == [(["-m", "integration", *_BROWSER_TESTS], True, "browser-offline")]


def test_missing_kronos_browser_test_is_not_a_dashboard_only_pass(tmp_path, monkeypatch):
    runner = _runner()
    dashboard = tmp_path / _BROWSER_TESTS[0]
    dashboard.parent.mkdir(parents=True)
    dashboard.touch()
    monkeypatch.setattr(runner, "_REPO", tmp_path)

    def refuse(*_args, **_kwargs):
        pytest.fail("an incomplete browser selection must not run")

    monkeypatch.setattr(runner, "_run_pytest", refuse)
    assert runner._browser_offline_check() == 1


def test_required_browser_skips_cannot_pass(monkeypatch):
    runner = _runner()
    monkeypatch.setattr(
        runner, "_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="1 passed, 1 skipped in 0.10s\n",
            stderr="",
        ),
    )
    assert runner._run_pytest(
        ["-m", "integration", *_BROWSER_TESTS], reject_skips=True, label="browser-offline",
    ) == 1
