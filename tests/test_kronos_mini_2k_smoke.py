"""Regression tests and pytest wrapper for the offline mini/2k smoke."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.kronos_mini_2k_smoke import MissingArtifacts, run_smoke


def _smoke_environment(tmp_path):
    missing = tmp_path / "missing"
    return {
        **os.environ,
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "TRADINGAGENTS_KRONOS_SOURCE": str(missing),
        "TRADINGAGENTS_KRONOS_MODELS": str(missing),
        "TRADINGAGENTS_KRONOS_PYTHON": str(missing),
    }


@pytest.mark.unit
def test_standalone_entrypoint_does_not_require_pytest(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    script = """\
import runpy
import sys
sys.modules['pytest'] = None
runpy.run_module('tests.kronos_mini_2k_smoke', run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=repo, env=_smoke_environment(tmp_path),
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 2
    assert "missing" in result.stderr


@pytest.mark.unit
def test_pytest_wrapper_skips_missing_artifacts(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--noconftest", "-q", "-m", "integration",
         "tests/test_kronos_mini_2k_smoke.py::test_mini_2k_sampler_runs_2048_closed_candles"],
        cwd=repo, env=_smoke_environment(tmp_path), capture_output=True,
        text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout


@pytest.mark.unit
def test_pytest_wrapper_fails_for_corrupt_artifact(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = _smoke_environment(tmp_path)
    source = tmp_path / "source"
    corrupt = source / "model" / "kronos.py"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("corrupt", encoding="utf-8")
    env["TRADINGAGENTS_KRONOS_SOURCE"] = str(source)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--noconftest", "-q", "-m", "integration",
         "tests/test_kronos_mini_2k_smoke.py::test_mini_2k_sampler_runs_2048_closed_candles"],
        cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "hash mismatch" in result.stdout


@pytest.mark.unit
def test_pytest_wrapper_skips_missing_interpreter(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = _smoke_environment(tmp_path)
    script = """\
import pytest
from tests import kronos_mini_2k_smoke as smoke
smoke.verify_artifacts = lambda *args: (None, {})
raise SystemExit(pytest.main([
    '--noconftest', '-q', '-m', 'integration',
    'tests/test_kronos_mini_2k_smoke.py::test_mini_2k_sampler_runs_2048_closed_candles',
]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=repo, env=env,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout
    assert "interpreter" in result.stdout


@pytest.mark.smoke
@pytest.mark.integration
def test_mini_2k_sampler_runs_2048_closed_candles():
    try:
        run_smoke()
    except MissingArtifacts as exc:
        pytest.skip(str(exc))
