import os
from copy import deepcopy

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Single source of truth for env-var → config-key overrides. To expose
# a new config key for environment-based override, add a row here — no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_QUICK_LLM_PROVIDER":   "quick_llm_provider",
    "TRADINGAGENTS_DEEP_LLM_PROVIDER":    "deep_llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    "TRADINGAGENTS_QUICK_LLM_BASE_URL":   "quick_llm_base_url",
    "TRADINGAGENTS_DEEP_LLM_BASE_URL":    "deep_llm_base_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    "TRADINGAGENTS_TEMPERATURE":          "temperature",
    "TRADINGAGENTS_LLM_MAX_RETRIES":      "llm_max_retries",
    # Legacy GX names are evaluated first; canonical GX_DATA_* names below win
    # when both are present.
    "GX_MARKET_INFO_TRANSPORT":           ("gx_market_info", "transport"),
    "GX_MARKET_INFO_BASE_URL":            ("gx_market_info", "base_url"),
    "GX_MARKET_INFO_API_VERSION":         ("gx_market_info", "api_version"),
    "GX_MARKET_INFO_TIMEOUT_SECONDS":     ("gx_market_info", "timeout_seconds"),
    # Deprecated alias retained for existing local setups.
    "GX_MARKET_INFO_POSTGRES_DSN":        ("gx_market_info", "postgres_dsn"),
    "GX_MARKET_INFO_DATABASE_URL":        ("gx_market_info", "postgres_dsn"),
    "GX_MARKET_INFO_EXPECTED_DB":         ("gx_market_info", "expected_database"),
    "GX_DATA_TRANSPORT":                  ("gx_market_info", "transport"),
    "GX_DATA_TIMEOUT_SECONDS":            ("gx_market_info", "timeout_seconds"),
    # Vietnam retail-social settings. FireAnt credentials intentionally have no
    # config mapping: they are read directly from environment at call time and
    # must never enter a persisted config/session payload.
    "TRADINGAGENTS_VN_SOCIAL_PROVIDER":           ("vn_social", "provider"),
    "TRADINGAGENTS_FIREANT_AUTHORIZED":           ("vn_social", "authorized"),
    "TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED": (
        "vn_social",
        "hosted_llm_authorized",
    ),
    "TRADINGAGENTS_VN_SOCIAL_TICKERS":            ("vn_social", "tickers"),
    "TRADINGAGENTS_VN_SOCIAL_LOOKBACK_DAYS":      ("vn_social", "lookback_days"),
    "TRADINGAGENTS_VN_SOCIAL_MIN_POSTS":          ("vn_social", "min_posts"),
    "TRADINGAGENTS_VN_SOCIAL_MIN_UNIQUE_AUTHORS": (
        "vn_social",
        "min_unique_authors",
    ),
    "TRADINGAGENTS_VN_SOCIAL_POLL_SECONDS":       ("vn_social", "poll_seconds"),
    "TRADINGAGENTS_SOCIAL_RAW_RETENTION_DAYS":    (
        "vn_social",
        "raw_retention_days",
    ),
    "TRADINGAGENTS_SOCIAL_ARCHIVE_PATH":          ("vn_social", "archive_path"),
    # Vietnam editorial-media policy. Authorization confirmations and the
    # archive encryption key deliberately have no config mapping: providers
    # read them directly from the environment at call time, and they must never
    # enter a durable config/session payload.
    "TRADINGAGENTS_VN_MEDIA_PROVIDERS":           ("vn_media", "providers"),
    "TRADINGAGENTS_VN_MEDIA_TICKERS":             ("vn_media", "tickers"),
    "TRADINGAGENTS_VN_MEDIA_LOOKBACK_DAYS":       ("vn_media", "lookback_days"),
    "TRADINGAGENTS_VN_MEDIA_MIN_ARTICLES":        ("vn_media", "min_articles"),
    "TRADINGAGENTS_VN_MEDIA_POLL_SECONDS":        ("vn_media", "poll_seconds"),
    "TRADINGAGENTS_VN_MEDIA_RAW_RETENTION_DAYS": (
        "vn_media",
        "raw_retention_days",
    ),
    "TRADINGAGENTS_VN_MEDIA_ARCHIVE_PATH":        ("vn_media", "archive_path"),
    # Vietnam macroeconomic evidence.  These settings contain no credentials:
    # the collector reads only allowlisted public NSO/SBV endpoints and stores
    # normalized observations in a separate point-in-time archive.
    "TRADINGAGENTS_VN_MACRO_ENABLED":             ("vn_macro", "enabled"),
    "TRADINGAGENTS_VN_MACRO_PROVIDERS":           ("vn_macro", "providers"),
    "TRADINGAGENTS_VN_MACRO_LOOKBACK_MONTHS":     ("vn_macro", "lookback_months"),
    "TRADINGAGENTS_VN_MACRO_STRICT_PIT":          (
        "vn_macro",
        "strict_point_in_time",
    ),
    "TRADINGAGENTS_VN_MACRO_TIMEOUT_SECONDS":     ("vn_macro", "timeout_seconds"),
    "TRADINGAGENTS_VN_MACRO_ARCHIVE_PATH":        ("vn_macro", "archive_path"),
    # Provider-specific reasoning/thinking knobs (None = each provider's own
    # default). Settable here for non-interactive runs; the CLI also offers an
    # interactive choice, which is skipped when the matching var is set.
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL":   "google_thinking_level",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
    "TRADINGAGENTS_ANTHROPIC_EFFORT":        "anthropic_effort",
}


