"""Operator overrides: the deliberate config a human sets, stored in PostgreSQL.

This replaces ``server/data/operator_config.json``. The values live in
``control_operator_settings`` (``0003_operator_settings.sql``) so they survive a
restart and are visible/auditable instead of being a file only a shell can see.

Two rules shape this surface:

* **Explicit allowlist.** Only the keys in ``_ALLOWED`` may be written. They are
  the knobs that ``PUT /api/settings`` deliberately refuses (the config halves
  of the execution gates) plus a few operational limits an operator may want to
  force regardless of ``.env``. Everything else belongs to ``/api/settings``.
* **Gates stay a double switch.** This table holds the config half only; the
  environment halves (``TRADINGAGENTS_EXEC_LIVE``, ``_AUTO_CONFIRM``,
  ``_DERIVATIVES``) are still read fail-closed at execute time from the
  process environment. Arming a gate here therefore requires an explicit
  ``confirm`` flag plus a written reason, and every change records its old and
  new value in the server action trail.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from server import audit as audit_log, control_store
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.default_config import DEFAULT_CONFIG

router = APIRouter(prefix="/api", tags=["operator-config"])

# key -> (json type, is an execution gate, group, Vietnamese label, hint)
_ALLOWED: dict[str, dict] = {
    "exec_live": {
        "type": "bool",
        "gate": True,
        "group": "gates",
        "label": "Cổng LIVE — nửa cấu hình (exec_live)",
        "hint": (
            "Chỉ là MỘT nửa của cổng FR5. Lệnh thật vẫn cần nửa môi trường "
            "TRADINGAGENTS_EXEC_LIVE đọc lúc gửi lệnh. Tắt cổng này là đóng ngay."
        ),
    },
    "exec_auto_confirm": {
        "type": "bool",
        "gate": True,
        "group": "gates",
        "label": "Tự động xác nhận — nửa cấu hình (exec_auto_confirm)",
        "hint": (
            "Nửa cấu hình của cổng cho autoloop tự duyệt kế hoạch. Vẫn cần "
            "TRADINGAGENTS_EXEC_AUTO_CONFIRM trong môi trường."
        ),
    },
    "exec_derivatives": {
        "type": "bool",
        "gate": True,
        "group": "gates",
        "label": "Phái sinh — nửa cấu hình (exec_derivatives)",
        "hint": (
            "Nửa cấu hình của cổng futures. Vẫn cần "
            "TRADINGAGENTS_EXEC_DERIVATIVES; executor còn đòi confirm=True."
        ),
    },
    "risk_max_derivatives_leverage": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Đòn bẩy phái sinh tối đa",
        "hint": "Trần RiskGuard. Đề xuất của agent vượt trần bị từ chối, không bị hạ xuống.",
    },
    "risk_max_derivatives_exposure_pct": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Phơi nhiễm phái sinh tối đa (%)",
        "hint": "0 = chặn hoàn toàn giao dịch phái sinh.",
    },
    "risk_derivatives_scale_out_enabled": {
        "type": "bool",
        "gate": False,
        "group": "risk",
        "label": "Chốt lời nhiều tầng (scale-out)",
        "hint": "Bật: hai tầng chốt theo R + phần còn lại chạy trailing. Tắt: một TP duy nhất.",
    },
}

_GATE_KEYS = {key for key, meta in _ALLOWED.items() if meta["gate"]}


def _coerce(key: str, value):
    """Coerce one JSON value to the key's declared type; raises ValueError."""
    expected = _ALLOWED[key]["type"]
    if expected == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{key} expects a boolean, got {value!r}")
    if expected == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} expects a number, got {value!r}")
        return float(value)
    raise ValueError(f"{key} has an unsupported declared type {expected!r}")


