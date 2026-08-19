import logging
from dataclasses import dataclass
from typing import Any

from .alpha_vantage import (
    get_balance_sheet as get_alpha_vantage_balance_sheet,
    get_cashflow as get_alpha_vantage_cashflow,
    get_fundamentals as get_alpha_vantage_fundamentals,
    get_global_news as get_alpha_vantage_global_news,
    get_income_statement as get_alpha_vantage_income_statement,
    get_indicator as get_alpha_vantage_indicator,
    get_insider_transactions as get_alpha_vantage_insider_transactions,
    get_news as get_alpha_vantage_news,
    get_stock as get_alpha_vantage_stock,
)
from .config import get_config
from .errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorRateLimitError,
)
from .fred import get_macro_data as get_fred_macro_data
from .gx_market_info import (
    get_balance_sheet as get_gx_balance_sheet,
    get_cashflow as get_gx_cashflow,
    get_fundamentals as get_gx_fundamentals,
    get_income_statement as get_gx_income_statement,
    get_indicators as get_gx_indicators,
    get_news as get_gx_news,
    get_stock_data as get_gx_stock_data,
)
from .polymarket import get_prediction_markets as get_polymarket_prediction_markets
from .reddit import fetch_reddit_posts
from .stocktwits import fetch_stocktwits_messages
from .vietnam_social import get_social_data as get_fireant_social_data
from .y_finance import (
    get_balance_sheet as get_yfinance_balance_sheet,
    get_cashflow as get_yfinance_cashflow,
    get_fundamentals as get_yfinance_fundamentals,
    get_income_statement as get_yfinance_income_statement,
    get_insider_transactions as get_yfinance_insider_transactions,
    get_stock_stats_indicators_window,
    get_YFin_data_online,
)
from .yfinance_news import get_global_news_yfinance, get_news_yfinance

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VendorResult:
    """Internal routing result with observed, non-secret provider provenance.

    Public LangChain tools continue returning ``value`` through
    :func:`route_to_vendor`.  Callers that need audit metadata (for example the
    sentiment session writer) can use :func:`route_to_vendor_result` without
    changing the upstream tool contract.
    """

    value: Any
    method: str
    category: str
    actual_vendor: str | None
    attempted_vendors: tuple[str, ...]

    @property
    def actual_vendor_observed(self) -> bool:
        return self.actual_vendor is not None

# Tools organized by category
TOOLS_CATEGORIES = {
    "core_stock_apis": {
        "description": "OHLCV stock price data",
        "tools": [
            "get_stock_data"
        ]
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": [
            "get_indicators"
        ]
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement"
        ]
    },
    "news_data": {
        "description": "News and insider data",
        "tools": [
            "get_news",
            "get_editorial_news",
            "get_disclosures",
            "get_global_news",
            "get_insider_transactions",
        ]
    },
    "macro_data": {
        "description": "Macroeconomic indicators (rates, inflation, labor, growth)",
        "tools": [
            "get_macro_indicators",
        ]
    },
    "vn_macro_data": {
        "description": "Point-in-time Vietnam macroeconomic evidence from NSO and SBV",
        "tools": [
            "get_vietnam_macro_context",
        ],
    },
    "prediction_markets": {
        "description": "Market-implied probabilities for forward-looking events",
        "tools": [
            "get_prediction_markets",
        ]
    },
    "social_data": {
        "description": "Retail-social evidence",
        "tools": ["get_social_data"],
    },
}

VENDOR_LIST = [
    "yfinance",
    "fred",
    "polymarket",
    "alpha_vantage",
    "gx_market_info",
    "vn_media",
    "fireant",
    "legacy_social",
    "vn_macro",
]


def _get_legacy_social_data(ticker: str, as_of: str, lookback_days: int | None = None) -> str:
    """Preserve upstream StockTwits + Reddit aggregation behind the new category."""
    return (
        "### StockTwits\n"
        f"{fetch_stocktwits_messages(ticker, as_of=as_of)}\n\n"
        "### Reddit\n"
        f"{fetch_reddit_posts(ticker, as_of=as_of, lookback_days=lookback_days or 7)}"
    )