_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value.

    Invalid values raise ``ValueError`` rather than silently falling back to a
    default — a misspelled boolean (e.g. ``treu``) or non-numeric int should fail
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
            if isinstance(key, tuple):
                parent, child = key
                config.setdefault(parent, {})[child] = _coerce(
                    raw, config.get(parent, {}).get(child)
                )
            else:
                config[key] = _coerce(raw, config.get(key))
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
    return config


DEFAULT_CONFIG = _apply_env_overrides({
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings
    # Role-specific provider/base URL values override the legacy shared values.
    # API keys intentionally are not config fields: they are resolved directly
    # from role/provider environment variables at client construction time.
    "llm_provider": "openai",
    "quick_llm_provider": None,
    "deep_llm_provider": None,
    "deep_think_llm": "gpt-5.5",
    "quick_think_llm": "gpt-5.4-mini",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    "quick_llm_base_url": None,
    "deep_llm_base_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Sampling temperature, forwarded to every provider when set. None leaves
    # each provider at its own default. Lower values reduce run-to-run
    # variation on models that honor it; reasoning models largely ignore it
    # and no setting makes LLM output bit-identical across runs (see README).
    "temperature": None,
    # SDK retry budget forwarded to every provider chat client. None leaves each
    # provider/SDK at its own default (usually 2). Raise it to ride out bursty
    # 429 throttling on rate-limited deployments instead of aborting a run (#1091).
    "llm_max_retries": None,
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
    # The configured value is the exact vendor chain — requests are NOT silently
    # routed to vendors you didn't choose. For ordered fallback, list several,
    # e.g. "yfinance,alpha_vantage". "default" uses all available vendors.
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
        "macro_data": "fred",                # Options: fred (needs FRED_API_KEY)
        # Inert in the upstream profile. ``apply_gx_market_info_defaults``
        # activates the separate Vietnam macro lane without changing FRED for
        # international/upstream callers.
        "vn_macro_data": "vn_macro",
        "prediction_markets": "polymarket",  # Options: polymarket (keyless)
        # Upstream/non-Vietnam sentiment keeps its existing StockTwits/Reddit
        # behavior. The GX profile below replaces this category with FireAnt.
        "social_data": "legacy_social",
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # GX source settings are inert until the ``gx_market_info`` profile/vendor
    # is selected. Credentials are read from environment by the adapter and are
    # deliberately not stored in this config dict.
    "gx_market_info": {
        "transport": "api",              # strict: api or postgres
        "base_url": "http://localhost:5005",
        "api_version": "v1.0.7",
        "timeout_seconds": 10.0,
        "postgres_dsn": None,
        "expected_database": "g_market_info_1229",
        "statement_timeout_ms": 15000,
        "strict_point_in_time": False,
    },
    # Non-secret social policy. ``authorized`` is deliberately false by
    # default: configuring a provider is not legal/business authorization to
    # collect or retain its content. FIREANT_ACCESS_TOKEN and
    # FIREANT_ARCHIVE_ENCRYPTION_KEY stay environment-only.
    "vn_social": {
        "provider": "legacy",
        "authorized": False,
        "hosted_llm_authorized": False,
        "legacy_sources_enabled": True,
        "tickers": "",
        "lookback_days": 7,
        "min_posts": 10,
        "min_unique_authors": 5,
        "poll_seconds": 300,
        "raw_retention_days": 90,
        "archive_path": os.path.join(
            _TRADINGAGENTS_HOME, "cache", "social", "vn_social.sqlite3"
        ),
        "prompt_version": "vn-social-v1",
        "archive_schema_version": 2,
    },
    # Non-secret Vietnam editorial-media settings. The empty provider list
    # leaves upstream/non-Vietnam behavior unchanged; the GX profile enables
    # the fixed CafeF/VnExpress RSS adapters. Runtime authorization flags and
    # VN_MEDIA_ARCHIVE_ENCRYPTION_KEY are intentionally absent.
    "vn_media": {
        "providers": "",
        "tickers": "",
        "lookback_days": 7,
        "min_articles": 3,
        "poll_seconds": 300,
        "raw_retention_days": 30,
        "archive_path": os.path.join(
            _TRADINGAGENTS_HOME, "cache", "media", "vn_media.sqlite3"
        ),
        "alias_policy_version": "vn-media-alias-v2",
        "prompt_version": "vn-media-v1",
        "archive_schema_version": 1,
    },
    # Public Vietnamese macro observations are retained as normalized numbers
    # and provenance only, so this archive does not need an encryption key.
    # It is disabled upstream and enabled explicitly by the GX/Vietnam profile.
    "vn_macro": {
        "enabled": False,
        "providers": "nso_sdmx,nso_release,sbv_html",
        "lookback_months": 24,
        "strict_point_in_time": True,
        "timeout_seconds": 15.0,
        "max_retries": 3,
        "archive_path": os.path.join(
            _TRADINGAGENTS_HOME, "cache", "macro", "vn_macro.sqlite3"
        ),
        "indicator_set_version": "vn-macro-v1",
        "prompt_version": "vn-macro-v1",
        "archive_schema_version": 1,
    },
    # Benchmark for alpha calculation in the reflection layer.
    # ``benchmark_ticker`` (when set) overrides the suffix map for all
    # tickers; leave it None to use ``benchmark_map`` for auto-detection
    # based on the ticker's exchange suffix. SPY remains the US default
    # so the reflection label keeps reading "Alpha vs SPY" for US tickers
    # while non-US tickers get their regional index automatically.
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
        "":     "SPY",         # default for US-listed tickers (no suffix)
    },
})


