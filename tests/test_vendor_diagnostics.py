from contextlib import contextmanager

import pytest

from tradingagents import diagnostics


class _Observer:
    def __init__(self):
        self.stages = []

    @contextmanager
    def span(self, stage, ticker=None):
        self.stages.append((stage, ticker))
        yield


@pytest.mark.unit
def test_diagnostic_scope_resets_after_span_error():
    observer = _Observer()

    with (
        pytest.raises(RuntimeError, match="source failure"),
        diagnostics.diagnostic_scope(observer),
        diagnostics.diagnostic_span("vendor:failure"),
    ):
        raise RuntimeError("source failure")

    with diagnostics.diagnostic_span("vendor:outside"):
        pass

    assert observer.stages == [("vendor:failure", None)]


@pytest.mark.unit
def test_router_spans_each_attempt_with_static_stages_and_preserves_fallback(monkeypatch):
    from tradingagents.dataflows import router
    from tradingagents.dataflows.config import set_config
    from tradingagents.dataflows.errors import VendorRateLimitError

    observer = _Observer()
    set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})

    def throttled(*args, **kwargs):
        raise VendorRateLimitError("limited")

    def fallback(*args, **kwargs):
        return "fallback data"

    monkeypatch.setitem(
        router.VENDOR_METHODS,
        "get_stock_data",
        {"yfinance": throttled, "alpha_vantage": fallback},
    )
    secret_argument = "DO_NOT_EMIT_THIS_ARGUMENT"

    with diagnostics.diagnostic_scope(observer):
        result = router.route_to_vendor("get_stock_data", secret_argument)

    assert result == "fallback data"
    assert observer.stages == [
        ("vendor:yfinance:get_stock_data", None),
        ("vendor:alpha_vantage:get_stock_data", None),
    ]
    assert secret_argument not in repr(observer.stages)


@pytest.mark.unit
def test_sentiment_sources_emit_static_stages_before_llm(monkeypatch):
    from tradingagents.agents.analysts import sentiment_analyst

    observer = _Observer()
    call_order = []
    monkeypatch.setattr(sentiment_analyst, "get_instrument_context_from_state", lambda state: "")
    monkeypatch.setattr(sentiment_analyst, "bind_structured", lambda *args: object())
    monkeypatch.setattr(sentiment_analyst, "get_news", type("News", (), {
        "func": staticmethod(lambda *args: "news")
    })())
    monkeypatch.setattr(sentiment_analyst, "jev_screen", lambda ticker: None)
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *args, **kwargs: "stocktwits")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *args, **kwargs: "reddit")

    def fake_invoke(*args):
        call_order.append("llm")
        return "report"

    monkeypatch.setattr(sentiment_analyst, "invoke_structured_or_freetext", fake_invoke)
    original_span = observer.span

    @contextmanager
    def ordered_span(stage, ticker=None):
        call_order.append(stage)
        with original_span(stage, ticker):
            yield

    observer.span = ordered_span
    analyst = sentiment_analyst.create_sentiment_analyst(object())

    with diagnostics.diagnostic_scope(observer):
        analyst({"company_of_interest": "SECRET_TICKER", "trade_date": "2026-01-09", "messages": []})

    assert call_order == [
        "vendor:news",
        "vendor:jev_screen",
        "vendor:stocktwits",
        "vendor:reddit",
        "llm",
    ]
    assert observer.stages == [
        ("vendor:news", None),
        ("vendor:jev_screen", None),
        ("vendor:stocktwits", None),
        ("vendor:reddit", None),
    ]
    assert "SECRET_TICKER" not in repr(observer.stages)