def _row(key: str, stored: dict[str, dict]) -> dict:
    meta = _ALLOWED[key]
    override = stored.get(key)
    return {
        "key": key,
        "type": meta["type"],
        "group": meta["group"],
        "label": meta["label"],
        "hint": meta["hint"],
        "gate": meta["gate"],
        "value": get_config().get(key),
        "default": DEFAULT_CONFIG.get(key),
        "override": override is not None,
        "stored_value": override["value"] if override else None,
        "updated_at": override["updated_at"] if override else None,
        "updated_by": override["updated_by"] if override else None,
    }


def _payload() -> dict:
    stored = {row["key"]: row for row in control_store.load_operator_setting_rows()}
    rows = [_row(key, stored) for key in _ALLOWED]
    rows.sort(key=lambda item: (item["group"], item["key"]))

    from server.settings import effective_exec_mode

    config = get_config()
    return {
        "generated_at": utc_now_iso(),
        "settings": rows,
        "gates": {
            "exec_live_config": config.get("exec_live") is True,
            "exec_auto_confirm_config": config.get("exec_auto_confirm") is True,
            "exec_derivatives_config": config.get("exec_derivatives") is True,
        },
        "effective_exec_mode": effective_exec_mode(),
        "allowlist": sorted(_ALLOWED),
    }


@router.get("/operator-config")
def get_operator_config():
    return _payload()


@router.put("/operator-config")
async def put_operator_config(request: Request):
    body = await json_body(request)
    settings_in = body.get("settings")
    if not isinstance(settings_in, dict) or not settings_in:
        raise ApiError(400, "validation_error", 'body must be {"settings": {key: value, ...}}')

    unknown = sorted(key for key in settings_in if key not in _ALLOWED)
    if unknown:
        raise ApiError(
            403, "forbidden_key",
            f"operator overrides accept only {sorted(_ALLOWED)}; rejected {unknown}",
            {"rejected": unknown, "allowlist": sorted(_ALLOWED)},
        )

    validated: dict[str, object] = {}
    for key, value in settings_in.items():
        try:
            validated[key] = _coerce(key, value)
        except ValueError as exc:
            raise ApiError(422, "coercion_error", str(exc), {"key": key}) from exc

    # Arming an execution gate is the one write that can enable real orders:
    # it needs a deliberate second signal and a written reason, both audited.
    gate_changes = {key: value for key, value in validated.items() if key in _GATE_KEYS}
    reason = body.get("reason")
    if gate_changes:
        if body.get("confirm") is not True:
            raise ApiError(
                412, "gate_confirmation_required",
                "changing an execution gate requires confirm=true",
                {"keys": sorted(gate_changes)},
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ApiError(
                422, "reason_required",
                "changing an execution gate requires a non-empty reason",
                {"keys": sorted(gate_changes)},
            )

    actor = body.get("actor")
    actor = actor.strip()[:120] if isinstance(actor, str) and actor.strip() else None

    config = get_config()
    previous = {key: config.get(key) for key in validated}
    for key, value in validated.items():
        control_store.set_operator_setting(key, value, updated_by=actor)

    set_config(validated)  # in-memory: effective for runs created after this call

    audit_log.record(
        "operator_config_updated",
        keys=sorted(validated),
        changes={key: {"from": previous[key], "to": validated[key]} for key in validated},
        gate_changes=sorted(gate_changes),
        reason=reason if gate_changes else None,
        actor=actor,
    )

    payload = _payload()
    payload["applied_at"] = utc_now_iso()
    return payload


@router.delete("/operator-config/{key}")
async def delete_operator_config(key: str, request: Request):
    await json_body(request)  # CSRF rule §0: application/json required, body may be {}
    if key not in _ALLOWED:
        raise ApiError(403, "forbidden_key", f"{key!r} is not an operator override key")

    if not control_store.delete_operator_setting(key):
        raise ApiError(404, "not_found", f"{key!r} has no stored override")

    default = DEFAULT_CONFIG.get(key)
    set_config({key: default})  # restore the shipped default in this process

    audit_log.record(
        "operator_config_reset",
        key=key,
        restored_default=default,
        gate=key in _GATE_KEYS,
    )
    return _payload()
