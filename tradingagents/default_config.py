import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Single source of truth for env-var â†’ config-key overrides. To expose
# a new config key for environment-based override, add a row here â€” no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    # Per-tier provider overrides: each tier (quick = analysts/researchers/
    # trader/risk voices, deep = research manager/portfolio manager) defaults
    # to llm_provider / backend_url when unset, so one run can mix providers
    # (e.g. a cheap OpenAI-compatible relay for analysis, a stronger native
    # API for decisions). API keys resolve per provider through
    # api_key_env.PROVIDER_API_KEY_ENV, so both keys coexist in .env.
    "TRADINGAGENTS_QUICK_PROVIDER":       "quick_provider",
    "TRADINGAGENTS_DEEP_PROVIDER":        "deep_provider",
    "TRADINGAGENTS_QUICK_BACKEND_URL":    "quick_backend_url",
    "TRADINGAGENTS_DEEP_BACKEND_URL":     "deep_backend_url",
    # OpenRouter-only fallback chain per tier, comma-separated model IDs tried
    # in order after the tier's primary model (sent as the request's "models"
    # list, so OpenRouter fails over on 429/5xx/unavailable upstreams).
    "TRADINGAGENTS_QUICK_FALLBACK_MODELS": "quick_fallback_models",
    "TRADINGAGENTS_DEEP_FALLBACK_MODELS":  "deep_fallback_models",
    # Wire protocol for OpenAI-compatible endpoints: "chat" (default) speaks
    # Chat Completions; "responses" switches to /responses for endpoints that
    # implement it. Validated in build_llm_kwargs.
    "TRADINGAGENTS_LLM_WIRE_PROTOCOL":         "llm_wire_protocol",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    "TRADINGAGENTS_TEMPERATURE":          "temperature",
    "TRADINGAGENTS_LLM_TIMEOUT":          "llm_timeout",
    "TRADINGAGENTS_LLM_MAX_RETRIES":      "llm_max_retries",
    "TRADINGAGENTS_MAX_TOKENS":           "max_tokens",
    # Provider-specific reasoning/thinking knobs (None = each provider's own
    # default). Settable here for non-interactive runs; the CLI also offers an
    # interactive choice, which is skipped when the matching var is set.
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL":   "google_thinking_level",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
    "TRADINGAGENTS_ANTHROPIC_EFFORT":        "anthropic_effort",
    # Execution bridge (L1 ccxt): quote currency only. TRADINGAGENTS_EXEC_LIVE
    # is deliberately NOT here â€” it is the env half of the FR5 double gate and
    # its only consumer is the fail-closed execute-time read in the bridge;
    # folding it into config at import would collapse the two gates into one.
    "TRADINGAGENTS_EXEC_QUOTE_CURRENCY":    "exec_quote_currency",
    # Risk guard limits (L2, FR-K): percentages coerce as floats, the streak
    # cap as an int. exec_symbol_overrides/exec_watchlist are deliberately NOT
    # here â€” _coerce cannot build a dict or list, so an env row would inject a
    # raw string where structured data is expected.
    "TRADINGAGENTS_RISK_MAX_DAILY_LOSS_PCT":           "risk_max_daily_loss_pct",
    "TRADINGAGENTS_RISK_MAX_POSITION_PCT_PER_ASSET":   "risk_max_position_pct_per_asset",
    "TRADINGAGENTS_RISK_MAX_TOTAL_EXPOSURE_PCT":       "risk_max_total_exposure_pct",
    "TRADINGAGENTS_RISK_MAX_CONSECUTIVE_LOSS_COUNT":   "risk_max_consecutive_loss_count",
    "TRADINGAGENTS_RISK_MAX_DERIVATIVES_LEVERAGE":     "risk_max_derivatives_leverage",
    "TRADINGAGENTS_RISK_MAX_DERIVATIVES_EXPOSURE_PCT": "risk_max_derivatives_exposure_pct",
    "TRADINGAGENTS_RISK_DERIVATIVES_MAX_RISK_FRACTION": "risk_derivatives_max_risk_fraction",
    "TRADINGAGENTS_RISK_DERIVATIVES_MAX_MARGIN_FRACTION": "risk_derivatives_max_margin_fraction",
    "TRADINGAGENTS_RISK_DERIVATIVES_RISK_FRACTION": "risk_derivatives_risk_fraction",
    "TRADINGAGENTS_RISK_DERIVATIVES_MARGIN_FRACTION": "risk_derivatives_margin_fraction",
    "TRADINGAGENTS_RISK_DERIVATIVES_PROFIT_PROTECTION_ENABLED": "risk_derivatives_profit_protection_enabled",
    "TRADINGAGENTS_RISK_DERIVATIVES_BREAK_EVEN_R": "risk_derivatives_break_even_r",
    "TRADINGAGENTS_RISK_DERIVATIVES_LOCK_PROFIT_R": "risk_derivatives_lock_profit_r",
    "TRADINGAGENTS_RISK_DERIVATIVES_TRAIL_START_R": "risk_derivatives_trail_start_r",
    "TRADINGAGENTS_RISK_DERIVATIVES_TRAIL_DISTANCE_R": "risk_derivatives_trail_distance_r",
    "TRADINGAGENTS_RISK_DERIVATIVES_TARGET_EXTENSION_R": "risk_derivatives_target_extension_r",
    "TRADINGAGENTS_RISK_DERIVATIVES_MIN_STOP_MOVE_R": "risk_derivatives_min_stop_move_r",
    "TRADINGAGENTS_RISK_DERIVATIVES_MARK_MAX_AGE_SECONDS": "risk_derivatives_mark_max_age_seconds",
    "TRADINGAGENTS_RISK_DERIVATIVES_SCALE_OUT_ENABLED": "risk_derivatives_scale_out_enabled",
    "TRADINGAGENTS_RISK_DERIVATIVES_SCALE_OUT_FIRST_R": "risk_derivatives_scale_out_first_r",
    "TRADINGAGENTS_RISK_DERIVATIVES_SCALE_OUT_FIRST_FRACTION": "risk_derivatives_scale_out_first_fraction",
    "TRADINGAGENTS_RISK_DERIVATIVES_SCALE_OUT_SECOND_R": "risk_derivatives_scale_out_second_r",
    "TRADINGAGENTS_RISK_DERIVATIVES_SCALE_OUT_SECOND_FRACTION": "risk_derivatives_scale_out_second_fraction",
    "TRADINGAGENTS_EXEC_EXCHANGE_ID":                  "exec_exchange_id",
    "TRADINGAGENTS_EXEC_BASE_URL":                     "exec_base_url",
    # Safe mode routes Binance to its Demo Trading API; other exchanges use
    # their ccxt sandbox when available. This is safe-direction and env-overridable.
    "TRADINGAGENTS_EXEC_SANDBOX":                      "exec_sandbox",
}


