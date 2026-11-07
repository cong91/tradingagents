"""Read-only futures fill history scoped to symbols found in the local audit."""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter

from server import audit as audit_log, performance_history_cache as history_cache
from server.contract import ApiError
from tradingagents.dataflows.config import get_config
from tradingagents.execution.exchange_client import venue_key

router = APIRouter(prefix="/api", tags=["performance"])

_MAX_SYMBOLS = 12
_FUTURES_SYMBOL = re.compile(r"^[A-Z0-9]{1,30}/[A-Z0-9]{1,15}:[A-Z0-9]{1,15}(?:-[0-9]{6})?$")
_ACTIONS = {
    "buy", "sell", "bracket_placed", "bracket_failed", "time_stop_closed",
    "time_stop_review_hold", "time_stop_review_unavailable",
}
_AUDIT_WARNING_CODES = {
    "audit_lines_skipped",
    "audit_venue_unverified",
    "audit_symbol_missing_or_invalid",
    "audit_timestamp_invalid",
    "audit_symbol_limit_reached",
}
_WARNINGS = {
    "audit_lines_skipped": "Một số dòng nhật ký không hợp lệ đã bị bỏ qua.",
    "audit_venue_unverified": "Chưa xác minh được sàn và môi trường của một số bản ghi nhật ký.",
    "audit_symbol_missing_or_invalid": "Một số bản ghi thiếu mã hợp đồng chính xác hoặc mã không hợp lệ.",
    "audit_timestamp_invalid": "Một số thời gian trong nhật ký không hợp lệ; thứ tự có thể không chính xác.",
    "audit_symbol_limit_reached": "Chỉ chọn tối đa 12 mã gần nhất; các mã khác đã bị bỏ qua.",
    "no_audit_symbols": "Không tìm thấy mã hợp đồng đủ điều kiện trong nhật ký.",
    "live_mode_unavailable": "Chế độ live chưa được bật; không thể đọc lịch sử từ sàn.",
    "history_fetch_busy": "Một yêu cầu khác đang đọc lịch sử; vui lòng thử lại sau.",
    "history_read_failed": "Không thể đọc lịch sử khớp lệnh.",
    "time_limit_reached": "Khoảng thời gian yêu cầu đã bị giới hạn.",
    "symbol_limit_reached": "Một số mã đã vượt quá giới hạn đọc.",
    "unsupported_exchange": "Sàn hiện tại không hỗ trợ đọc lịch sử Futures này.",
    "unknown_profile": "Không xác minh được môi trường sàn hiện tại.",
    "deadline_reached": "Đã hết thời gian đọc lịch sử.",
    "client_unavailable": "Không thể tạo kết nối đọc dữ liệu sàn.",
    "profile_mismatch": "Kết nối sàn không khớp với môi trường đã cấu hình.",
    "rate_limit_disabled": "Giới hạn tốc độ của kết nối sàn chưa được bật.",
    "rate_limited": "Sàn đã giới hạn yêu cầu; vui lòng thử lại sau.",
    "history_rate_limit_cooldown": "Sàn đã giới hạn yêu cầu; đang tạm ngừng đọc lịch sử theo thời gian chờ an toàn.",
    "normalized_history_unverified": "Lịch sử chuẩn hóa chưa được xác minh đầy đủ.",
    "portfolio_margin_unsupported": "Không hỗ trợ lịch sử Futures cho tài khoản Portfolio Margin.",
    "markets_unavailable": "Không thể đọc thông tin hợp đồng từ sàn.",
    "unsupported_symbol": "Mã hợp đồng không được hỗ trợ.",
    "fetch_failed": "Không thể đọc khớp lệnh từ sàn.",
    "invalid_page": "Sàn trả về trang lịch sử không hợp lệ.",
    "invalid_fill": "Một số khớp lệnh không hợp lệ đã bị bỏ qua.",
    "conflicting_fill": "Các bản ghi khớp lệnh trùng mã có nội dung mâu thuẫn.",
    "initial_page_may_be_truncated": "Trang lịch sử đầu tiên có thể đã bị cắt ngắn.",
    "cursor_not_advancing": "Không thể tiếp tục phân trang lịch sử.",
    "page_limit_reached": "Đã đạt giới hạn số trang lịch sử.",
    "history_reader_warning": "Có cảnh báo khi đọc lịch sử từ sàn.",
}
_COVERAGE = (
    "Chỉ các mã trong nhật ký được chọn; chưa xác minh đầy đủ tài khoản và lịch sử trước khoảng này. "
    "Các giao dịch là của tài khoản sàn đang chọn, có thể do người dùng hoặc hệ thống khác thực hiện. "
    "Không xác minh được giao dịch nào do bot thực hiện. "
    "Chưa xác định cơ sở vị thế ban đầu và thời gian lưu trữ của sàn."
)
_WARNING_INTERVAL = re.compile(
    r"([^ ]+) \[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z), "
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\)"
)


