"""Content-free vendor timing diagnostics, scoped to the current context."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_observer: ContextVar[Any | None] = ContextVar("tradingagents_diagnostic_observer", default=None)


@contextmanager
def diagnostic_scope(observer: Any):
    """Make an observer available to diagnostic spans in this context."""
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


@contextmanager
def diagnostic_span(stage: str, ticker: str | None = None):
    """Record a span when an observer is scoped, otherwise act as a no-op."""
    observer = _observer.get()
    if observer is None:
        yield
        return
    with observer.span(stage, ticker):
        yield


__all__ = ["diagnostic_scope", "diagnostic_span"]