def _get_vietnam_editorial_news(
    ticker: str,
    start_date: str,
    end_date: str,
    aliases: list[str] | None = None,
) -> str:
    """Load Vietnam editorial RSS from the local archive.

    The import is deliberately lazy: ``vn-media`` is an optional dependency,
    and upstream profiles must remain importable without it installed.
    """
    from .vietnam_media import get_editorial_news

    return get_editorial_news(ticker, start_date, end_date, aliases=aliases)


def _get_vietnam_macro_context(
    curr_date: str,
    look_back_months: int | None = None,
) -> str:
    """Load Vietnam macro evidence without importing optional parsers eagerly."""
    from .vietnam_macro import get_vietnam_macro_context

    return get_vietnam_macro_context(
        curr_date,
        look_back_months=24 if look_back_months is None else look_back_months,
    )

# Optional enrichment categories. These add macro/event context to the news
# analyst but are not core to a decision, so a vendor failure here degrades to a
# sentinel instead of aborting the run (a bad LLM-supplied indicator, a missing
# key, or a network blip should not crash an analysis over flavour data). Core
# categories (prices, fundamentals, news) still raise so a broken primary is loud.
OPTIONAL_CATEGORIES = {"macro_data", "vn_macro_data", "prediction_markets"}

# Mapping of methods to their vendor-specific implementations
VENDOR_METHODS = {
    # core_stock_apis
    "get_stock_data": {
        "alpha_vantage": get_alpha_vantage_stock,
        "yfinance": get_YFin_data_online,
        "gx_market_info": get_gx_stock_data,
    },
    # technical_indicators
    "get_indicators": {
        "alpha_vantage": get_alpha_vantage_indicator,
        "yfinance": get_stock_stats_indicators_window,
        "gx_market_info": get_gx_indicators,
    },
    # fundamental_data
    "get_fundamentals": {
        "alpha_vantage": get_alpha_vantage_fundamentals,
        "yfinance": get_yfinance_fundamentals,
        "gx_market_info": get_gx_fundamentals,
    },
    "get_balance_sheet": {
        "alpha_vantage": get_alpha_vantage_balance_sheet,
        "yfinance": get_yfinance_balance_sheet,
        "gx_market_info": get_gx_balance_sheet,
    },
    "get_cashflow": {
        "alpha_vantage": get_alpha_vantage_cashflow,
        "yfinance": get_yfinance_cashflow,
        "gx_market_info": get_gx_cashflow,
    },
    "get_income_statement": {
        "alpha_vantage": get_alpha_vantage_income_statement,
        "yfinance": get_yfinance_income_statement,
        "gx_market_info": get_gx_income_statement,
    },
    # news_data
    "get_news": {
        "alpha_vantage": get_alpha_vantage_news,
        "yfinance": get_news_yfinance,
        "gx_market_info": get_gx_news,
    },
    "get_editorial_news": {
        "vn_media": _get_vietnam_editorial_news,
    },
    "get_disclosures": {
        # The existing GX ``get_news`` renderer is intentionally retained for
        # compatibility; this new contract gives its corporate-event payload
        # the correct semantic name in the Vietnam profile.
        "gx_market_info": get_gx_news,
    },
    "get_global_news": {
        "yfinance": get_global_news_yfinance,
        "alpha_vantage": get_alpha_vantage_global_news,
    },
    "get_insider_transactions": {
        "alpha_vantage": get_alpha_vantage_insider_transactions,
        "yfinance": get_yfinance_insider_transactions,
    },
    # macro_data
    "get_macro_indicators": {
        "fred": get_fred_macro_data,
    },
    "get_vietnam_macro_context": {
        "vn_macro": _get_vietnam_macro_context,
    },
    # prediction_markets
    "get_prediction_markets": {
        "polymarket": get_polymarket_prediction_markets,
    },
    # social_data
    "get_social_data": {
        "fireant": get_fireant_social_data,
        "legacy_social": _get_legacy_social_data,
    },
}

