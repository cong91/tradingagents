"""Settings: GET/PUT /api/settings (docs/ui-api-contract.md §1).

Reads the engine's ``_ENV_OVERRIDES`` surface and masks every credential.
PUT applies in-memory via ``set_config`` (effective for runs started after
the change) and persists to PostgreSQL (``control_engine_settings``,
applied again at boot between the env layer and the operator overrides).
The FR5/FR-S2/FR-D gate keys are deliberately absent from ``_ENV_OVERRIDES``
(default_config.py:33-36) and are therefore unwriteable here — one switch
must never arm live trading. ``.env`` is deployment-only configuration and
is never written at runtime.
"""

import os

from fastapi import APIRouter, Request

from server import auto_modes, control_store
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.default_config import (
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
    "llm_timeout": "float",
    "llm_max_retries": "int|null",
    "max_tokens": "int|null",
}

# The deployment .env the CLI also reads (tradingagents/__init__.py loads it)
# is deployment-only configuration: read at boot, never written at runtime.

_ENV_BY_KEY = {key: env for env, key in _ENV_OVERRIDES.items()}
_READONLY_ENV_KEYS = {"exec_base_url"}


def effective_exec_mode() -> str:
    """Return the current config-controlled execution mode."""
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
    from tradingagents.execution.exchange_client import credential_env_names

    rows = [
        _settings_row(key, env_var, editable=key not in _READONLY_ENV_KEYS)
        for key, env_var in _ENV_BY_KEY.items()
    ]
    rows.extend(_settings_row(key, env_var, editable=False) for key, env_var in _PATH_KEYS.items())
    rows.sort(key=lambda row: row["key"])

    config = get_config()
    modes = auto_modes.mode_config(config)
    exchange_id = str(config.get("exec_exchange_id", "binance"))
    credential_vars = ["OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"]
    credential_vars.extend(credential_env_names(exchange_id, config.get("exec_base_url")))
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
            "auto_spot": modes["spot"],
            "auto_futures": modes["futures"],
            "any_auto_enabled": any(modes.values()),
        },
        "effective_exec_mode": effective_exec_mode(),
    }


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
        if env_var is None or key in _READONLY_ENV_KEYS:
            raise ApiError(403, "forbidden_key", f"setting {key!r} is read-only or unknown", {"key": key})
        try:
            validated[key] = _coerce_json(value, _type_name(key, DEFAULT_CONFIG.get(key)))
        except (TypeError, ValueError) as exc:
            raise ApiError(
                422, "coercion_error", str(exc), {"env_var": env_var, "message": str(exc)}
            ) from exc

    # Domain validation BEFORE anything is applied or persisted: a tuning value
    # the forecast core cannot run (a 3-path budget, an unknown mode, a horizon
    # that is not whole bars of its timeframe, a non-finite number) must be
    # refused per field here, not land half-applied and surface later as an
    # ``unavailable`` block during a live cycle. Nothing has been mutated yet,
    # so a refusal leaves both memory and PostgreSQL untouched.
    kronos_fields = [key for key in validated if key.startswith("kronos_active_")]
    if kronos_fields:
        from tradingagents.forecast.settings import tuning_problems

        problems = tuning_problems(validated, get_config())
        if problems:
            raise ApiError(
                422, "validation_error",
                "one or more Kronos tuning values cannot run: " + "; ".join(
                    f"{key}: {reason}" for key, reason in sorted(problems.items())
                ),
                {"fields": problems},
            )

    set_config(validated)  # in-memory: effective for runs created after this call
    # Durable in PostgreSQL (0006_engine_settings.sql), re-applied at boot
    # between the env layer and the operator overrides — .env is never written
    # at runtime.
    control_store.set_engine_settings(validated)
    audit_log.record("settings_updated", keys=sorted(validated), persisted="postgresql")
    payload = _settings_payload()
    payload["applied_at"] = utc_now_iso()
    return payload