def _build_bridge(config: dict):
    """Create a separate bridge and therefore a separate lazy exchange client."""
    from tradingagents.execution.bridge import ExchangeBridge

    return ExchangeBridge(config=config)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _symbols(events: list[dict], venue_key: str, warnings: list[str]) -> list[str]:
    """Select exact recorded futures symbols, newest valid timestamp first."""
    candidates = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            if "audit_lines_skipped" not in warnings:
                warnings.append("audit_lines_skipped")
            continue
        action = event.get("action")
        if not isinstance(action, str):
            if action is not None and "audit_lines_skipped" not in warnings:
                warnings.append("audit_lines_skipped")
            continue
        if (
            event.get("phase") != "execute" or event.get("mode") != "live"
            or event.get("market") != "derivatives" or action not in _ACTIONS
        ):
            continue
        recorded_venue = event.get("venue_key")
        if recorded_venue is not None and recorded_venue != "" and recorded_venue != venue_key:
            continue
        if not recorded_venue and "audit_venue_unverified" not in warnings:
            warnings.append("audit_venue_unverified")
        symbol = event.get("ccxt_symbol")
        if not isinstance(symbol, str) or not _FUTURES_SYMBOL.fullmatch(symbol):
            if "audit_symbol_missing_or_invalid" not in warnings:
                warnings.append("audit_symbol_missing_or_invalid")
            continue
        timestamp = event.get("timestamp")
        try:
            if not isinstance(timestamp, str):
                raise ValueError("missing timestamp")
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timestamp has no timezone")
            parsed = parsed.astimezone(UTC)
        except (OverflowError, ValueError):
            parsed = datetime.min.replace(tzinfo=UTC)
            if "audit_timestamp_invalid" not in warnings:
                warnings.append("audit_timestamp_invalid")
        candidates.append((recorded_venue == venue_key, parsed, index, symbol))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    symbols = list(dict.fromkeys(item[3] for item in candidates))
    if len(symbols) > _MAX_SYMBOLS:
        warnings.append("audit_symbol_limit_reached")
    return symbols[:_MAX_SYMBOLS]


def _response(result: dict) -> dict:
    result["warnings"] = [_WARNINGS.get(code, code) for code in result["warnings"]]
    return result


