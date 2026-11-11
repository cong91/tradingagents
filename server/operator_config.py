"""Operator overrides: deliberate config a human sets, stored in PostgreSQL.

This replaces ``server/data/operator_config.json``. The values live in
``control_operator_settings`` (``0003_operator_settings.sql``) so they survive a
restart and are visible/auditable instead of being a file only a shell can see.

Two rules shape this surface:

* **Explicit allowlist.** Only the market automation modes and operational risk
  limits in ``_ALLOWED`` may be written. Deployment/runtime safety gates such as
  ``exec_live`` and legacy execution switches are never user controls here.
* **Modes are deliberate runtime switches.** Enabling either mode requires an
  explicit ``confirm`` flag plus a written reason, and every change records its
  old and new value in the server action trail.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Request

from server import audit as audit_log, auto_modes, control_store, settings_events
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.default_config import DEFAULT_CONFIG

router = APIRouter(prefix="/api", tags=["operator-config"])

# key -> (json type, is an execution gate, group, Vietnamese label, hint)
_ALLOWED: dict[str, dict] = {
    "auto_spot_enabled": {
        "type": "bool",
        "gate": True,
        "group": "gates",
        "label": "Auto Spot",
        "hint": "Cho phép hệ thống tự sàng lọc, phân tích và tạo lệnh Spot mới.",
    },
    "auto_futures_enabled": {
        "type": "bool",
        "gate": True,
        "group": "gates",
        "label": "Auto Futures",
        "hint": (
            "Cho phép hệ thống tự sàng lọc, phân tích và tạo vị thế Futures mới. "
            "Tắt không dừng bảo vệ vị thế đang mở."
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
    "risk_max_derivatives_exposure_hard_pct": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Phơi nhiễm phái sinh tối đa khi vượt trần (%)",
        "hint": (
            "Trần cứng của cơ chế vượt trần: chỉ lệnh đạt đủ điều kiện "
            "(confirmation 1h+4h, R đủ cao, rủi ro chặt, funding thuận, còn "
            "ngân sách trong ngày) mới được đẩy tổng phơi nhiễm tới mức này. "
            "0 = tắt cơ chế vượt trần."
        ),
    },
    "risk_derivatives_override_min_r": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "R tối thiểu để vượt trần",
        "hint": "Lời mục tiêu / rủi ro stop tối thiểu của đề xuất để đủ điều kiện vượt trần.",
    },
    "risk_derivatives_override_max_risk_fraction": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Rủi ro tối đa mỗi lệnh khi vượt trần (phần equity)",
        "hint": (
            "Lệnh vượt trần phải rủi ro chặt hơn bình thường: 0.01 = tối đa "
            "1% equity nếu stop khớp."
        ),
    },
    "risk_derivatives_override_max_per_day": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Số lệnh vượt trần tối đa mỗi ngày (UTC)",
        "hint": (
            "0 = không bao giờ vượt trần. Mỗi lần dùng ghi một dòng "
            "derivatives_override_used vào audit."
        ),
    },
    "risk_max_open_heat_pct": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Tổng rủi ro đang mở tối đa — heat (%)",
        "hint": (
            "Tổng phần equity mất nếu MỌI stop khớp cùng lúc (mọi vị thế + "
            "lệnh mới). LƯU Ý khác các trần khác: 0 = TẮT (không chặn), "
            ">0 = bật. Khuyến nghị 6."
        ),
    },
    "risk_derivatives_scale_out_enabled": {
        "type": "bool",
        "gate": False,
        "group": "risk",
        "label": "Chốt lời nhiều tầng (scale-out)",
        "hint": "Bật: hai tầng chốt theo R + phần còn lại chạy trailing. Tắt: một TP duy nhất.",
    },
    "risk_derivatives_break_even_r": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Mốc hòa vốn (R)",
        "hint": (
            "Đạt mốc R này thì dời SL về hòa vốn. Mốc hòa vốn đặt cao hơn "
            "entry đúng bằng phí khứ hồi, nên thoát ở SL vẫn không lỗ sau phí. "
            "0 không hợp lệ: phải > 0 và nhỏ hơn mốc khóa lợi nhuận."
        ),
    },
    "risk_derivatives_lock_profit_r": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Mốc khóa lợi nhuận (R)",
        "hint": (
            "Đạt mốc R này thì SL khóa tối thiểu 1R. Phải lớn hơn mốc hòa vốn "
            "và nhỏ hơn mốc bắt đầu trailing; 0 không hợp lệ."
        ),
    },
    "risk_derivatives_trail_start_r": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Bắt đầu trailing (R)",
        "hint": (
            "Từ mốc R này SL bám theo mark, cách mark đúng khoảng trailing. "
            "Phải lớn hơn mốc khóa lợi nhuận; 0 không hợp lệ."
        ),
    },
    "risk_derivatives_trail_distance_r": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Khoảng trailing (R)",
        "hint": (
            "SL bám mark cách bao nhiêu R khi đã trailing. Càng nhỏ càng bám "
            "sát, dễ bị wick quét; 0 không hợp lệ, phải > 0."
        ),
    },
    "risk_derivatives_max_hold_hours": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Thời gian giữ tối đa (giờ)",
        "hint": (
            "Giữ quá số giờ này mà lợi nhuận chưa đạt mốc R tối thiểu thì "
            "time stop đóng vị thế và huỷ bracket. 0 = tắt time stop."
        ),
    },
    "risk_derivatives_time_stop_min_r": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Mốc R tối thiểu trước time stop (R)",
        "hint": (
            "Vị thế phải đạt ít nhất mốc R này trước hạn giữ tối đa, nếu không "
            "sẽ bị đóng khi hết hạn. 0 không hợp lệ: phải > 0."
        ),
    },
    "risk_derivatives_round_trip_fee_rate": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Phí khứ hồi (tỷ lệ)",
        "hint": (
            "Tổng phí vào + ra theo tỷ lệ giá trị danh nghĩa (0.001 = 0.1%). "
            "Dùng để nâng SL hòa vốn lên trên entry. 0 = không bù phí, hòa vốn "
            "đúng bằng entry."
        ),
    },
    "risk_derivatives_time_stop_postpone_minutes": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Hoãn time stop trước settlement (phút)",
        "hint": (
            "Khi time stop sắp đóng một vị thế đang NHẬN funding mà settlement "
            "kế tiếp còn trong khoảng phút này thì hoãn đóng tới sau settlement "
            "để thu funding (tối đa một kỳ, cửa sổ không được phủ quá chu kỳ "
            "funding), kỳ sau sẽ đóng lại. Vị thế đang TRẢ phí thì vẫn đóng "
            "ngay. 0 = tắt hoãn, đóng đúng hạn."
        ),
    },
    "risk_derivatives_time_stop_review_enabled": {
        "type": "bool",
        "gate": False,
        "group": "risk",
        "label": "Phân tích vị thế trước khi time stop",
        "hint": (
            "Bật: quá hạn giữ tối đa, hệ thống chạy một vòng phân tích vị thế "
            "(một call LLM) để quyết định GIỮ (gia hạn hạn mức, hẹn phân tích "
            "lại sau) hay ĐÓNG. Nếu vòng phân tích không chạy được, vị thế "
            "được nộp thêm 'grace' giờ trước khi đóng cơ học. Tắt: quá hạn là "
            "đóng cơ học như trước."
        ),
    },
    "risk_derivatives_time_stop_review_interval_hours": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Chu kỳ phân tích lại sau khi giữ (giờ)",
        "hint": (
            "Khi phân tích kết luận GIỮ, hết khoảng giờ này hệ thống phân tích "
            "lại vị thế một lần nữa. 0 hoặc giá trị lỗi = tắt phân tích, quá "
            "hạn đóng cơ học."
        ),
    },
    "risk_derivatives_time_stop_review_grace_hours": {
        "type": "float",
        "gate": False,
        "group": "risk",
        "label": "Grace khi phân tích lỗi (giờ)",
        "hint": (
            "Nếu vòng phân tích không chạy được (LLM lỗi, trả sai định dạng), "
            "vị thế được trì hoãn đóng thêm khoảng giờ này trước khi áp time "
            "stop cơ học. 0 = không grace, đóng ngay."
        ),
    },
    "kronos_active_enabled": {
        "type": "bool",
        # A deliberate operator switch: enabling it changes what an unattended
        # decision cycle reads and pays 20-200 s of model inference per
        # candidate, so the write needs confirm + reason + audit like the Auto
        # modes. It is NOT an order-arming gate: the confirmation machinery
        # (`_GATE_KEYS`) is what enforces the deliberate-write rule, while the
        # actual live-order arming lives in auto_modes.MODE_KEYS, which this key
        # is deliberately not part of.
        "gate": True,
        "group": "forecast",
        "label": "Kronos làm ngữ cảnh quyết định",
        "hint": (
            "Bật: mỗi ứng viên futures được dự báo bằng model Kronos đã ghim "
            "(nến 4h đóng, horizon 24h, 16 paths, T=0.5) và khối dự báo nén "
            "được đưa vào ngữ cảnh TRƯỚC khi Trader/Portfolio Manager quyết "
            "định. Đây là DỮ LIỆU, không phải lệnh và không phải veto: không "
            "ứng viên nào bị cắt, không nới risk/leverage, không thay đổi "
            "confirmation hay RiskGuard. Tắt (mặc định): không đọc gì, không "
            "chạy model, chu kỳ giữ nguyên như trước. Đây là switch vận hành "
            "nên cần confirm + lý do và được ghi audit. Lưu ý: model không "
            "công bố training cutoff và chưa có bằng chứng lợi nhuận."
        ),
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
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} expects a finite non-negative number, got {value!r}")
        return value
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
        "default": auto_modes.MODE_DEFAULTS.get(key, DEFAULT_CONFIG.get(key)),
        "override": override is not None,
        "stored_value": override["value"] if override else None,
        "updated_at": override["updated_at"] if override else None,
        "updated_by": override["updated_by"] if override else None,
    }


def _kronos_section() -> dict:
    """Read-only Kronos status for the settings screen.

    Everything here is derived from the same sources the decision path reads —
    the engine config's ``kronos_active_*`` keys and the pinned-artifact
    verification the forecast service runs before any child process — so the
    screen shows what a cycle would actually use, not a parallel claim. No
    model is started: ``from_config``-level artifact checks are file reads and
    hashes (memoized), nothing heavier.
    """
    from tradingagents.forecast.artifact import (
        MODEL_REPO,
        MODEL_REVISION,
        SOURCE_REVISION,
        isolated_model_dir,
        isolated_python,
        isolated_source_dir,
        verify_artifacts,
    )
    from tradingagents.forecast.settings import settings_from_config

    config = get_config()
    settings = settings_from_config(config)
    python = isolated_python()
    if not python.exists():
        readiness = {
            "status": "unavailable",
            "reason": f"the isolated Kronos interpreter is absent ({python}); "
                      "the model cannot run here until it is installed outside the repo",
        }
    else:
        try:
            problem, _verified = verify_artifacts(isolated_source_dir(), isolated_model_dir())
        except OSError as exc:
            problem = f"the pinned artifacts could not be inspected: {exc}"
        readiness = {"status": "ready" if problem is None else "unavailable", "reason": problem}
    return {
        "enabled": settings.enabled,
        "mode": settings.mode,
        "timeframe": settings.timeframe,
        "horizon_hours": settings.horizon_hours,
        "paths": settings.paths,
        "temperature": settings.temperature,
        "lookback_bars": settings.lookback_bars,
        "max_batch_rows": settings.max_batch_rows,
        "failure_policy": settings.failure_policy,
        "max_band_spread_ratio": settings.max_band_spread_ratio,
        # A tuning value the core cannot run: reported here AND enforced by
        # PUT /api/settings, so the screen never shows a knob the cycle would
        # silently turn into an unavailable forecast.
        "settings_problem": settings.unavailable_reason(),
        "readiness": readiness,
        "model": {"source_revision": SOURCE_REVISION, "model_revision": MODEL_REVISION,
                  "repo": MODEL_REPO},
    }


def _payload() -> dict:
    stored = {row["key"]: row for row in control_store.load_operator_setting_rows()}
    rows = [_row(key, stored) for key in _ALLOWED]
    rows.sort(key=lambda item: (item["group"], item["key"]))

    from server.settings import effective_exec_mode

    config = get_config()
    modes = auto_modes.mode_config(config)
    return {
        "generated_at": utc_now_iso(),
        "settings": rows,
        "modes": modes,
        "gates": {
            "auto_spot": modes["spot"],
            "auto_futures": modes["futures"],
            "any_auto_enabled": any(modes.values()),
        },
        "effective_exec_mode": effective_exec_mode(),
        "kronos": _kronos_section(),
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

    from tradingagents.execution.auto_authorization import mode_update_lock

    affected_modes = [mode for mode, key in auto_modes.MODE_KEYS.items() if key in gate_changes]
    with mode_update_lock(affected_modes):
        config = get_config()
        previous = {key: config.get(key) for key in validated}
        control_store.set_operator_settings(validated, updated_by=actor)
        set_config(validated)  # apply only after the entire batch commits
    if gate_changes:
        from server import autoloop

        autoloop.sync_mode_state()
    settings_events.publish_change("operator_config_updated")

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

    from tradingagents.execution.auto_authorization import mode_update_lock

    default = auto_modes.MODE_DEFAULTS.get(key, DEFAULT_CONFIG.get(key))
    affected_modes = [mode for mode, mode_key in auto_modes.MODE_KEYS.items() if mode_key == key]
    with mode_update_lock(affected_modes):
        if key in auto_modes.MODE_DEFAULTS:
            reset = control_store.reset_operator_setting(key, default)
        else:
            reset = control_store.delete_operator_setting(key)
        if not reset:
            raise ApiError(404, "not_found", f"{key!r} has no stored override")

        set_config({key: default})  # restore the shipped default in this process
    if key in auto_modes.MODE_DEFAULTS:
        from server import autoloop

        autoloop.sync_mode_state()
    settings_events.publish_change("operator_config_reset")

    audit_log.record(
        "operator_config_reset",
        key=key,
        restored_default=default,
        gate=key in _GATE_KEYS,
    )
    return _payload()