_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value.

    Invalid values raise ``ValueError`` rather than silently falling back to a
    default â€” a misspelled boolean (e.g. ``treu``) or non-numeric int should fail
    loudly at startup, not quietly misconfigure an unattended run.
    """
    if isinstance(reference, bool):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
        raise ValueError(
            f"expected a boolean ({'/'.join(_BOOL_TRUE + _BOOL_FALSE)}), got {value!r}"
        )
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            config[key] = _coerce(raw, config.get(key))
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
    return config


DEFAULT_CONFIG = _apply_env_overrides({
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR") or os.path.join(_TRADINGAGENTS_HOME, "logs"),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR") or os.path.join(_TRADINGAGENTS_HOME, "cache"),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH") or os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md"),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-6-sol",
    "quick_think_llm": "gpt-6-luna",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Per-tier provider overrides (None = follow llm_provider / backend_url),
    # so quick and deep tiers can use different providers with their own API
    # keys (keys resolve per provider via api_key_env.PROVIDER_API_KEY_ENV).
    "quick_provider": None,
    "deep_provider": None,
    "quick_backend_url": None,
    "deep_backend_url": None,
    # OpenRouter fallback chains (comma-separated model IDs; "" = primary only).
    # Ignored for every other provider.
    "quick_fallback_models": "",
    "deep_fallback_models": "",
    # Wire protocol for OpenAI-compatible endpoints: "chat" (Chat Completions,
    # the wide-compatibility default) or "responses" (/responses for endpoints
    # that implement it). Native OpenAI ignores this -- it already prefers
    # Responses for models that support it.
    "llm_wire_protocol": "chat",
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Sampling temperature, forwarded to every provider when set. None leaves
    # each provider at its own default. Lower values reduce run-to-run
    # variation on models that honor it; reasoning models largely ignore it
    # and no setting makes LLM output bit-identical across runs (see README).
    "temperature": None,
    # Bound one provider request so a stalled relay cannot hold an autoloop
    # worker forever. The default stays below common reverse-proxy read limits.
    "llm_timeout": 60.0,
    # SDK retry budget forwarded to every provider chat client. None leaves each
    # provider/SDK at its own default (usually 2). Raise it to ride out bursty
    # 429 throttling on rate-limited deployments instead of aborting a run (#1091).
    "llm_max_retries": None,
    # Cap on output tokens forwarded to every provider chat client. None leaves
    # each provider at its own default. Set it to bound a model that emits
    # unbounded reasoning/output and hangs or trips a gateway idle timeout
    # (e.g. some deepseek-v4-flash deployments, #1204).
    "max_tokens": None,
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # News / data fetching parameters
    # Increase for longer lookback strategies or to broaden macro coverage;
    # decrease to reduce token usage in agent prompts.
    "news_article_limit": 20,             # max articles per ticker (ticker-news)
    "global_news_article_limit": 10,      # max articles for global/macro news
    "global_news_lookback_days": 7,       # macro news lookback window
    # Search queries used by get_global_news for macro headlines. Extend or
    # replace to broaden geographic / sector coverage.
    "global_news_queries": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
    # Data vendor configuration
    # Category-level configuration (default for all tools in category).
    # The configured value is the exact vendor chain â€” requests are NOT silently
    # routed to vendors you didn't choose. For ordered fallback, list several,
    # e.g. "yfinance,alpha_vantage". "default" uses all available vendors.
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
        "macro_data": "fred",                # Options: fred (needs FRED_API_KEY)
        "prediction_markets": "polymarket",  # Options: polymarket (keyless)
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # Benchmark for alpha calculation in the reflection layer.
    # ``benchmark_ticker`` (when set) overrides the suffix map for all
    # tickers; leave it None to use ``benchmark_map`` for auto-detection
    # based on the ticker's exchange suffix. SPY remains the US default
    # so the reflection label keeps reading "Alpha vs SPY" for US tickers
    # while non-US tickers get their regional index automatically.
    # Trading days after the analysis date over which a decision's outcome is
    # measured, for reflection and for the backtest figures.
    "holding_period_days": 5,
    "benchmark_ticker": None,
    "benchmark_map": {
        ".NS":  "^NSEI",       # NSE India (Nifty 50)
        ".BO":  "^BSESN",      # BSE India (Sensex)
        ".T":   "^N225",       # Tokyo (Nikkei 225)
        ".HK":  "^HSI",        # Hong Kong (Hang Seng)
        ".L":   "^FTSE",       # London (FTSE 100)
        ".TO":  "^GSPTSE",     # Toronto (TSX Composite)
        ".AX":  "^AXJO",       # Australia (ASX 200)
        ".SS":  "000001.SS",   # Shanghai (SSE Composite)
        ".SZ":  "399001.SZ",   # Shenzhen (SZSE Component)
        ".SA":  "^BVSP",       # B3 Brazil (Ibovespa)
        "":     "SPY",         # default for US-listed tickers (no suffix)
    },
    # Execution bridge (L1 ccxt, Binance spot). Live requires BOTH this gate â€”
    # armed as a literal True in code, never via env â€” and the
    # TRADINGAGENTS_EXEC_LIVE env flag, re-read at execute time, fail-closed.
    # Keeping the halves independent means no single switch arms live trading.
    "exec_live": False,
    "exec_log_path": os.getenv("TRADINGAGENTS_EXEC_LOG_PATH") or os.path.join(_TRADINGAGENTS_HOME, "execution", "audit.jsonl"),
    "exec_quote_currency": "USDT",
    # Risk guard limits (L2, FR-K). Percent limits are fail-open by design:
    # when equity cannot be computed (no cash and nothing to mark) the check
    # is skipped and logged. The consecutive-loss cap is absolute â€” no
    # denominator â€” but it only enforces while the audit log is READABLE:
    # execute-time re-checks fail closed on an unreadable log (refuse the
    # order), plan-time checks stay fail-open.
    # risk_max_derivatives_exposure_pct defaults to 0: derivatives notional
    # is blocked outright until raised.
    "risk_max_daily_loss_pct": 5.0,
    "risk_max_position_pct_per_asset": 25.0,
    "risk_max_total_exposure_pct": 80.0,
    "risk_max_consecutive_loss_count": 3,
    "risk_max_derivatives_leverage": 1.0,
    "risk_max_derivatives_exposure_pct": 0.0,
    # Decisions the futures agents own, and the ceilings the executor enforces
    # on them. The agent proposes; a proposal above these caps is refused, never
    # silently clamped to a different size.
    "risk_derivatives_max_risk_fraction": 0.02,
    "risk_derivatives_max_margin_fraction": 0.5,
    "risk_derivatives_risk_fraction": 0.01,
    "risk_derivatives_margin_fraction": 0.25,
    "risk_derivatives_profit_protection_enabled": True,
    "risk_derivatives_break_even_r": 1.0,
    "risk_derivatives_lock_profit_r": 2.0,
    "risk_derivatives_trail_start_r": 3.0,
    "risk_derivatives_trail_distance_r": 1.0,
    "risk_derivatives_target_extension_r": 1.0,
    # Refuse tiny stop nudges that would churn conditional orders every poll,
    # and treat a venue mark older than this as unusable (no mutation).
    "risk_derivatives_min_stop_move_r": 0.1,
    "risk_derivatives_mark_max_age_seconds": 120.0,
    # Deliberate scale-out: two profit tranches at R multiples plus a runner
    # managed by the trailing stop. OFF by default; the single bracket keeps
    # the previous behaviour until an operator opts in.
    "risk_derivatives_scale_out_enabled": False,
    "risk_derivatives_scale_out_first_r": 1.0,
    "risk_derivatives_scale_out_first_fraction": 0.33,
    "risk_derivatives_scale_out_second_r": 2.0,
    "risk_derivatives_scale_out_second_fraction": 0.33,
    # Execution bridge (L2): multi-exchange. ``exec_exchange_id`` selects the
    # ccxt class; credentials come from {ID}_API_KEY / {ID}_SECRET env vars.
    # ``exec_symbol_overrides`` maps a pipeline ticker to a full ccxt symbol
    # (e.g. {"BTC-USD": "BTC/USDC"}), bypassing the closed crypto set.
    "exec_exchange_id": "binance",
    "exec_symbol_overrides": {},
    # Explicit Binance venue URL. Demo is the safe default; switching to
    # production is a credentials-and-URL change, independent of live gates.
    "exec_base_url": "https://demo-api.binance.com",
    # Route non-Binance exchanges through ccxt sandbox when requested.
    "exec_sandbox": False,
    # Default coin list for the daily runner (pipeline ticker form).
    "exec_watchlist": ["BTC-USD", "ETH-USD"],
    # L2 FR5-style gates. Deliberately NOT in _ENV_OVERRIDES (same reasoning
    # as exec_live above): the env halves TRADINGAGENTS_EXEC_AUTO_CONFIRM /
    # TRADINGAGENTS_EXEC_DERIVATIVES are read fail-closed at call time, so no
    # single switch can arm automatic or derivatives trading. Derivatives
    # additionally requires per-order confirm=True even in dry-run.
    "exec_auto_confirm": False,
    "exec_derivatives": False,
})
