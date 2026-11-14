"""Filesystem isolation for experiment runs (plan §4.1).

One concern: point every home-directory-bound path an experiment may touch —
``results_dir``, ``data_cache_dir``, ``memory_log_path`` — inside the
experiment's own sandbox directory, so a run can neither read the operator's
memory log nor write decisions into it. The pattern follows
``tests/conftest.py`` (``_own_file_locations``), applied to a run instead of
the suite. The real memory log is never opened by an experiment: the isolation
config is produced here and the runner passes it to the simulation, which
takes an explicit ``audit_log_path`` rather than the runtime's default.
"""

from __future__ import annotations

import os
from pathlib import Path

# The runtime env vars the conftest pattern reroutes; an experiment sandbox
# keeps every one of them inside its own directory.
_SANDBOXED_VARS = (
    "TRADINGAGENTS_RESULTS_DIR",
    "TRADINGAGENTS_CACHE_DIR",
    "TRADINGAGENTS_MEMORY_LOG_PATH",
    "TRADINGAGENTS_EXEC_LOG_PATH",
)


class IsolationSandbox:
    """One run's private directory tree with rerouted config paths."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.results_dir = self.root / "results"
        self.cache_dir = self.root / "cache"
        self.memory_log_path = self.root / "memory" / "trading_memory.md"
        self.exec_log_path = self.root / "execution" / "audit.jsonl"

    def prepare(self) -> None:
        for directory in (self.results_dir, self.cache_dir, self.memory_log_path.parent,
                          self.exec_log_path.parent):
            directory.mkdir(parents=True, exist_ok=True)

    def config_overlay(self, base: dict) -> dict:
        """``base`` config with every file-bound key pointed into the sandbox."""
        overlay = dict(base)
        overlay["results_dir"] = str(self.results_dir)
        overlay["data_cache_dir"] = str(self.cache_dir)
        overlay["memory_log_path"] = str(self.memory_log_path)
        overlay["exec_log_path"] = str(self.exec_log_path)
        return overlay

    def assert_sandboxed(self, config: dict) -> None:
        """Fail loudly if any file-bound key escapes the sandbox root."""
        for key in ("results_dir", "data_cache_dir", "memory_log_path", "exec_log_path"):
            value = config.get(key)
            if value is None:
                continue
            path = Path(value).resolve()
            root = self.root.resolve()
            if not path.is_relative_to(root):
                raise RuntimeError(
                    f"experiment config key {key!r} points outside the sandbox "
                    f"({path} not under {root}); operator state must stay untouched"
                )

    def apply_env(self) -> dict[str, str]:
        """Env overrides (a copy) rerouting the sandboxed vars into the sandbox.

        Returns the previous values so ``restore_env`` can put them back; used
        for subprocess-based checks that must prove the environment is clean.
        """
        saved: dict[str, str] = {}
        targets = {
            "TRADINGAGENTS_RESULTS_DIR": str(self.results_dir),
            "TRADINGAGENTS_CACHE_DIR": str(self.cache_dir),
            "TRADINGAGENTS_MEMORY_LOG_PATH": str(self.memory_log_path),
            "TRADINGAGENTS_EXEC_LOG_PATH": str(self.exec_log_path),
        }
        for var in _SANDBOXED_VARS:
            saved[var] = os.environ.get(var, "")
        os.environ.update(targets)
        return saved

    def restore_env(self, saved: dict[str, str]) -> None:
        for var, previous in saved.items():
            if previous == "":
                os.environ.pop(var, None)
            else:
                os.environ[var] = previous


__all__ = ["IsolationSandbox", "_SANDBOXED_VARS"]
