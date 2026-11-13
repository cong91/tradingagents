"""Test runner for the trading research delivery plan.

Plan: docs/plans/trading-research-delivery-20261110.md

Usage:
    .venv/Scripts/python.exe scripts/verify_research_delivery.py --stage ledger|experiments|dashboard|kronos
    .venv/Scripts/python.exe scripts/verify_research_delivery.py --full

Guarantees required by the plan:
- The disposable PostgreSQL service comes from tests/postgres-compose.yml under a
  random Compose project name of its own (ta-verify-<hex>); it never touches the
  ``ta-migration-test-*`` project that tests/test_postgres_migrations_integration.py
  starts for itself.
- ``TRADINGAGENTS_TEST_DATABASE_URL`` is injected into the pytest child environment
  only. This runner never reads ``TRADINGAGENTS_DATABASE_URL`` (the runtime DSN); it
  only removes that variable from the child environment so a misbehaving test cannot
  reach the deployment database.
- Cleanup removes only the Compose project and build output this process created.

Exit codes: 0 = requested parts passed, 1 = a part failed (it ran and returned a
failure) or a required prerequisite (Docker engine, web/node_modules, the pinned
Kronos environment) is unavailable, 2 = the requested stage has no implemented
test files yet. Summary buckets are PASS (exit 0), SKIPPED (exit 2: a conditional
part that has not been implemented yet), FAIL (anything else, including a
conditional part that ran and failed). A part that ran and failed can never land
in SKIPPED, and a full run with any FAIL exits 1: no fake pass.

The active Kronos stage and ``--full`` require the real pinned model: an absent
isolated interpreter or weight file fails the run with the concrete reason rather
than skipping the smoke, because a green Kronos stage over a model that never ran
is the exact fake pass this runner exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_COMPOSE_FILE = _REPO / "tests" / "postgres-compose.yml"
_SERVICE = "postgres-test"
_PROJECT_PREFIX = "ta-verify-"
_WEB_BUILD_DIR = ".next-verify"
_COMPOSE_UP_TIMEOUT = 180.0
_COMPOSE_DOWN_TIMEOUT = 60.0
_PORT_TIMEOUT = 30.0
_READINESS_SECONDS = 30.0

# Stage -> pytest files. A stage is "not implemented" until at least one of its
# files exists; the runner then refuses to fake a pass (exit 2).
_STAGES: dict[str, list[str]] = {
    "ledger": [
        "tests/test_outcome_attribution.py",
        "tests/test_outcome_currency.py",
        "tests/test_outcome_netting.py",
        "tests/test_outcome_ledger_postgres.py",
        "tests/test_outcome_ledger_worker.py",
        "tests/test_outcomes_api.py",
        "tests/test_futures_simulation.py",
    ],
    "experiments": [
        "tests/test_experiments_walkforward.py",
        "tests/test_experiments_runner.py",
        "tests/test_experiments_isolation.py",
        "tests/test_experiments_baselines.py",
        "tests/test_experiments_registry.py",
        "tests/test_experiments_api.py",
        "tests/test_financial_experiment_cli.py",
    ],
    "dashboard": [
        "tests/test_operating_brief.py",
        # The server-render acceptance for the positions owner (Portfolio card):
        # precision, signed quantities, observed_at, incomplete reads, unknown
        # currencies and stale snapshots moved here when the duplicated
        # performance/positions block was removed (the assertions moved WITH it,
        # none were deleted).
        "tests/test_performance_ui_render.py",
        "tests/test_settings_field_accessibility.py",
        "tests/test_research_delivery_verification.py",
    ],
    "kronos": [
        "tests/test_forecast_inputs.py",
        "tests/test_forecast_cache.py",
        "tests/test_forecast_sampler.py",
        "tests/test_forecast_context.py",
        "tests/test_kronos_decision_integration.py",
        "tests/test_kronos_decision_settings.py",
        "tests/test_financial_experiment_cli.py",
        "tests/test_experiments_registry.py",
    ],
}

# Stages whose mapped tests reach PostgreSQL through the conftest postgres_schema
# fixture and therefore need the disposable service. The dashboard brief tests
# drive the ASGI app against the ledger/cycle stores, so they need it too — and
# without the DSN postgres_schema SKIPS, which would let the stage go green while
# nothing ran.
_STAGE_NEEDS_PG = {"ledger": True, "experiments": True, "dashboard": True}

_WEB_DIR = _REPO / "web"


class _Failure(Exception):
    """A required prerequisite is unavailable; message is shown, exit is 1."""


def _python() -> str:
    """The repo venv interpreter; falls back to the running interpreter."""
    venv = _REPO / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv) if venv.exists() else sys.executable


def _node(relative_bin: str) -> list[str]:
    """A node_modules binary invoked through node itself (no npm shims needed)."""
    return ["node", str(_WEB_DIR / relative_bin)]


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
         timeout: float | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(command)}" + (f"  (cwd={cwd})" if cwd else ""))
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        timeout=timeout,
        check=False,
        text=bool(capture),
        capture_output=capture,
    )


def _docker_available() -> bool:
    try:
        result = _run(["docker", "info", "--format", "{{.ServerVersion}}"],
                      capture=True, timeout=10.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _compose(command_tail: list[str], project: str, *, timeout: float,
             capture: bool = False) -> subprocess.CompletedProcess[str]:
    return _run(
        ["docker", "compose", "-p", project, "-f", str(_COMPOSE_FILE), *command_tail],
        cwd=_REPO, timeout=timeout, capture=capture,
    )


def _start_test_postgres(project: str) -> str:
    """Start the disposable service and return the DSN for the pytest child."""
    result = _compose(["up", "-d", "--wait"], project, timeout=_COMPOSE_UP_TIMEOUT)
    if result.returncode != 0:
        raise _Failure("docker compose up --wait failed for the disposable test PostgreSQL")
    port_result = _compose(["port", _SERVICE, "5432"], project, timeout=_PORT_TIMEOUT, capture=True)
    if port_result.returncode != 0:
        raise _Failure("docker compose port failed for the disposable test PostgreSQL")
    address, port = port_result.stdout.strip().rsplit(":", 1)
    host = address.strip("[]")
    return f"postgresql://migration:migration@{host}:{port}/migration_test"


def _wait_ready(dsn: str) -> None:
    deadline = time.monotonic() + _READINESS_SECONDS
    while True:
        try:
            import psycopg

            with psycopg.connect(dsn, connect_timeout=2):
                return
        except ImportError as exc:
            raise _Failure("psycopg is not installed; the disposable PostgreSQL is unusable") from exc
        except Exception as exc:  # noqa: BLE001 - readiness probe: retry until the deadline
            if time.monotonic() >= deadline:
                raise _Failure(f"disposable test PostgreSQL never became ready: {exc}") from exc
            time.sleep(0.25)


def _child_env(dsn: str) -> dict[str, str]:
    """Test child environment: TEST DSN in, runtime DSN out."""
    env = os.environ.copy()
    # Never read the runtime DSN; just make sure the child cannot see it.
    env.pop("TRADINGAGENTS_DATABASE_URL", None)
    env["TRADINGAGENTS_TEST_DATABASE_URL"] = dsn
    return env


class _TestPostgres:
    """Owns exactly one disposable Compose project; cleanup never runs twice."""

    def __init__(self) -> None:
        self.project = _PROJECT_PREFIX + secrets.token_hex(4)
        self._up = False
        self.dsn: str | None = None

    def __enter__(self) -> _TestPostgres:
        if not _docker_available():
            raise _Failure("Docker engine is unavailable; PostgreSQL-backed tests cannot run")
        print(f"[pg] starting disposable Compose project {self.project}")
        self.dsn = _start_test_postgres(self.project)
        assert self.dsn is not None
        _wait_ready(self.dsn)
        self._up = True
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._up:
            print(f"[pg] removing disposable Compose project {self.project}")
            _compose(["down", "--volumes"], self.project, timeout=_COMPOSE_DOWN_TIMEOUT)
            self._up = False


def _pytest_files(relative: list[str]) -> tuple[list[str], list[str]]:
    present = [rel for rel in relative if (_REPO / rel).exists()]
    missing = [rel for rel in relative if rel not in present]
    return present, missing


# The pytest summary line of the most recent captured run (label -> summary).
# Stage tables print it so a green verdict always shows HOW MANY tests actually
# ran — a stage must never look green over an uncounted run.
_PYTEST_SUMMARIES: dict[str, str] = {}


def _run_pytest(args: list[str], env: dict[str, str] | None = None,
                reject_skips: bool = False, label: str | None = None) -> int:
    """Run pytest; with ``reject_skips`` a reported skip is a FAILURE.

    Two gates need this. The browser check: the test declines to run when Chrome
    is absent, and the brief suite: ``postgres_schema`` SKIPS when no test DSN
    was injected. pytest exits 0 for a fully-skipped selection either way, so
    without this the stage printed PASS over nothing executed — a fake green,
    the exact failure mode this runner exists to prevent.
    """
    command = [_python(), "-m", "pytest", "-q", *args]
    if not reject_skips:
        _PYTEST_SUMMARIES[label or " ".join(args[:1])] = "(uncaptured; streamed above)"
        return _run(command, cwd=_REPO, env=env).returncode
    result = _run(command + ["-rs"], cwd=_REPO, env=env, capture=True)
    tail = (result.stdout or "") + (result.stderr or "")
    # Print the COMPLETE captured output, never a truncated tail: a failure in
    # the middle must stay visible, not be swallowed by a warning tail.
    print(tail.rstrip())
    if result.returncode != 0:
        _PYTEST_SUMMARIES[label or " ".join(args[:1])] = "(FAILED — see the full output above)"
        return result.returncode
    summary = tail.strip().splitlines()[-1] if tail.strip() else ""
    _PYTEST_SUMMARIES[label or " ".join(args[:1])] = summary.strip() or "(no summary line)"
    if "skipped" in summary or "no tests ran" in summary or "passed" not in summary:
        print(f"[pytest] FAIL: the selection did not actually run ({summary.strip()!r}); "
              "a gate may not pass over zero executed tests")
        return 1
    print(f"[pytest] {summary.strip()}")
    return 0


def _summary_of(label: str) -> str:
    summary = _PYTEST_SUMMARIES.get(label)
    return f" ({summary})" if summary else ""


def _not_implemented(stage: str, missing: list[str], present: list[str]) -> int:
    print(f"[stage:{stage}] not implemented yet (exit 2).")
    print(f"  missing test files: {', '.join(missing)}")
    if present:
        print(f"  already present: {', '.join(present)}")
    print("  Implement the stage per docs/plans/trading-research-delivery-20261110.md, then rerun.")
    return 2


def run_stage(stage: str) -> int:
    """Run one delivery stage end to end."""
    relative = _STAGES[stage]
    present, missing = _pytest_files(relative)
    print(f"[stage:{stage}] mapped files: {len(present)} present, {len(missing)} missing")
    if stage == "dashboard":
        return _run_dashboard_stage(present, missing)
    if stage == "kronos":
        return _run_kronos_stage(present, missing)
    if not present:
        return _not_implemented(stage, missing, present)

    env: dict[str, str] | None = None
    if _STAGE_NEEDS_PG[stage]:
        with _TestPostgres() as postgres:
            assert postgres.dsn is not None
            env = _child_env(postgres.dsn)
            print(f"[stage:{stage}] pytest with disposable PostgreSQL ({len(present)} file(s))")
            return _run_pytest(present, env=env)
    print(f"[stage:{stage}] pytest without PostgreSQL ({len(present)} file(s))")
    return _run_pytest(present)


def _run_kronos_stage(present: list[str], missing: list[str]) -> int:
    """Active-Kronos gate: the suite with PG, plus the REAL pinned model smoke.

    The smoke part is not a skip. An absent isolated interpreter or weight file
    fails the stage with the concrete reason, because a green Kronos stage over
    a model that never ran would fake exactly the property this stage exists to
    prove — that the decision path's forecast block is drawn by the real pinned
    model.
    """
    results: list[tuple[str, int, str]] = []
    if not present:
        return _not_implemented("kronos", missing, present)
    print(f"[stage:kronos] suite with disposable PostgreSQL ({len(present)} file(s))")
    try:
        with _TestPostgres() as postgres:
            assert postgres.dsn is not None
            results.append(("kronos-suite", _run_pytest(
                present, env=_child_env(postgres.dsn), reject_skips=True,
                label="kronos-suite",
            ), _summary_of("kronos-suite")))
    except _Failure as exc:
        print(f"[pg] {exc}")
        results.append(("kronos-suite", 1, "(PostgreSQL unavailable)"))

    print("[stage:kronos] real local Kronos inference smoke (active path)")
    smoke_code = _kronos_active_inference_smoke()
    results.append(("kronos-model-smoke", smoke_code, _summary_of("kronos-model-smoke")))

    for name, code, summary in results:
        verdict = "PASS" if code == 0 else ("SKIPPED" if code == 2 else "FAIL")
        print(f"  {verdict:7s} {name}{summary}")
    if any(code not in (0, 2) for _, code, _ in results):
        print("stage kronos FAILED")
        if {n: c for n, c, _ in results}.get("kronos-model-smoke", 0) != 0:
            print("  the model smoke must run the real pinned model; an unavailable "
                  "environment is a failure here by design (see the reason above)")
        return 1
    if any(code == 2 for _, code, _ in results):
        print("stage kronos not implemented yet (exit 2)")
        return 2
    print("stage kronos OK")
    return 0


def _kronos_active_inference_smoke() -> int:
    """Real local inference on the ACTIVE path; a missing model is a FAILURE."""
    smoke = _REPO / "tests" / "test_kronos_active_smoke.py"
    if not smoke.exists():
        print("[kronos:smoke] not implemented yet (tests/test_kronos_active_smoke.py missing)")
        return 2
    return _run_pytest(
        ["-m", "smoke and integration", "tests/test_kronos_active_smoke.py"],
        reject_skips=True, label="kronos-model-smoke",
    )


def _run_dashboard_stage(present: list[str], missing: list[str]) -> int:
    """Dashboard gate: suite pytest + web trio + offline browser check together.

    The suite drives the ASGI app against the ledger/cycle stores, so it runs
    inside the disposable PostgreSQL and with ``reject_skips``: without the DSN
    every test skips and pytest still exits 0, which would print PASS over
    nothing executed.
    """
    results: list[tuple[str, int, str]] = []
    if present:
        print(f"[stage:dashboard] suite pytest with disposable PostgreSQL ({len(present)} file(s))")
        try:
            with _TestPostgres() as postgres:
                assert postgres.dsn is not None
                results.append(("suite-pytest", _run_pytest(
                    present, env=_child_env(postgres.dsn), reject_skips=True,
                    label="dashboard-suite",
                ), _summary_of("dashboard-suite")))
        except _Failure as exc:
            print(f"[pg] {exc}")
            results.append(("suite-pytest", 1, "(PostgreSQL unavailable)"))
    else:
        print(f"[stage:dashboard] dashboard pytest not implemented yet: {', '.join(missing)}")
        results.append(("suite-pytest", 2, ", ".join(missing)))
    # Build first: it generates .next-verify/types, which the typecheck needs
    # for the per-route globals Next declares (LayoutProps and friends).
    results.append(("web-build", _web_build(), ""))
    results.append(("web-typecheck", _web_typecheck(), ""))
    results.append(("web-lint", _web_lint(), ""))
    browser_code = _browser_offline_check()
    results.append(("browser-offline", browser_code, _summary_of("browser-offline")))
    # The browser check serves the build output, so the cleanup happens only
    # once every part that reads .next-verify has finished.
    _cleanup_web_build()
    for name, code, summary in results:
        verdict = "PASS" if code == 0 else ("SKIPPED" if code == 2 else "FAIL")
        print(f"  {verdict:7s} {name}{summary}")
    if any(code not in (0, 2) for _, code, _ in results):
        print("stage dashboard FAILED")
        return 1
    if any(code == 2 for _, code, _ in results):
        print("stage dashboard not implemented yet (exit 2)")
        return 2
    print("stage dashboard OK")
    return 0


def _web_typecheck() -> int:
    """Typecheck the app with Next's generated route types in scope.

    ``app/layout.tsx`` types its props with the global ``LayoutProps`` that Next
    generates per route. That global only exists after a build has written
    ``.next-verify/types``, so a bare ``tsc --noEmit`` on a clean tree reports a
    false ``Cannot find name 'LayoutProps'`` and proves nothing. This writes a
    throwaway tsconfig that extends the app's own and adds that directory to the
    program, then typechecks with it — the same arrangement Next's own build
    uses, without touching the tracked ``web/tsconfig.json``.
    """
    tsc = _WEB_DIR / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.exists():
        print("[web:typecheck] FAIL prerequisite: node_modules/typescript missing (npm install in web/)")
        return 1
    generated = _WEB_DIR / _WEB_BUILD_DIR / "types"
    if not (generated / "routes.d.ts").exists():
        print(f"[web:typecheck] FAIL: {_WEB_BUILD_DIR}/types/routes.d.ts is missing; "
              "the build part must run first or the per-route globals cannot resolve")
        return 1
    config = _WEB_DIR / f"tsconfig.{_WEB_BUILD_DIR.lstrip('.')}.json"
    config.write_text(json.dumps({
        "extends": "./tsconfig.json",
        "include": [
            "next-env.d.ts", "**/*.ts", "**/*.tsx", "**/*.mts",
            f"{_WEB_BUILD_DIR}/types/**/*.ts",
        ],
    }), encoding="utf-8")
    try:
        print(f"[web:typecheck] using generated route types from {_WEB_BUILD_DIR}/types")
        return _run(
            _node("node_modules/typescript/bin/tsc") + ["--noEmit", "-p", str(config)],
            cwd=_WEB_DIR,
        ).returncode
    finally:
        config.unlink(missing_ok=True)


def _web_lint() -> int:
    eslint = _WEB_DIR / "node_modules" / "eslint" / "bin" / "eslint.js"
    if not eslint.exists():
        print("[web:lint] FAIL prerequisite: node_modules/eslint missing (npm install in web/)")
        return 1
    return _run(_node("node_modules/eslint/bin/eslint.js") + ["."], cwd=_WEB_DIR).returncode


def _next_dist_dir_wired() -> bool:
    config = (_WEB_DIR / "next.config.ts").read_text(encoding="utf-8")
    return "NEXT_DIST_DIR" in config


def _web_build() -> int:
    """Production build into .next-verify so a running dev server keeps .next.

    Next rewrites ``web/tsconfig.json`` during the build to add this output's
    type paths. That rewrite is tool churn, not part of the change under test,
    so the file is restored afterwards; its generated types stay in `.next-verify`
    until the stage is done, because the typecheck part reads them.
    """
    if not _next_dist_dir_wired():
        print("[web:build] not wired yet: web/next.config.ts does not honor NEXT_DIST_DIR")
        print("           (dashboard stage adds distDir = process.env.NEXT_DIST_DIR || '.next')")
        return 2
    tsconfig = _WEB_DIR / "tsconfig.json"
    tsconfig_before = tsconfig.read_bytes() if tsconfig.exists() else None
    env = os.environ.copy()
    env["NEXT_DIST_DIR"] = _WEB_BUILD_DIR
    try:
        return _run(_node("node_modules/next/dist/bin/next") + ["build"], cwd=_WEB_DIR, env=env).returncode
    finally:
        if tsconfig_before is not None and tsconfig.read_bytes() != tsconfig_before:
            tsconfig.write_bytes(tsconfig_before)
            print("[web:build] restored web/tsconfig.json (the build had rewritten it)")


def _cleanup_web_build() -> None:
    """Remove the verify build output the stage created (never a pre-existing one)."""
    build_dir = _WEB_DIR / _WEB_BUILD_DIR
    if build_dir.exists():
        print(f"[web:build] removing build output {_WEB_BUILD_DIR}")
        shutil.rmtree(build_dir, ignore_errors=True)


def _web_parts() -> dict[str, int]:
    """Web typecheck/lint/build results. Missing node_modules is a prerequisite
    failure (exit 1), not a skip: a full run must not go green without web."""
    results: dict[str, int] = {}
    if not (_WEB_DIR / "node_modules").exists():
        for name in ("typecheck", "lint", "build"):
            print(f"[web:{name}] FAIL prerequisite: web/node_modules missing (npm install in web/)")
            results[name] = 1
        return results
    # Build before typecheck: the generated .next-verify/types are what make the
    # per-route globals (LayoutProps) resolvable. Cleanup happens in run_full
    # after the browser check, which serves this same output.
    results["build"] = _web_build()
    results["typecheck"] = _web_typecheck()
    results["lint"] = _web_lint()
    return results


def _browser_offline_check() -> int:
    """Run the isolated dashboard and Kronos Settings browser acceptance.

    Both selections need Chrome and local ports, so integration is selected
    explicitly. Missing or skipped required scenarios cannot produce a pass.
    """
    browser_tests = [
        "tests/browser/test_dashboard_brief_offline.py",
        "tests/test_operator_settings_kronos_browser.py",
    ]
    missing = [path for path in browser_tests if not (_REPO / path).is_file()]
    if missing:
        print(f"[browser] FAIL: required acceptance tests are missing: {', '.join(missing)}")
        return 1
    if not _next_dist_dir_wired():
        print("[browser] not ready: NEXT_DIST_DIR is not wired into web/next.config.ts")
        return 2
    return _run_pytest(
        ["-m", "integration", *browser_tests],
        reject_skips=True, label="browser-offline",
    )


def run_full() -> int:
    """Everything: ruff, smoke imports, pytest with PG, migration integration, web."""
    failed: list[str] = []
    skipped: list[str] = []
    passed: list[str] = []

    def note(name: str, code: int) -> None:
        # 0 = pass, 2 = not implemented (skip, loud), anything else = FAIL.
        # A part that ran and failed can never land in skipped: that would fake
        # a green full run.
        if code == 0:
            passed.append(name)
        elif code == 2:
            skipped.append(name)
        else:
            failed.append(name)

    print("=== full: ruff ===")
    note("ruff", _run([_python(), "-m", "ruff", "check", "."], cwd=_REPO).returncode)

    print("=== full: clean-import smoke ===")
    note("imports", _run([_python(), "-c", "import tradingagents, cli.main"], cwd=_REPO).returncode)

    print("=== full: pytest with disposable PostgreSQL ===")
    try:
        with _TestPostgres() as postgres:
            assert postgres.dsn is not None
            note("pytest", _run_pytest([], env=_child_env(postgres.dsn)))
    except _Failure as exc:
        print(f"[pg] {exc}")
        failed.append("pytest")

    print("=== full: migration integration tests (own Compose project) ===")
    if _docker_available():
        migration_tests = _REPO / "tests" / "test_postgres_migrations_integration.py"
        if migration_tests.exists():
            note("migration-integration", _run_pytest(["-m", "integration", "tests/test_postgres_migrations_integration.py"]))
        else:
            skipped.append("migration-integration (file missing)")
    else:
        print("[migration-integration] not run: Docker engine is unavailable")
        failed.append("migration-integration")

    print("=== full: web typecheck / lint / build ===")
    for name, code in _web_parts().items():
        note(f"web-{name}", code)

    print("=== full: dashboard offline browser fixture check ===")
    note("browser-offline", _browser_offline_check())
    _cleanup_web_build()

    print("=== full: kronos active inference smoke ===")
    note("kronos-smoke", _kronos_active_inference_smoke())

    print("\n=== summary ===")
    for name in passed:
        print(f"  PASS    {name}")
    for name in skipped:
        print(f"  SKIPPED {name}")
    for name in failed:
        print(f"  FAIL    {name}")
    if failed:
        print("full run FAILED")
        return 1
    print("full run OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Test runner for docs/plans/trading-research-delivery-20261110.md",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stage", choices=sorted(_STAGES), help="run one delivery stage")
    group.add_argument("--full", action="store_true",
                       help="ruff + smoke imports + pytest with PG + migration integration + web trio + conditional browser/Kronos checks")
    args = parser.parse_args(argv)

    os.chdir(_REPO)
    if args.full:
        return run_full()
    return run_stage(str(args.stage))


if __name__ == "__main__":
    sys.exit(main())