def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")

def get_vendor(category: str, method: str = None) -> str:
    """Get the configured vendor for a data category or specific tool method.
    Tool-level configuration takes precedence over category-level.
    """
    config = get_config()

    # Check tool-level configuration first (if method provided)
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    # Fall back to category-level configuration
    return config.get("data_vendors", {}).get(category, "default")

def route_to_vendor(method: str, *args, **kwargs):
    """Route a call and preserve the original public raw-value contract."""
    return route_to_vendor_result(method, *args, **kwargs).value


def route_to_vendor_result(method: str, *args, **kwargs) -> VendorResult:
    """Route a call while retaining which configured vendor actually served it."""
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    primary_vendors = [v.strip() for v in vendor_config.split(',')]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    all_available_vendors = list(VENDOR_METHODS[method].keys())

    # The configured vendor list IS the chain: we do NOT silently fall back to
    # vendors the user did not choose (#988/#289) — that returned data from an
    # unexpected source and caused cross-vendor inconsistencies. For multi-vendor
    # fallback, list them in order, e.g. data_vendors="yfinance,alpha_vantage".
    # The "default" sentinel (no explicit config) uses all available vendors.
    explicit = [v for v in primary_vendors if v and v != "default"]
    if explicit:
        vendor_chain = [v for v in explicit if v in VENDOR_METHODS[method]]
        if not vendor_chain:
            raise ValueError(
                f"Configured vendor(s) {explicit} not available for '{method}'. "
                f"Available: {all_available_vendors}."
            )
    else:
        vendor_chain = all_available_vendors

    last_no_data: NoMarketDataError | None = None
    first_error: Exception | None = None
    attempted_vendors: list[str] = []
    for vendor in vendor_chain:
        attempted_vendors.append(vendor)
        vendor_impl = VENDOR_METHODS[method][vendor]
        impl_func = vendor_impl[0] if isinstance(vendor_impl, list) else vendor_impl

        try:
            return VendorResult(
                value=impl_func(*args, **kwargs),
                method=method,
                category=category,
                actual_vendor=vendor,
                attempted_vendors=tuple(attempted_vendors),
            )
        except VendorRateLimitError:
            logger.warning("Vendor %r rate-limited for %s; trying next vendor.", vendor, method)
            continue
        except VendorNotConfiguredError as e:
            logger.warning("Vendor %r not configured for %s; trying next vendor.", vendor, method)
            if first_error is None:
                first_error = e  # Surface it if no other vendor can serve the call.
            continue
        except NoMarketDataError as e:
            last_no_data = e  # No data here; another configured vendor may have it
            continue
        except Exception as e:
            # Don't let one vendor's failure crash the call when another can
            # serve it, but never swallow silently: a broken primary must be
            # visible in the logs (#989), not hidden behind a fallback's verdict.
            logger.warning("Vendor %r failed for %s: %s", vendor, method, e)
            if first_error is None:
                first_error = e
            continue

    # If any vendor reported "no data", the symbol is genuinely unavailable.
    # Return one explicit, instructive sentinel rather than a vendor-specific
    # empty string, so the agent reports "unavailable" instead of inventing a
    # value. This takes precedence over incidental fallback errors.
    if last_no_data is not None:
        if first_error is not None:
            # A vendor also hit a real error; surface it in logs so the no-data
            # verdict can't hide a broken primary (network/auth/etc.).
            logger.warning(
                "Returning NO_DATA for %s, but a vendor errored earlier: %s",
                method, first_error,
            )
        sym = last_no_data.symbol
        canonical = last_no_data.canonical
        resolved = "" if canonical == sym else f" (resolved to '{canonical}')"
        # Surface the typed error's detail (e.g. "latest row is 2025-06-11 ...
        # stale") so the agent sees the specific reason — invalid symbol, no
        # coverage, or stale data — not just a generic "unavailable".
        reason = f" ({last_no_data.detail})" if last_no_data.detail else ""
        return VendorResult(
            value=(
                f"NO_DATA_AVAILABLE: No usable market data for '{sym}'{resolved} from "
                f"any configured vendor{reason}. The symbol may be invalid, delisted, "
                f"not covered, or the vendor returned stale data. Do not estimate or "
                f"fabricate values — report that data is unavailable for this symbol."
            ),
            method=method,
            category=category,
            actual_vendor=None,
            attempted_vendors=tuple(attempted_vendors),
        )

    # No vendor returned data and none reported clean "no data" — surface the
    # first real error (e.g. the primary vendor's network failure). Optional
    # enrichment categories degrade to a sentinel instead, so flavour data can't
    # abort the run.
    if first_error is not None:
        if category in OPTIONAL_CATEGORIES:
            logger.warning("Optional %s unavailable for %s: %s", category, method, first_error)
            return VendorResult(
                value=(
                    f"DATA_UNAVAILABLE: optional {category} could not be retrieved "
                    f"({first_error}). Proceed without it; do not fabricate values."
                ),
                method=method,
                category=category,
                actual_vendor=None,
                attempted_vendors=tuple(attempted_vendors),
            )
        raise first_error

    raise RuntimeError(f"No available vendor for '{method}'")