def apply_gx_market_info_defaults(config: dict | None = None) -> dict:
    """Return a copy configured for GX/Vietnam analysis.

    Upstream defaults remain Yahoo-based for backward compatibility. Explicit
    caller or environment values win; this helper supplies the approved GX CLI
    profile only where a value still equals the built-in default or is unset.
    """
    profile = deepcopy(DEFAULT_CONFIG if config is None else config)
    profile.setdefault("data_vendors", {}).update({
        "core_stock_apis": "gx_market_info",
        "technical_indicators": "gx_market_info",
        # Retail-social data for Vietnamese symbols is FireAnt-only. This
        # intentionally removes StockTwits/Reddit from the GX vendor chain;
        # their upstream/non-Vietnam defaults remain unchanged.
        "social_data": "fireant",
        "vn_macro_data": "vn_macro",
    })
    profile.setdefault("tool_vendors", {}).update({
        "get_fundamentals": "gx_market_info",
        "get_balance_sheet": "gx_market_info",
        "get_income_statement": "gx_market_info",
        # PostgreSQL mode reads fiin_cashflow directly. API mode remains an
        # explicit NOT_MODELED gap until the GX analysis endpoint exposes it;
        # neither mode silently substitutes Yahoo cash-flow data.
        "get_cashflow": "gx_market_info",
        # Official GX disclosures and editorial RSS are distinct evidence
        # lanes. They are aggregated by the Vietnam analysts, never treated as
        # fallback substitutes for one another.
        "get_disclosures": "gx_market_info",
        "get_editorial_news": "vn_media",
        "get_global_news": "yfinance",
        # The Vietnam News Analyst prefetches this bundle once.  FRED remains
        # available to the upstream profile but is not called by GX runs.
        "get_vietnam_macro_context": "vn_macro",
    })
    gx_settings = profile.setdefault("gx_market_info", {})
    gx_settings.setdefault("transport", "api")
    gx_settings["strict_point_in_time"] = True
    social_settings = profile.setdefault("vn_social", {})
    social_settings["provider"] = "fireant"
    social_settings["legacy_sources_enabled"] = False
    media_settings = profile.setdefault("vn_media", {})
    if not str(media_settings.get("providers") or "").strip():
        media_settings["providers"] = "cafef_rss,vnexpress_rss"
    macro_settings = profile.setdefault("vn_macro", {})
    if not os.environ.get("TRADINGAGENTS_VN_MACRO_ENABLED"):
        macro_settings["enabled"] = True
    if not str(macro_settings.get("providers") or "").strip():
        macro_settings["providers"] = "nso_sdmx,nso_release,sbv_html"
    profile["benchmark_ticker"] = profile.get("benchmark_ticker") or "VNINDEX"

    # Environment overlays were already applied while DEFAULT_CONFIG was
    # created. Treat their presence and non-default caller values as explicit.
    approved_defaults = {
        "output_language": ("English", "Vietnamese", "TRADINGAGENTS_OUTPUT_LANGUAGE"),
        "max_debate_rounds": (1, 1, "TRADINGAGENTS_MAX_DEBATE_ROUNDS"),
        "max_risk_discuss_rounds": (1, 1, "TRADINGAGENTS_MAX_RISK_ROUNDS"),
        "checkpoint_enabled": (False, True, "TRADINGAGENTS_CHECKPOINT_ENABLED"),
    }
    for key, (upstream_default, gx_default, env_name) in approved_defaults.items():
        if not os.environ.get(env_name) and profile.get(key, upstream_default) == upstream_default:
            profile[key] = gx_default

    if profile.get("llm_provider", "").lower() == "openai":
        if not os.environ.get("TRADINGAGENTS_OPENAI_REASONING_EFFORT") and profile.get(
            "openai_reasoning_effort"
        ) is None:
            profile["openai_reasoning_effort"] = "medium"
        if not os.environ.get("TRADINGAGENTS_LLM_MAX_RETRIES") and profile.get(
            "llm_max_retries"
        ) is None:
            profile["llm_max_retries"] = 6
    return profile
