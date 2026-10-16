from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

_SAFE_STAGE = re.compile(r"[^A-Za-z0-9_:. -]")
_SAFE_TICKER = re.compile(r"[^A-Za-z0-9_./:-]")


class CycleTelemetry(BaseCallbackHandler):
    """Bounded, content-free timing events for one autoloop cycle.

    Notifications run under the reentrant state lock, so observers may read a
    snapshot and close waits for any publication already in progress.
    """

    MAX_EVENTS = 100
    run_inline = True

    def __init__(self, on_change=None):
        self._lock = threading.RLock()
        self._on_change = on_change
        self._events: list[dict] = []
        self._spans: dict[str, dict] = {}
        self._sequence = 0
        self._dropped_events = 0
        self._last_stage: str | None = None
        self._current_ticker: str | None = None
        self._closed_at: float | None = None
        self._frozen_snapshot: dict | None = None

    def start(self, stage: str, ticker: str | None = None) -> str:
        with self._lock:
            return self._start_unlocked(stage, ticker, parent_id=self._active_id_unlocked())

    def finish(self, span_id: str, error: BaseException | None = None) -> None:
        with self._lock:
            self._finish_unlocked(
                span_id, self._safe_error_type(error) if error is not None else None,
            )

    @contextmanager
    def span(self, stage: str, ticker: str | None = None):
        span_id = self.start(stage, ticker)
        try:
            yield
        except BaseException as exc:
            self.finish(span_id, exc)
            raise
        else:
            self.finish(span_id)

    def graph_event(self, payload: dict, ticker: str) -> None:
        with self._lock:
            if self._closed_at is not None or not isinstance(payload, dict):
                return
            details = payload.get("payload")
            if not isinstance(details, dict):
                return
            node = details.get("name")
            task_id = details.get("id")
            if not isinstance(node, str) or not node or not isinstance(task_id, (str, UUID)):
                return
            span_id = f"graph:{task_id}"
            if payload.get("type") == "task":
                parent_id = next(
                    (key for key in reversed(self._spans) if self._spans[key]["node"] is None),
                    None,
                )
                self._start_unlocked(f"graph_node:{node}", ticker, span_id=span_id, parent_id=parent_id)
            elif payload.get("type") == "task_result":
                error_type = "GraphNodeError" if details.get("error") is not None else None
                self._finish_unlocked(span_id, error_type)

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        self._start_callback_span(run_id, "llm", serialized, **kwargs)

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._start_callback_span(run_id, "llm", serialized, **kwargs)

    def on_llm_end(self, response, *, run_id, **kwargs):
        self.finish(f"callback:{run_id}")

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.finish(f"callback:{run_id}", error)

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self._start_callback_span(run_id, "tool", serialized, **kwargs)

    def on_tool_end(self, output, *, run_id, **kwargs):
        self.finish(f"callback:{run_id}")

    def on_tool_error(self, error, *, run_id, **kwargs):
        self.finish(f"callback:{run_id}", error)

    def close(self) -> None:
        with self._lock:
            if self._closed_at is None:
                self._closed_at = time.monotonic()
                self._frozen_snapshot = self._snapshot_unlocked()
                self._spans.clear()
                self._events.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_unlocked()

    def _start_callback_span(
        self, run_id: UUID, kind: str, serialized, *, parent_run_id=None, metadata=None, **kwargs,
    ) -> None:
        with self._lock:
            if self._closed_at is not None:
                return
            node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
            node = (_SAFE_STAGE.sub("", node)[:85] or None) if isinstance(node, str) else None
            parent_id = f"callback:{parent_run_id}"
            parent = self._spans.get(parent_id)
            if parent is None or (node is not None and parent["node"] != node):
                if node is None:
                    parent_id = self._active_id_unlocked()
                else:
                    parent_id = next(
                        (key for key in reversed(self._spans)
                         if self._spans[key]["stage"] == f"graph_node:{node}"),
                        None,
                    )
                    if parent_id is None:
                        parent_id = next(
                            (key for key in reversed(self._spans) if self._spans[key]["node"] is None),
                            None,
                        )
                parent = self._spans.get(parent_id)
            ticker = parent["ticker"] if parent is not None else self._current_ticker
            self._start_unlocked(
                self._callback_stage(kind, serialized), ticker,
                span_id=f"callback:{run_id}", parent_id=parent_id, node=node,
            )

    def _start_unlocked(
        self, stage: str, ticker: str | None, *, span_id: str | None = None,
        parent_id: str | None = None, node: str | None = None,
    ) -> str:
        if self._closed_at is not None or span_id in self._spans:
            return ""
        if span_id is None:
            self._sequence += 1
            span_id = str(self._sequence)
        stage = _SAFE_STAGE.sub("", str(stage))[:96] or "unknown"
        parent = self._spans.get(parent_id, {})
        ticker = self._safe_ticker(ticker) if ticker is not None else parent.get("ticker")
        path = parent.get("path", ())
        if stage.startswith("graph_node:"):
            node = stage.removeprefix("graph_node:") or None
        else:
            node = node or parent.get("node")
            if node is not None and node != parent.get("node"):
                path += (f"graph_node:{node}",)
        span = {
            "stage": stage,
            "ticker": ticker,
            "node": node,
            "started": time.monotonic(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "path": (*path, stage),
        }
        self._spans[span_id] = span
        self._last_stage = stage
        if ticker is not None:
            self._current_ticker = ticker
        self._append({"event": "started", **self._span_fields(span)})
        self._notify_unlocked()
        return span_id

    def _finish_unlocked(self, span_id: str, error_type: str | None = None) -> None:
        if self._closed_at is not None:
            return
        span = self._spans.pop(span_id, None)
        if span is None:
            return
        self._append({
            "event": "failed" if error_type is not None else "finished",
            **self._span_fields(span),
            "duration_ms": round((time.monotonic() - span["started"]) * 1000, 1),
            **({"error_type": error_type} if error_type is not None else {}),
        })
        self._notify_unlocked()

    def _active_id_unlocked(self) -> str | None:
        return next(reversed(self._spans), None)

    @staticmethod
    def _span_fields(span: dict) -> dict:
        return {key: span[key] for key in ("stage", "ticker", "node", "started_at")}

    def _append(self, event: dict) -> None:
        self._events.append(event)
        if len(self._events) > self.MAX_EVENTS:
            self._events.pop(0)
            self._dropped_events += 1

    def _snapshot_unlocked(self) -> dict:
        if self._frozen_snapshot is not None:
            return deepcopy(self._frozen_snapshot)
        end = self._closed_at if self._closed_at is not None else time.monotonic()
        active = self._spans.get(self._active_id_unlocked())
        return {
            "active_stage": active["stage"] if active is not None else None,
            "active_ticker": active["ticker"] if active is not None else None,
            "active_elapsed_ms": round((end - active["started"]) * 1000, 1) if active is not None else None,
            "active_node": active["node"] if active is not None else None,
            "active_path": list(active["path"]) if active is not None else [],
            "active_spans": [
                {**self._span_fields(span), "elapsed_ms": round((end - span["started"]) * 1000, 1)}
                for span in self._spans.values()
            ],
            "last_stage": self._last_stage,
            "current_ticker": self._current_ticker,
            "events": [dict(event) for event in self._events],
            "dropped_events": self._dropped_events,
        }

    @staticmethod
    def _safe_error_type(error: BaseException) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "", type(error).__name__)[:64] or "Error"

    @staticmethod
    def _safe_ticker(ticker: str | None) -> str | None:
        if ticker is None:
            return None
        return _SAFE_TICKER.sub("", str(ticker))[:48] or None

    @classmethod
    def _callback_stage(cls, kind: str, serialized: dict | None) -> str:
        name = serialized.get("name") if isinstance(serialized, dict) else None
        if isinstance(name, str) and name:
            safe_name = _SAFE_STAGE.sub("", name)[:64]
            if safe_name:
                return f"{kind}:{safe_name}"
        return kind

    def _notify_unlocked(self) -> None:
        if self._on_change is not None:
            self._on_change(self._snapshot_unlocked())


__all__ = ["CycleTelemetry"]
