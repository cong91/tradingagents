"""Validated, deterministic envelope for a closed-candle futures signal."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from tradingagents.execution.futures_safety import validate_bracket


@dataclass(frozen=True)
class FuturesSignalEnvelope:
    signal_id: str
    symbol: str
    timeframe: str
    candle_open: str
    candle_close: str
    direction: str
    entry: float
    stop: float
    targets: tuple[float, ...]
    created_at: str
    expires_at: str
    confirmation_candle_open: str
    strategy: str
    strategy_version: str

    @property
    def cycle_id(self) -> str:
        return f"futures:{self.symbol}:{self.confirmation_candle_open}:{self.candle_open}:{self.strategy_version}"

    @classmethod
    def create(cls, *, symbol: str, candle_open: datetime, candle_close: datetime,
               direction: str, entry: float, stop: float, targets: tuple[float, ...],
               created_at: datetime, expires_at: datetime,
               confirmation_candle_open: datetime, strategy: str,
                strategy_version: str) -> FuturesSignalEnvelope:

        if any(value.tzinfo is None for value in
               (candle_open, candle_close, created_at, expires_at, confirmation_candle_open)):
            raise ValueError("signal timestamps must be timezone-aware")
        if candle_close > created_at.astimezone(UTC):
            raise ValueError("signal candle is not closed at creation time")
        if expires_at <= created_at:
            raise ValueError("signal expiry must be after creation")
        if direction not in {"long", "short"} or not targets:
            raise ValueError("signal requires a direction and at least one target")
        side = "buy" if direction == "long" else "sell"
        validate_bracket(side=side, entry=entry, stop_loss=stop, take_profit=targets[0])
        canonical_open = candle_open.astimezone(UTC).isoformat()
        material = f"futures:{symbol}:{confirmation_candle_open.astimezone(UTC).isoformat()}:{candle_open.astimezone(UTC).isoformat()}:{strategy_version}"
        signal_id = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return cls(signal_id, symbol, "1h", canonical_open,
                   candle_close.astimezone(UTC).isoformat(), direction,
                   float(entry), float(stop), tuple(map(float, targets)),
                   created_at.astimezone(UTC).isoformat(),
                   expires_at.astimezone(UTC).isoformat(),
                   confirmation_candle_open.astimezone(UTC).isoformat(),
                   strategy, strategy_version)

    def validate_at(self, as_of: datetime) -> None:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if as_of.astimezone(UTC).isoformat() >= self.expires_at:
            raise ValueError("futures signal has expired")

    def to_json(self) -> str:
        data = asdict(self)
        data["targets"] = list(self.targets)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str) -> FuturesSignalEnvelope:
        data = json.loads(payload)
        data["targets"] = tuple(data["targets"])
        result = cls(**data)
        expected = hashlib.sha256(
            f"futures:{result.symbol}:{result.confirmation_candle_open}:{result.candle_open}:{result.strategy_version}".encode()
        ).hexdigest()
        if result.signal_id != expected:
            raise ValueError("signal_id does not match envelope fields")
        return result
