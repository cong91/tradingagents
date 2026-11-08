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
from tradingagents.llm_clients.factory import tier_provider
from tradingagents.llm_clients.provider_metadata import get_provider
from tradingagents.llm_clients.runtime_credentials import resolve_credential

router = APIRouter(prefix="/api", tags=["health"])

_LLM_KEY_VARS = {"openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def _llm_key_status(config: dict) -> tuple[dict[str, bool], bool]:
    """Report active tier credential readiness without exposing key values."""
    statuses: dict[str, bool] = {}
    ready = True
    for tier in ("quick", "deep"):
        provider = tier_provider(config, tier)
        credential = resolve_credential(provider, tier)
        configured = credential.value is not None
        statuses[f"{tier}:{provider}"] = configured
        metadata = get_provider(provider)
        if metadata and metadata["credential_required"] and not configured:
            ready = False
    # Retain the legacy provider booleans for existing health consumers.
    statuses.update({name: bool(os.environ.get(var)) for name, var in _LLM_KEY_VARS.items()})
    return statuses, ready


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

    keys, llm_ready = _llm_key_status(config)

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
