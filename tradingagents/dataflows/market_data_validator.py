"""Deterministic market-data verification snapshot.

The market analyst is an LLM that can confabulate exact numbers — citing a
Bollinger band or a "historically validated bounce" that the underlying data
doesn't support (#830). This module computes a ground-truth snapshot (latest
OHLCV row on or before the analysis date, common indicators, recent closes)
the analyst is told to treat as the source of truth for any exact numeric
claim. Deterministic, no LLM involved.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
from stockstats import wrap

from tradingagents.dataflows.stockstats_utils import load_ohlcv

# A fixed, common indicator set so the snapshot is the same shape every run.
DEFAULT_SNAPSHOT_INDICATORS: tuple[str, ...] = (
    "close_10_ema", "close_50_sma", "close_200_sma",
    "rsi", "boll", "boll_ub", "boll_lb",
    "macd", "macds", "macdh", "atr",
)


def _verified_rows(symbol: str, curr_date: str) -> pd.DataFrame:
    """OHLCV on or before curr_date, date-sorted. Raises if nothing usable.

    ``load_ohlcv`` already normalizes the Date column and filters out
    look-ahead rows, but we re-apply the cutoff defensively — this is a
    verification path, so it must not trust its input to be pre-filtered.
    """
    data = load_ohlcv(symbol, curr_date)
    if data is None or data.empty:
        raise ValueError(f"No OHLCV data available for {symbol}.")

    df = data.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    cutoff_date = pd.to_datetime(curr_date).date()
    # Daily vendors may timestamp a completed candle at the exchange close
    # rather than midnight. Compare the session date so the requested day's
    # verified row is not incorrectly discarded.
    df = df[df["Date"].dt.date <= cutoff_date].sort_values("Date")
    if df.empty:
        raise ValueError(f"No OHLCV rows on or before {curr_date} for {symbol}.")
    return df


def get_verified_price_reference(symbol: str, curr_date: str) -> dict[str, Any]:
    """Return the frozen completed-session close used to validate a price target.

    This deliberately reuses the same strict OHLCV path as the verified market
    snapshot. It never uses a live quote, wall clock, or an LLM report, so a
    resumed run validates against the original analysis cutoff.
    """
    df = _verified_rows(symbol, curr_date)
    latest = df.iloc[-1]
    try:
        price = Decimal(str(latest.get("Close")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"No positive finite completed-session close for {symbol}."
        ) from exc
    if not price.is_finite() or price <= 0:
        raise ValueError(f"No positive finite completed-session close for {symbol}.")

    provenance = df.attrs.get("gx_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    currency = provenance.get("currency") or df.attrs.get("currency")
    price_unit = provenance.get("price_unit") or df.attrs.get("price_unit")
    from tradingagents.dataflows.config import get_config

    config = get_config()
    vendor = config.get("tool_vendors", {}).get(
        "get_stock_data",
        config.get("data_vendors", {}).get("core_stock_apis", "yfinance"),
    )
    configured_vendors = [
        item.strip() for item in str(vendor).split(",") if item.strip()
    ]
    sole_gx_vendor = configured_vendors == ["gx_market_info"]
    # With a fallback chain, an unprovenanced frame could have come from
    # Yahoo/another vendor. Never label it VND/exact merely because GX appears
    # somewhere in the configured chain. A sole GX route is deterministic and
    # may safely restore metadata lost by benign pandas transformations.
    if sole_gx_vendor:
        if not currency:
            currency = "VND"
        if not price_unit:
            price_unit = "VND"

    normalized_price = format(price.normalize(), "f")
    if "." in normalized_price:
        normalized_price = normalized_price.rstrip("0").rstrip(".")

    return {
        "status": "available",
        "ticker": str(symbol).strip().upper(),
        "close": normalized_price,
        "currency": str(currency).strip().upper() if currency else None,
        "price_unit": str(price_unit).strip() if price_unit else None,
        "session_date": pd.Timestamp(latest["Date"]).date().isoformat(),
        "analysis_cutoff": str(curr_date),
        "source": provenance.get(
            "source", "gx_market_info" if sole_gx_vendor else "unprovenanced_ohlcv"
        ),
        "point_in_time_quality": provenance.get(
            "point_in_time_quality", "exact" if sole_gx_vendor else "partial"
        ),
    }


def _fmt(value) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _live_quote_lines(symbol: str, analysis_cutoff: str) -> list[str]:
    """Render a GX quote separately from completed daily OHLCV.

    The quote is evidence at the frozen cutoff only.  It is deliberately never
    appended to ``df`` or ``stock_df``, so it cannot alter a daily indicator.
    """
    from tradingagents.dataflows.config import get_config

    config = get_config()
    vendor = config.get("tool_vendors", {}).get(
        "get_stock_data",
        config.get("data_vendors", {}).get("core_stock_apis", "yfinance"),
    )
    vendors = [item.strip() for item in str(vendor).split(",") if item.strip()]
    if vendors != ["gx_market_info"]:
        return [
            "### Live quote at frozen cutoff",
            "",
            "- Unavailable: live quote is only supported by the GX profile.",
        ]

    try:
        from tradingagents.dataflows.gx_market_info import get_gx_market_info_client

        client = get_gx_market_info_client()
        quote = client.get_quote(symbol, analysis_cutoff)
        meta: dict[str, Any] = client.provenance
    except Exception:  # noqa: BLE001 - optional evidence must not sink market analysis
        return [
            "### Live quote at frozen cutoff",
            "",
            "- Unavailable: GX had no usable quote at the frozen cutoff.",
        ]

    lines = [
        "### Live quote at frozen cutoff (not used in daily indicators)",
        "",
        f"- Cutoff: {analysis_cutoff}",
        f"- Source timestamp: {_fmt(meta.get('source_timestamp') or quote.get('source_updated_at'))}",
        f"- is_final: {str(bool(quote.get('is_final', False))).lower()}",
        "- Point-in-time quality: partial (latest retained quote snapshot)",
        "",
        "| Field | Value |",
        "|---|---:|",
    ]
    for field in (
        "last_price",
        "price_change",
        "price_change_percentage",
        "open_price",
        "high_price",
        "low_price",
        "total_matched_volume",
    ):
        lines.append(f"| {field} | {_fmt(quote.get(field))} |")
    return lines


def build_verified_market_snapshot(
    symbol: str,
    curr_date: str,
    look_back_days: int = 30,
    indicators: Iterable[str] | None = None,
    *,
    include_live_quote: bool = False,
) -> str:
    """Render a ground-truth snapshot: latest OHLCV row, indicators, recent closes."""
    # `df` keeps the original capitalized OHLCV columns (Open/High/Low/Close/
    # Volume); stockstats `wrap()` lowercases columns and adds indicator
    # columns, so read raw prices from `df` and indicators from `stock_df`.
    df = _verified_rows(symbol, curr_date)
    stock_df = wrap(df.copy())

    selected = tuple(indicators or DEFAULT_SNAPSHOT_INDICATORS)
    indicator_values: dict[str, str] = {}
    for name in selected:
        try:
            stock_df[name]  # triggers stockstats calculation
            indicator_values[name] = _fmt(stock_df.iloc[-1][name])
        except Exception as exc:  # noqa: BLE001 — one bad indicator shouldn't sink the snapshot
            indicator_values[name] = f"N/A ({type(exc).__name__})"

    latest = df.iloc[-1]
    latest_date = _fmt(latest["Date"])
    window = max(1, min(int(look_back_days), 30))
    recent = df.tail(window)

    lines = [
        f"## Verified market data snapshot for {symbol.upper()}",
        "",
        f"- Requested analysis date: {curr_date}",
        f"- Latest trading row used: {latest_date}",
        "- Rows after the requested analysis date are excluded before verification.",
        "",
        "### Latest verified OHLCV row",
        "",
        "| Field | Value |",
        "|---|---:|",
    ]
    for field in ("Open", "High", "Low", "Close", "Volume"):
        lines.append(f"| {field} | {_fmt(latest.get(field))} |")

    lines += ["", "### Verified technical indicators (latest row)", "",
              "| Indicator | Value |", "|---|---:|"]
    for name, value in indicator_values.items():
        lines.append(f"| {name} | {value} |")

    lines += ["", f"### Recent verified closes (last {len(recent)} rows)", "",
              "| Date | Close |", "|---|---:|"]
    for _, row in recent.iterrows():
        lines.append(f"| {_fmt(row['Date'])} | {_fmt(row.get('Close'))} |")

    if include_live_quote:
        lines += [""] + _live_quote_lines(symbol, curr_date)

    lines += [
        "",
        "Use this snapshot as the source of truth for exact OHLCV, price-level, "
        "and indicator-value claims. If another tool output conflicts with it, "
        "flag the discrepancy rather than inventing a reconciled number. Do not "
        "claim historical validation, support/resistance bounces, or exact "
        "percentage moves unless directly supported by tool output with concrete "
        "dates and prices.",
    ]
    return "\n".join(lines)