def _reader_warnings(warnings, symbols: list[str], start: datetime, end: datetime) -> list[str]:
    """Translate known warnings and retain only validated symbol/time context."""
    if not isinstance(warnings, list):
        raise ValueError("invalid history warnings")
    result = []
    for warning in warnings:
        if not isinstance(warning, str):
            raise ValueError("invalid history warning")
        code, _, detail = warning.partition(": ")
        message = _WARNINGS.get(code, _WARNINGS["history_reader_warning"])
        match = _WARNING_INTERVAL.fullmatch(detail)
        if match and match[1] in symbols:
            try:
                interval_start = datetime.fromisoformat(match[2].replace("Z", "+00:00"))
                interval_end = datetime.fromisoformat(match[3].replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                if start <= interval_start < interval_end <= end:
                    message += f" {match[1]} [{match[2]}, {match[3]})."
        if message not in result:
            result.append(message)
    return result


@router.get("/performance/fills")
def get_performance_fills():
    now = datetime.now(UTC)
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)
    result = {
        "generated_at": _iso(now),
        "status": "unavailable",
        "source": "unavailable",
        "venue_profile": None,
        "start_at": _iso(now - timedelta(days=7)),
        "end_at": _iso(now),
        "observed_at": None,
        "coverage": _COVERAGE,
        "warnings": [],
        "fills": [],
    }
    warnings = result["warnings"]
    try:
        events, skipped = audit_log.read_events()
    except ApiError as exc:
        if exc.status_code == 503:
            raise ApiError(503, "audit_log_unreadable", "audit log cannot be read") from exc
        result.update(status="error", source="unavailable")
        warnings.append("history_read_failed")
        return _response(result)
    except Exception:
        result.update(status="error", source="unavailable")
        warnings.append("history_read_failed")
        return _response(result)
    try:
        if skipped:
            warnings.append("audit_lines_skipped")
        config = copy.deepcopy(get_config())
        if config.get("exec_live") is not True:
            warnings.append("live_mode_unavailable")
            return _response(result)
        if str(config.get("exec_exchange_id") or "").strip().lower() != "binance":
            result["status"] = "unsupported"
            warnings.append("unsupported_exchange")
            return _response(result)
        bridge = _build_bridge(config)
        if bridge.effective_mode() != "live":
            warnings.append("live_mode_unavailable")
            return _response(result)
        symbols = _symbols(events, venue_key(bridge), warnings)
        if symbols:
            result["coverage"] += " Mã đã chọn: " + ", ".join(symbols) + "."
        if "audit_venue_unverified" in warnings:
            result["coverage"] += " Sàn và môi trường của một số bản ghi chưa được xác minh."
        if not symbols:
            warnings.append("no_audit_symbols")
            return _response(result)
        if not history_cache.HISTORY_LOCK.acquire(blocking=False):
            raise ApiError(503, "history_busy", "another history read is in progress")
        try:
            cache_key = history_cache.cache_key(config, bridge, symbols)
            if history_cache.is_rate_limited() and history_cache.cache_response(cache_key) is None:
                warnings.append("history_rate_limit_cooldown")
                return _response(result)
            try:
                client = bridge.client()
            except Exception as exc:
                if not history_cache.is_rate_limit_error(exc):
                    raise
                retry_after = history_cache.retry_after_from_exception(exc)
                history_cache.start_cooldown({"retry_after_seconds": retry_after})
                warnings.append("rate_limited")
                return _response(result)
            cache_key = history_cache.client_cache_key(config, bridge, client, symbols)
            cached = history_cache.cache_response(cache_key)
            if cached is not None and (
                get_config() == config and bridge.config == config and bridge.client() is client
                and bridge.effective_mode() == "live"
                and history_cache.client_cache_key(config, bridge, client, symbols) == cache_key
            ):
                audit_warning_texts = {_WARNINGS[code] for code in _AUDIT_WARNING_CODES}
                cached["warnings"] = [
                    warning for warning in cached["warnings"] if warning not in audit_warning_texts
                ]
                current_audit_warnings = [_WARNINGS.get(code, code) for code in warnings]
                cached["warnings"] = list(dict.fromkeys(
                    [*cached["warnings"], *current_audit_warnings]
                ))
                cached["coverage"] = result["coverage"]
                return cached
            if history_cache.is_rate_limited():
                warnings.append("history_rate_limit_cooldown")
                return _response(result)
            if (
                cache_key is None or get_config() != config or bridge.config != config
                or bridge.client() is not client or bridge.effective_mode() != "live"
                or history_cache.client_cache_key(config, bridge, client, symbols) != cache_key
            ):
                warnings.append("history_read_failed")
                return _response(result)
            client.timeout = 5000
            from server.futures_trade_history import read_futures_trade_history

            until_ms = int(now.timestamp() * 1000)
            history = read_futures_trade_history(
                bridge, symbols, until_ms - 7 * 24 * 60 * 60 * 1000, until_ms,
                max_pages_per_symbol=3, max_total_seconds=15,
            )
            if (
                get_config() != config or bridge.config != config or bridge.client() is not client
                or bridge.effective_mode() != "live"
                or history_cache.client_cache_key(config, bridge, client, symbols) != cache_key
            ):
                warnings.append("history_read_failed")
                return _response(result)
            reader_warnings = history.get("warnings")
            if isinstance(reader_warnings, list) and any(
                isinstance(warning, str) and warning.partition(": ")[0] == "rate_limited"
                for warning in reader_warnings
            ):
                history_cache.start_cooldown(history)
            status = history["status"]
            source = history["source"]
            profile = history["venue_profile"]
            fills = history["fills"]
            if (
                status not in {"ok", "partial", "error", "unsupported", "unavailable"}
                or source not in {"venue_fills", "unavailable"}
                or profile not in {"demo", "production", None}
                or not isinstance(fills, list)
                or (status in {"error", "unsupported", "unavailable"} and (source != "unavailable" or fills))
                or (status in {"ok", "partial"} and source != "venue_fills")
            ):
                raise ValueError("invalid history response")
            warnings.extend(_reader_warnings(history["warnings"], symbols, now - timedelta(days=7), now))
            result.update({
                "status": "partial" if status == "ok" else status,
                "source": source,
                "venue_profile": profile,
                "observed_at": history["observed_at"],
                "fills": fills,
            })
            response = _response(result)
            if (
                cache_key is not None and get_config() == config and bridge.config == config
                and bridge.client() is client and bridge.effective_mode() == "live"
                and history_cache.client_cache_key(config, bridge, client, symbols) == cache_key
            ):
                history_cache.store_cache(cache_key, response)
            return response
        finally:
            history_cache.HISTORY_LOCK.release()
    except ApiError:
        raise
    except Exception as exc:
        if history_cache.is_rate_limit_error(exc):
            history_cache.start_cooldown({
                "retry_after_seconds": history_cache.retry_after_from_exception(exc),
            })
            warnings.append("rate_limited")
        else:
            warnings.append("history_read_failed")
        result.update(status="error", source="unavailable", venue_profile=None, observed_at=None, fills=[])
        return _response(result)
