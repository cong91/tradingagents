"""Settings: GET/PUT /api/settings (docs/ui-api-contract.md §1).

Reads the engine's ``_ENV_OVERRIDES`` surface and masks every credential.
PUT applies in-memory via ``set_config`` (effective for runs started after
the change) and upserts ``TRADINGAGENTS_*`` lines into the repo ``.env``,
preserving comments and unrelated lines. The FR5/FR-S2/FR-D gate keys are
deliberately absent from ``_ENV_OVERRIDES`` (default_config.py:33-36) and are
therefore unwriteable here — one switch must never arm live trading.
"""

import os
from pathlib import Path

from fastapi import APIRouter, Request

from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.default_config import (
    _BOOL_TRUE,
    _ENV_OVERRIDES,
    DEFAULT_CONFIG,
    _coerce,
)

router = APIRouter(prefix="/api", tags=["settings"])

# Read-only path surface (default_config.py:97-99, 207) — never editable, only
# shown so the UI can point at where runs read and write.
_PATH_KEYS = {
    "results_dir": "TRADINGAGENTS_RESULTS_DIR",
    "data_cache_dir": "TRADINGAGENTS_CACHE_DIR",
    "memory_log_path": "TRADINGAGENTS_MEMORY_LOG_PATH",
    "exec_log_path": "TRADINGAGENTS_EXEC_LOG_PATH",
}

# Defaults that are None give _coerce nothing to infer a type from, so the
# JSON-facing types are pinned here (default_config.py:113-136, 188).
_NULL_DEFAULT_TYPES = {
    "backend_url": "str|null",
    "benchmark_ticker": "str|null",
    "google_thinking_level": "str|null",
    "openai_reasoning_effort": "str|null",
    "anthropic_effort": "str|null",
    "temperature": "float|null",
    "llm_max_retries": "int|null",
    "max_tokens": "int|null",
}

# The repo .env the CLI also reads (tradingagents/__init__.py loads it).
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

_ENV_BY_KEY = {key: env for env, key in _ENV_OVERRIDES.items()}


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name)
    return bool(raw) and raw.strip().lower() in _BOOL_TRUE


def effective_exec_mode() -> str:
    """``bridge.effective_mode()`` (bridge.py:480-486): live only when the
    ``exec_live`` config literal is True AND ``TRADINGAGENTS_EXEC_LIVE`` reads
    true at call time, fail-closed."""
    from tradingagents.execution.bridge import ExchangeBridge

    try:
        return ExchangeBridge(get_config()).effective_mode()
    except ValueError:
        # exec_exchange_id is empty: no venue to verify a gate against, stay closed.
        return "dry"


def _type_name(key: str, default) -> str:
    if default is None:
        return _NULL_DEFAULT_TYPES.get(key, "str|null")
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    return "str"


def _coerce_json(value, type_name: str):
    """Coerce one JSON value to the setting's declared type; raises ValueError."""
    if type_name.endswith("|null") and value is None:
        return None
    base = type_name.split("|")[0]
    if base == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return _coerce(value, True)
        raise ValueError(f"expected a boolean, got {value!r}")
    if base == "int":
        if isinstance(value, bool):
            raise ValueError(f"expected an integer, got {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return _coerce(value, 0)
        raise ValueError(f"expected an integer, got {value!r}")
    if base == "float":
        if isinstance(value, bool):
            raise ValueError(f"expected a number, got {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return _coerce(value, 0.0)
        raise ValueError(f"expected a number, got {value!r}")
    if isinstance(value, str):
        return value
    raise ValueError(f"expected a string, got {value!r}")


def _settings_row(key: str, env_var: str, editable: bool) -> dict:
    config = get_config()
    return {
        "key": key,
        "env_var": env_var,
        "value": config.get(key),
        "default": DEFAULT_CONFIG.get(key),
        "type": _type_name(key, DEFAULT_CONFIG.get(key)),
        "editable": editable,
        "source": "env" if os.environ.get(env_var) else "default",
    }


def _settings_payload() -> dict:
    from tradingagents.execution.bridge import credential_env_names

    rows = [_settings_row(key, env_var, editable=True) for key, env_var in _ENV_BY_KEY.items()]
    rows.extend(_settings_row(key, env_var, editable=False) for key, env_var in _PATH_KEYS.items())
    rows.sort(key=lambda row: row["key"])

    exchange_id = str(get_config().get("exec_exchange_id", "binance"))
    credential_vars = ["OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"]
    credential_vars.extend(credential_env_names(exchange_id))
    credentials = []
    for env_var in credential_vars:
        raw = os.environ.get(env_var)
        configured = bool(raw)
        credentials.append({
            "env_var": env_var,
            "configured": configured,
            "masked": f"••••{raw[-4:]}" if configured else None,
        })

    return {
        "generated_at": utc_now_iso(),
        "settings": rows,
        "credentials": credentials,
        "gates_readonly": {
            "exec_live_config": get_config().get("exec_live") is True,
            "TRADINGAGENTS_EXEC_LIVE": _env_flag("TRADINGAGENTS_EXEC_LIVE"),
            "exec_auto_confirm_config": get_config().get("exec_auto_confirm") is True,
            "TRADINGAGENTS_EXEC_AUTO_CONFIRM": _env_flag("TRADINGAGENTS_EXEC_AUTO_CONFIRM"),
            "exec_derivatives_config": get_config().get("exec_derivatives") is True,
            "TRADINGAGENTS_EXEC_DERIVATIVES": _env_flag("TRADINGAGENTS_EXEC_DERIVATIVES"),
        },
        "effective_exec_mode": effective_exec_mode(),
    }


def _render_env_value(value) -> str:
    if value is None:
        return ""  # KEY= reads back as unset: _apply_env_overrides skips empty values
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _persist_env(updates: dict) -> None:
    """Upsert ``TRADINGAGENTS_*`` lines into .env, keeping comments and other
    lines intact; tmp-file + replace so a crash cannot truncate the file."""
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    for env_var, value in updates.items():
        rendered = f"{env_var}={_render_env_value(value)}"
        for index, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == env_var:
                lines[index] = rendered
                break
        else:
            lines.append(rendered)
    tmp_path = ENV_PATH.with_name(ENV_PATH.name + ".tmp")
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp_path.replace(ENV_PATH)


@router.get("/settings")
def get_settings():
    return _settings_payload()


@router.put("/settings")
async def put_settings(request: Request):
    from server import audit as audit_log

    body = await json_body(request)
    settings_in = body.get("settings")
    if not isinstance(settings_in, dict) or not settings_in:
        raise ApiError(400, "validation_error", 'body must be {"settings": {key: value, ...}}')

    validated: dict[str, object] = {}
    for key, value in settings_in.items():
        env_var = _ENV_BY_KEY.get(key)
        if env_var is None:
            raise ApiError(403, "forbidden_key", f"setting {key!r} is read-only or unknown", {"key": key})
        try:
            validated[key] = _coerce_json(value, _type_name(key, DEFAULT_CONFIG.get(key)))
        except (TypeError, ValueError) as exc:
            raise ApiError(
                422, "coercion_error", str(exc), {"env_var": env_var, "message": str(exc)}
            ) from exc

    set_config(validated)  # in-memory: effective for runs created after this call
    _persist_env({_ENV_BY_KEY[key]: value for key, value in validated.items()})
    audit_log.record("settings_updated", keys=sorted(validated))
    payload = _settings_payload()
    payload["applied_at"] = utc_now_iso()
    return payload