def get_ohlcv_frame(
    symbol: str,
    start_date: str,
    end_date: str,
    resolution: str = "1D",
):
    """Return a canonical OHLCV DataFrame from the configured price vendor.

    This internal route keeps deterministic indicator, validation, identity,
    and reflection paths on the same data source as ``get_stock_data``.
    """
    category = "core_stock_apis"
    vendor_config = get_vendor(category, "get_stock_data")
    vendors = [v.strip() for v in vendor_config.split(",") if v.strip()]
    if not vendors or vendors == ["default"]:
        vendors = list(VENDOR_METHODS["get_stock_data"])

    first_error: Exception | None = None
    last_no_data: NoMarketDataError | None = None
    for vendor in vendors:
        try:
            if vendor == "gx_market_info":
                from .gx_market_info import get_ohlcv_frame as gx_get_ohlcv_frame

                return gx_get_ohlcv_frame(symbol, start_date, end_date, resolution)
            if vendor == "yfinance":
                from .stockstats_utils import load_yfinance_ohlcv_range

                if resolution != "1D":
                    raise ValueError("Yahoo internal OHLCV routing supports only resolution=1D")
                return load_yfinance_ohlcv_range(symbol, start_date, end_date)
            if vendor == "alpha_vantage":
                from io import StringIO

                import pandas as pd

                raw = get_alpha_vantage_stock(symbol, start_date, end_date)
                frame = pd.read_csv(StringIO(raw))
                date_col = frame.columns[0]
                return frame.rename(columns={date_col: "Date"})
        except NoMarketDataError as exc:
            last_no_data = exc
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if last_no_data is not None:
        raise last_no_data
    if first_error is not None:
        raise first_error
    raise ValueError(f"No OHLCV frame implementation for configured vendors: {vendors}")


def get_instrument_identity(
    symbol: str,
    as_of: str | None = None,
) -> dict:
    """Resolve identity from the configured core vendor, fail-open by design."""
    vendor_config = get_vendor("core_stock_apis", "get_stock_data")
    vendors = [v.strip() for v in vendor_config.split(",") if v.strip()]
    if "gx_market_info" in vendors:
        try:
            from .gx_market_info import get_instrument_identity as gx_identity

            return gx_identity(symbol, as_of)
        except Exception as exc:
            logger.warning("GX identity lookup failed for %s: %s", symbol, exc)
            return {}
    return {}
