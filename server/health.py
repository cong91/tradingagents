"""Health: GET /api/health (docs/ui-api-contract.md §9).

One glance at gate state, audit readability, key configuration and what the
server is currently doing. Never returns a key value — only booleans.
"""

import os
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter

from server import approvals, audit as audit_log, runs
from server.contract import utc_now_iso
from server.settings import effective_exec_mode
from tradingagents.dataflows.config import get_config

router = APIRouter(prefix="/api", tags=["health"])

_LLM_KEY_VARS = {"openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def _audit_log_readable() -> bool:
    try:
        audit_log.read_events()
    except Exception:  # health must answer, not raise
        return False
    return True


@router.get("/health")
def get_health():
    config = get_config()
    try:
        halted, _ = audit_log.halt_state()
        audit_readable = True
    except Exception:  # an unreadable log is degraded, not a 500
        halted, audit_readable = False, False

    provider = str(config.get("llm_provider", "openai"))
    keys = {name: bool(os.environ.get(var)) for name, var in _LLM_KEY_VARS.items()}
    key_for_provider = keys.get(provider)
    llm_ready = key_for_provider is None or key_for_provider  # unknown providers don't degrade

    return {
        "status": "ok" if audit_readable and llm_ready else "degraded",
        "version": _package_version(),
        "generated_at": utc_now_iso(),
        "exec_mode": effective_exec_mode(),
        "halted_today": halted,
        "audit_log_readable": audit_readable,
        "results_dir": str(config.get("results_dir", "")),
        "active_run_id": runs.active_run_id(),
        "pending_approvals": approvals.pending_count(),
        "llm_key_configured": keys,
        "llm_wire_protocol": config.get("llm_wire_protocol"),
    }


def _package_version() -> str:
    try:
        return version("tradingagents")
    except PackageNotFoundError:
        return "unknown"
