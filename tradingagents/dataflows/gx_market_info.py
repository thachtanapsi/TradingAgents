"""Point-in-time data adapter for the GX Vietnam market-data platform.

The public vendor functions at the bottom of this module match
``dataflows.interface.VENDOR_METHODS``.  The classes above them expose a typed,
non-LLM API that the stage runner and health checks can use directly.

GX stores equity prices in VND internally while the TradingView UDF renders
candles in thousands of VND.  Both HTTP and PostgreSQL transports normalize
OHLC values back to VND at this boundary so every downstream consumer sees one
unit regardless of transport.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import pandas as pd
import requests
from stockstats import wrap

from .config import get_config
from .errors import NoMarketDataError, VendorNotConfiguredError, VendorRateLimitError

GX_VENDOR = "gx_market_info"
GX_SOURCE_NAME = "g_market_info_1229"
GX_TIMEZONE = "Asia/Ho_Chi_Minh"
GX_PRICE_SCALE = Decimal("1000")
_UTC = timezone.utc
_VN_TZ = timezone(timedelta(hours=7))
_ALLOWED_TRANSPORTS = frozenset({"api", "postgres"})
_ALLOWED_RESOLUTIONS = frozenset({"1", "5", "15", "30", "60", "240", "300", "1D", "1W", "1M"})
_PRICE_COLUMNS = ("Open", "High", "Low", "Close")
# GX cash-session identifiers observed after ATC/end-of-day processing.  A
# quote is only final when one of these markers (or a source update at/after
# close) is also consistent with the immutable cutoff and source timestamp.
_FINAL_QUOTE_SESSION_IDS = frozenset({"00", "60", "99"})


class GxMarketInfoError(RuntimeError):
    """GX returned a response that could not be used safely."""


class GxMarketInfoNotConfiguredError(VendorNotConfiguredError):
    """The selected GX transport is missing required connection settings."""


class GxMarketInfoRateLimitError(VendorRateLimitError):
    """The GX API throttled the request."""


def _now_vn() -> datetime:
    return datetime.now(_VN_TZ)


def _normalize_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("GX symbol must be a non-empty string")
    value = symbol.strip().upper()
    if ":" in value:
        value = value.split(":", 1)[1].strip()
    if value.endswith(".VN"):
        value = value[:-3]
    aliases = {
        "^VNINDEX": "VNINDEX",
        "VN-INDEX": "VNINDEX",
        "HNX": "HNXINDEX",
        "UPCOM": "HNXUPCOMINDEX",
    }
    return aliases.get(value, value)


def _parse_date(value: str | date | datetime, name: str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(_VN_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{name} must be YYYY-MM-DD or an ISO-8601 timestamp"
            ) from exc
        return parsed.astimezone(_VN_TZ).date() if parsed.tzinfo else parsed.date()


def _as_of_datetime(value: str | date | datetime | None) -> datetime:
    """Return an aware cutoff, interpreting date-only values as VN market close."""
    if value is None:
        return _now_vn()
    if isinstance(value, datetime):
        return value.replace(tzinfo=_VN_TZ) if value.tzinfo is None else value.astimezone(_VN_TZ)
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    "as_of must be YYYY-MM-DD or an ISO-8601 timestamp"
                ) from exc
            return (
                parsed.replace(tzinfo=_VN_TZ)
                if parsed.tzinfo is None
                else parsed.astimezone(_VN_TZ)
            )
    else:
        parsed_date = _parse_date(value, "as_of")
    # A date-only analysis means the completed VN cash session, never an
    # artificial end-of-day cutoff that could admit disclosures published later.
    return datetime.combine(parsed_date, time(15, 0), tzinfo=_VN_TZ)


def _explicit_as_of_datetime(value: str | date | datetime) -> datetime | None:
    """Return a frozen cutoff only when ``value`` actually contains a time."""
    if isinstance(value, datetime):
        return _as_of_datetime(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            date.fromisoformat(text)
        except ValueError:
            return _as_of_datetime(text)
    return None


def _as_of_iso(value: str | date | datetime | None) -> str:
    # ``auto`` preserves microseconds supplied by a frozen live StageSession
    # while keeping date-only close cutoffs compact and backward compatible.
    return _as_of_datetime(value).isoformat(timespec="auto")


def _quote_is_final(
    quote_data: Mapping[str, Any],
    as_of: str | date | datetime | None,
    meta: Mapping[str, Any] | None = None,
) -> bool:
    """Prove that a retained quote represents a completed VN cash session.

    This deliberately uses only the frozen cutoff and source evidence.  It
    never consults the current clock, so resuming a live run cannot change the
    answer.  Before 15:00 a quote is always non-final.  After close, the source
    timestamp must belong to that same session and either the GX session code
    must be post-ATC/end-of-day or the source itself must have been updated at
    or after 15:00.  Missing/ambiguous evidence fails closed.
    """
    cutoff = _as_of_datetime(as_of)
    if cutoff.time() < time(15, 0):
        return False

    metadata = meta or {}
    if metadata.get("session_completed_at_as_of") is False:
        return False
    raw_timestamp = (
        quote_data.get("source_updated_at")
        or metadata.get("source_timestamp")
    )
    if raw_timestamp is None:
        return False
    try:
        source_timestamp = datetime.fromisoformat(
            str(raw_timestamp).strip().replace("Z", "+00:00")
        )
    except ValueError:
        return False
    # stocks_price.last_updated is a UTC-naive database timestamp.
    if source_timestamp.tzinfo is None:
        source_timestamp = source_timestamp.replace(tzinfo=_UTC)
    source_vn = source_timestamp.astimezone(_VN_TZ)
    if source_vn > cutoff or source_vn.date() != cutoff.date():
        return False

    session_id = str(quote_data.get("trading_session_id") or "").strip()
    source_after_close = source_vn.time() >= time(15, 0)
    if session_id:
        return session_id in _FINAL_QUOTE_SESSION_IDS
    return source_after_close


def _with_quote_finality(
    quote_data: Mapping[str, Any],
    as_of: str | date | datetime | None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(quote_data)
    result["is_final"] = _quote_is_final(result, as_of, meta)
    return result


def _date_window(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
) -> tuple[date, date]:
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    return start, end


def _market_range_unix(start_date: date, end_date: date) -> tuple[int, int]:
    start = datetime.combine(start_date, time.min, tzinfo=_VN_TZ).astimezone(_UTC)
    end = datetime.combine(end_date, time(15, 0), tzinfo=_VN_TZ).astimezone(_UTC)
    return int(start.timestamp()), int(end.timestamp())


def _to_native(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _to_native(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_to_native(item) for item in value]
    return value


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _validate_statement_limit(value: Any) -> int:
    """Return a bounded integer page size for GX financial statements."""
    try:
        numeric = Decimal(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("limit must be an integer between 1 and 20") from exc
    if (
        not numeric.is_finite()
        or numeric != numeric.to_integral_value()
        or numeric < 1
        or numeric > 20
    ):
        raise ValueError("limit must be an integer between 1 and 20")
    return int(numeric)


def _first_present(record: Mapping[str, Any], keys: Sequence[str]) -> Any:
    return next((record.get(key) for key in keys if record.get(key) is not None), None)


def _sum_present(record: Mapping[str, Any], keys: Sequence[str]) -> Decimal | None:
    values = [_coerce_decimal(record.get(key)) for key in keys]
    present = [value for value in values if value is not None]
    return sum(present, Decimal("0")) if present else None


def _canonical_statement_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(record)
    row.update(
        {
            "period_start": row.get("period_start", row.get("startdate")),
            "period_end": row.get("period_end", row.get("enddate")),
            "fiscal_year": row.get("fiscal_year", row.get("yearreport")),
            "fiscal_period": row.get("fiscal_period", row.get("lengthreport")),
            "published_at": row.get("published_at", row.get("publicdate")),
            "audited": row.get("audited", row.get("isaudit")),
            "status": row.get("status"),
            "company_type": row.get("company_type", row.get("comtypecode")),
        }
    )
    return row


def _normalized_revenue(record: Mapping[str, Any]) -> Any:
    company_type = str(
        record.get("comtypecode") or record.get("company_type") or ""
    ).strip().upper()
    if company_type == "NH":
        direct = _first_present(record, ("isb38",))
        return direct if direct is not None else _sum_present(
            record,
            ("isb27", "isb30", "isb31", "isb32", "isb33", "isb36", "isb37"),
        )
    if company_type == "BH":
        return _first_present(record, ("isi64", "isi105", "isi103"))
    if company_type == "CK":
        direct = _first_present(record, ("isa3", "isa1"))
        return direct if direct is not None else _sum_present(
            record,
            (
                "iss115", "iss119", "iss120", "iss121", "iss122", "iss42",
                "iss44", "iss45", "iss46", "iss48", "iss47", "iss43",
                "iss49", "iss123", "iss50",
            ),
        )
    return _first_present(record, ("isa3", "isa1"))


def _normalize_income_statement(record: Mapping[str, Any]) -> dict[str, Any]:
    row = _canonical_statement_fields(record)
    row["normalized"] = {
        "revenue": _normalized_revenue(row),
        "profit_before_tax": row.get("isa16"),
        "net_income": row.get("isa20"),
        "parent_company_net_income": row.get("isa22"),
        "eps_basic": row.get("isa23"),
        "eps_diluted": row.get("isa24"),
    }
    return row


def _normalize_balance_sheet(record: Mapping[str, Any]) -> dict[str, Any]:
    row = _canonical_statement_fields(record)
    row["normalized"] = {
        "total_assets": row.get("bsa53"),
        "total_equity": row.get("bsa78"),
        "charter_capital": row.get("bsa80"),
    }
    return row


def _normalize_cash_flow(record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize FIIN cash-flow totals without discarding source fields.

    The field meanings below come from the supplied Navisoft/APG API data
    dictionary dated 2024-12-26.  Those CFA totals describe ordinary companies
    (``comtypecode=CT``).  Bank, securities-company, and insurer rows retain
    their non-null CFB/CFS/CFI fields, but are not forced into a corporate cash
    flow taxonomy whose equivalent totals have not been verified.
    """
    # A 242-column FIIN row is mostly null for any one company type. Keeping
    # only populated source values makes the LLM payload tractable while still
    # preserving every reported number and all audit/publication metadata.
    populated = {str(key).lower(): value for key, value in record.items() if value is not None}
    row = _canonical_statement_fields(populated)
    company_type = str(row.get("comtypecode") or row.get("company_type") or "").upper()
    normalized: dict[str, Any] = {}
    if company_type == "CT":
        operating = _coerce_decimal(row.get("cfa18"))
        capex = _coerce_decimal(row.get("cfa19"))
        normalized = {
            "net_cash_from_operating_activities": row.get("cfa18"),
            # CFA19 is the signed cash payment for fixed/other long-term assets.
            "capital_expenditures": row.get("cfa19"),
            "net_cash_from_investing_activities": row.get("cfa26"),
            "net_cash_from_financing_activities": row.get("cfa34"),
            "net_change_in_cash": row.get("cfa35"),
            "cash_and_cash_equivalents_beginning": row.get("cfa36"),
            "foreign_exchange_effect": row.get("cfa37"),
            "cash_and_cash_equivalents_ending": row.get("cfa38"),
            "free_cash_flow": operating + capex
            if operating is not None and capex is not None
            else None,
        }
    row.update(
        {
            "cash_flow_method": "direct" if row.get("isdirect") is True else (
                "indirect" if row.get("isdirect") is False else None
            ),
            "report_form_type": row.get("reportformtypecode"),
            "source_name": row.get("sourcename"),
            "created_at": row.get("createdate"),
            "updated_at": row.get("updatedate"),
            "normalized": normalized,
            "normalization_profile": (
                "fiin_cash_flow_ct_2024_12_26" if company_type == "CT" else "raw_only"
            ),
        }
    )
    return row


def _normalize_ratio_ttm(record: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(record)
    row["normalized"] = {
        "net_profit_margin": row.get("rtq29"),
        "nim": row.get("rtq44"),
        "yield_on_earning_assets": row.get("rtq45"),
        "cost_of_funds": row.get("rtq46"),
        "loans_to_deposits": row.get("rtq57"),
        "loan_loss_reserves_to_loans": row.get("rtq60"),
        "liquid_assets_to_total_assets": row.get("rtq120"),
        "customer_deposits_to_total_assets": row.get("rtq121"),
    }
    return row


def _normalize_ratio_daily(record: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(record)
    row["normalized"] = {
        "eps": row.get("rtd14"),
        "pe": row.get("rtd21"),
        "pb": row.get("rtd25"),
        "ps": row.get("rtd26"),
        "beta": row.get("rtd35"),
    }
    return row


def _canonicalize_fundamentals(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize API and PostgreSQL payloads to one stable domain contract."""
    overview = payload.get("overview")
    income = [
        _normalize_income_statement(row)
        for row in payload.get("income_statements", [])
        if isinstance(row, Mapping)
    ]
    balance = [
        _normalize_balance_sheet(row)
        for row in payload.get("balance_sheets", [])
        if isinstance(row, Mapping)
    ]
    raw_cash_flows = payload.get("cash_flow_statements", payload.get("cash_flows", []))
    cash_flows = [
        _normalize_cash_flow(row)
        for row in raw_cash_flows
        if isinstance(row, Mapping)
    ]
    ratios = payload.get("ratios") if isinstance(payload.get("ratios"), Mapping) else {}
    ratio_ttm = [
        _normalize_ratio_ttm(row)
        for row in ratios.get("ttm", [])
        if isinstance(row, Mapping)
    ]
    ratio_daily = [
        _normalize_ratio_daily(row)
        for row in ratios.get("daily", [])
        if isinstance(row, Mapping)
    ]
    company_type = next(
        (
            row.get("comtypecode") or row.get("company_type")
            for row in (*income, *balance, *cash_flows)
            if row.get("comtypecode") or row.get("company_type")
        ),
        payload.get("company_type"),
    )
    raw_unavailable = payload.get("unavailable")
    unavailable = dict(raw_unavailable) if isinstance(raw_unavailable, Mapping) else {}
    if cash_flows:
        unavailable.pop("cash_flow", None)
    else:
        unavailable.setdefault(
            "cash_flow",
            {
                "code": "NOT_MODELED",
                "reason": "Cash-flow data is not modeled or exposed by the selected GX transport.",
            },
        )
    result = {
        "overview": dict(overview) if isinstance(overview, Mapping) else None,
        "income_statements": income,
        "balance_sheets": balance,
        "cash_flow_statements": cash_flows,
        "ratios": {"ttm": ratio_ttm, "daily": ratio_daily},
        "unavailable": unavailable,
        "company_type": company_type,
        "coverage": {
            "overview": isinstance(overview, Mapping),
            "income_statement": bool(income),
            "balance_sheet": bool(balance),
            "ratios": bool(ratio_ttm or ratio_daily),
            "cash_flow": bool(cash_flows),
        },
    }
    return _to_native(result)


def _bound_to_last_completed_session(
    source: Any,
    requested_end: date,
    *,
    as_of: datetime | None = None,
) -> date:
    """Prevent current/future daily requests from consuming a partial session."""
    now = as_of.astimezone(_VN_TZ) if as_of is not None else _now_vn()
    if requested_end < now.date() or (
        requested_end == now.date() and now.time() >= time(15, 0)
    ):
        return requested_end
    value = source.get_last_trading_session(now)
    if not value:
        raise NoMarketDataError(
            "GX", "GX", "last completed trading session is unavailable"
        )
    return min(requested_end, _parse_date(value, "last_trading_session"))


def _normalize_ohlcv(
    rows: pd.DataFrame | Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    end_date: date,
    price_multiplier: Decimal = GX_PRICE_SCALE,
) -> pd.DataFrame:
    """Normalize transport rows to sorted, VND-denominated OHLCV."""
    frame = rows.copy() if isinstance(rows, pd.DataFrame) else pd.DataFrame(list(rows))
    if frame.empty:
        raise NoMarketDataError(symbol, symbol, f"no rows through {end_date.isoformat()}")

    date_column = next(
        (name for name in ("Date", "trading_date", "timestamp", "t") if name in frame.columns),
        None,
    )
    if date_column is not None:
        frame["Date"] = frame[date_column]

    aliases = {
        "o": "Open",
        "open": "Open",
        "open_price": "Open",
        "h": "High",
        "high": "High",
        "high_price": "High",
        "l": "Low",
        "low": "Low",
        "low_price": "Low",
        "c": "Close",
        "close": "Close",
        "close_price": "Close",
        "v": "Volume",
        "volume": "Volume",
        "trade_volumn": "Volume",
        "total_volume": "Volume",
    }
    frame = frame.rename(columns={col: aliases.get(str(col).lower(), col) for col in frame.columns})
    required = {"Date", "Close"}
    missing = required - set(frame.columns)
    if missing:
        raise GxMarketInfoError(f"GX OHLCV response missing columns: {sorted(missing)}")

    date_values = frame["Date"]
    numeric_dates = pd.to_numeric(date_values, errors="coerce")
    if numeric_dates.notna().all():
        parsed_dates = pd.to_datetime(numeric_dates, unit="s", utc=True, errors="coerce")
        parsed_dates = parsed_dates.dt.tz_convert(GX_TIMEZONE).dt.tz_localize(None)
    else:
        parsed_dates = pd.to_datetime(date_values, errors="coerce")
        if getattr(parsed_dates.dt, "tz", None) is not None:
            parsed_dates = parsed_dates.dt.tz_convert(GX_TIMEZONE).dt.tz_localize(None)
    frame["Date"] = parsed_dates

    for column in (*_PRICE_COLUMNS, "Volume"):
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    multiplier = float(price_multiplier)
    for column in _PRICE_COLUMNS:
        frame[column] = frame[column] * multiplier

    cutoff = pd.Timestamp(datetime.combine(end_date, time(15, 0)))
    frame = frame.dropna(subset=["Date", "Close"])
    frame = frame[frame["Date"] <= cutoff]
    frame = frame.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
    frame = frame[["Date", "Open", "High", "Low", "Close", "Volume"]].reset_index(drop=True)
    if frame.empty:
        raise NoMarketDataError(symbol, symbol, f"no rows on or before {end_date.isoformat()}")
    return frame


@runtime_checkable
class GxDataSource(Protocol):
    """Canonical point-in-time GX source implemented by API and PostgreSQL."""

    def get_ohlcv(
        self,
        symbol: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
        resolution: str = "1D",
    ) -> pd.DataFrame: ...

    def get_profile(self, symbol: str, as_of: str | date | datetime | None = None) -> dict[str, Any]: ...

    def get_quote(self, symbol: str, as_of: str | date | datetime | None = None) -> dict[str, Any]: ...

    def get_fundamentals(
        self,
        symbol: str,
        as_of: str | date | datetime | None = None,
        frequency: str = "quarterly",
        limit: int = 8,
    ) -> dict[str, Any]: ...

    def get_cashflow(
        self,
        symbol: str,
        as_of: str | date | datetime | None = None,
        frequency: str = "quarterly",
        limit: int = 8,
    ) -> list[dict[str, Any]]: ...

    def get_events(
        self,
        symbol: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...

    def get_index_history(
        self,
        index_code: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
    ) -> pd.DataFrame: ...

    def get_last_trading_session(
        self, as_of: str | date | datetime | None = None
    ) -> str | None: ...


class GxHttpDataSource:
    """Read-only GX REST client using the analysis-data and TradingView APIs."""

    def __init__(
        self,
        base_url: str,
        api_version: str = "v1.0.7",
        analysis_token: str | None = None,
        tradingview_token: str | None = None,
        timeout_seconds: float = 10.0,
        session: requests.Session | None = None,
    ):
        if not base_url or not str(base_url).strip():
            raise GxMarketInfoNotConfiguredError("GX API transport requires base_url")
        self.base_url = str(base_url).rstrip("/")
        self.api_version = str(api_version).strip("/")
        self.analysis_token = analysis_token or ""
        self.tradingview_token = tradingview_token or ""
        self.timeout_seconds = float(timeout_seconds)
        self.session = session or requests.Session()
        self.last_meta: dict[str, Any] = {}

    @property
    def _analysis_base(self) -> str:
        return f"{self.base_url}/trade/api/{self.api_version}/analysis-data"

    @property
    def _tradingview_base(self) -> str:
        return f"{self.base_url}/trade/api/{self.api_version}/tradingview/datafeed"

    def _request_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any],
        token_kind: str,
        symbol: str | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if token_kind == "analysis" and self.analysis_token:
            headers["Authorization"] = f"Bearer {self.analysis_token}"
        elif token_kind == "tradingview" and self.tradingview_token:
            headers["x-tv-token"] = self.tradingview_token
        try:
            response = self.session.get(
                url,
                params={key: value for key, value in params.items() if value is not None},
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise GxMarketInfoError(f"GX request timed out after {self.timeout_seconds:g}s") from exc
        except requests.RequestException as exc:
            # Request exceptions may embed URLs or adapter diagnostics. Keep
            # those on the exception chain, never in user-facing output.
            raise GxMarketInfoError("GX API request failed") from exc

        if response.status_code == 429:
            raise GxMarketInfoRateLimitError("GX API rate limit exceeded")
        if response.status_code in {401, 403}:
            raise GxMarketInfoNotConfiguredError(
                "GX API rejected the configured analysis/TradingView credential"
            )
        if response.status_code == 404:
            raise NoMarketDataError(symbol or "GX", symbol, "GX API returned 404")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GxMarketInfoError("GX API returned invalid JSON") from exc
        if response.status_code >= 400:
            raise GxMarketInfoError(f"GX API returned HTTP {response.status_code}")
        return payload

    def _analysis_data(self, path: str, *, params: Mapping[str, Any], symbol: str | None = None) -> Any:
        payload = self._request_json(
            f"{self._analysis_base}/{path.lstrip('/')}",
            params=params,
            token_kind="analysis",
            symbol=symbol,
        )
        if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
            raise GxMarketInfoError("GX analysis-data response has an unsupported envelope")
        if payload.get("error"):
            error = payload["error"]
            code = error.get("code") if isinstance(error, Mapping) else "ANALYSIS_DATA_ERROR"
            raise NoMarketDataError(symbol or "GX", symbol, str(code))
        self.last_meta = dict(payload.get("meta") or {})
        self.last_meta.setdefault("source", GX_SOURCE_NAME)
        self.last_meta["transport"] = "api"
        return payload.get("data")

    def get_ohlcv(
        self,
        symbol: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
        resolution: str = "1D",
    ) -> pd.DataFrame:
        symbol = _normalize_symbol(symbol)
        requested_cutoff = _explicit_as_of_datetime(end_date)
        start, end = _date_window(start_date, end_date)
        end = _bound_to_last_completed_session(self, end, as_of=requested_cutoff)
        if start > end:
            raise NoMarketDataError(symbol, symbol, "no completed session in requested range")
        if resolution not in _ALLOWED_RESOLUTIONS:
            raise ValueError(f"unsupported GX resolution: {resolution}")

        if symbol in {"VNINDEX", "HNXINDEX", "HNXUPCOMINDEX"}:
            if resolution != "1D":
                raise ValueError("GX index history currently supports only resolution=1D")
            return self.get_index_history(symbol, start, requested_cutoff or end)

        from_ts, to_ts = _market_range_unix(start, end)
        payload = self._request_json(
            f"{self._tradingview_base}/history",
            params={
                "symbol": symbol,
                "from": from_ts,
                "to": to_ts,
                "resolution": resolution,
            },
            token_kind="tradingview",
            symbol=symbol,
        )
        if not isinstance(payload, Mapping):
            raise GxMarketInfoError("GX TradingView history returned an invalid response")
        if payload.get("s") == "no_data":
            raise NoMarketDataError(symbol, symbol, f"no rows between {start} and {end}")
        if payload.get("s") != "ok":
            raise GxMarketInfoError(f"GX TradingView history error: {payload.get('errmsg', payload)}")
        columns = {key: payload.get(key, []) for key in ("t", "o", "h", "l", "c", "v")}
        lengths = {len(value) for value in columns.values() if isinstance(value, list)}
        if len(lengths) != 1:
            raise GxMarketInfoError("GX TradingView history arrays have inconsistent lengths")
        frame = _normalize_ohlcv(pd.DataFrame(columns), symbol=symbol, end_date=end)
        self.last_meta = {
            "source": GX_SOURCE_NAME,
            "transport": "api",
            "timezone": GX_TIMEZONE,
            "currency": "VND",
            "price_unit": "VND",
            "resolution": resolution,
            "as_of": _as_of_iso(requested_cutoff or end),
            "point_in_time_quality": "exact",
        }
        frame.attrs["gx_provenance"] = deepcopy(self.last_meta)
        return frame

    def get_profile(self, symbol: str, as_of: str | date | datetime | None = None) -> dict[str, Any]:
        symbol = _normalize_symbol(symbol)
        data = self._analysis_data(
            f"stocks/{quote(symbol, safe='')}/profile",
            params={"as_of": _as_of_iso(as_of)},
            symbol=symbol,
        )
        if not isinstance(data, Mapping):
            raise NoMarketDataError(symbol, symbol, "GX profile unavailable")
        return dict(data)

    def get_quote(self, symbol: str, as_of: str | date | datetime | None = None) -> dict[str, Any]:
        symbol = _normalize_symbol(symbol)
        data = self._analysis_data(
            f"stocks/{quote(symbol, safe='')}/quote",
            params={"as_of": _as_of_iso(as_of)},
            symbol=symbol,
        )
        if not isinstance(data, Mapping):
            raise NoMarketDataError(symbol, symbol, "GX quote unavailable at cutoff")
        return _with_quote_finality(data, as_of, self.last_meta)

    def get_fundamentals(
        self,
        symbol: str,
        as_of: str | date | datetime | None = None,
        frequency: str = "quarterly",
        limit: int = 8,
    ) -> dict[str, Any]:
        symbol = _normalize_symbol(symbol)
        limit = _validate_statement_limit(limit)
        data = self._analysis_data(
            f"stocks/{quote(symbol, safe='')}/fundamentals",
            params={
                "as_of": _as_of_iso(as_of),
                "frequency": frequency,
                "limit": limit,
            },
            symbol=symbol,
        )
        return dict(data) if isinstance(data, Mapping) else {}

    def get_cashflow(
        self,
        symbol: str,
        as_of: str | date | datetime | None = None,
        frequency: str = "quarterly",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        symbol = _normalize_symbol(symbol)
        payload = self.get_fundamentals(symbol, as_of, frequency, limit)
        data = payload.get("cash_flow_statements", payload.get("cash_flows", []))
        rows = [dict(row) for row in data if isinstance(row, Mapping)]
        if not rows:
            gap = payload.get("unavailable", {}).get("cash_flow", {})
            reason = gap.get("reason") if isinstance(gap, Mapping) else None
            raise NoMarketDataError(
                symbol,
                symbol,
                reason or "GX API does not expose cash-flow statements",
            )
        return rows

    def get_events(
        self,
        symbol: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        symbol = _normalize_symbol(symbol)
        requested_cutoff = _explicit_as_of_datetime(end_date)
        start, end = _date_window(start_date, end_date)
        data = self._analysis_data(
            f"stocks/{quote(symbol, safe='')}/events",
            params={
                "as_of": _as_of_iso(requested_cutoff or end),
                "from": start.isoformat(),
                "to": end.isoformat(),
                "limit": int(limit),
            },
            symbol=symbol,
        )
        return [dict(row) for row in data] if isinstance(data, list) else []

    def get_index_history(
        self,
        index_code: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
    ) -> pd.DataFrame:
        index_code = _normalize_symbol(index_code)
        requested_cutoff = _explicit_as_of_datetime(end_date)
        start, end = _date_window(start_date, end_date)
        end = _bound_to_last_completed_session(self, end, as_of=requested_cutoff)
        if start > end:
            raise NoMarketDataError(index_code, index_code, "no completed session in requested range")
        data = self._analysis_data(
            f"indexes/{quote(index_code, safe='')}/history",
            params={
                "as_of": _as_of_iso(requested_cutoff or end),
                "from": start.isoformat(),
                "to": end.isoformat(),
                "resolution": "1D",
            },
            symbol=index_code,
        )
        rows = data if isinstance(data, list) else []
        # Index values are points, not thousand-VND equity prices.
        frame = _normalize_ohlcv(
            rows,
            symbol=index_code,
            end_date=end,
            price_multiplier=Decimal("1"),
        )
        frame.attrs["gx_provenance"] = deepcopy(self.last_meta)
        return frame

    def get_last_trading_session(
        self, as_of: str | date | datetime | None = None
    ) -> str | None:
        data = self._analysis_data(
            "calendar/last-session",
            params={"as_of": _as_of_iso(as_of)},
        )
        if not isinstance(data, Mapping):
            return None
        value = data.get("trading_date") or data.get("trade_date")
        return str(value) if value else None


class GxPostgresDataSource:
    """Read-only PostgreSQL implementation of :class:`GxDataSource`.

    ``psycopg`` is imported lazily so API-only installations do not require it.
    The configured database role should itself be read-only; this class also
    opens read-only transactions and never interpolates user data into SQL.
    """

    def __init__(
        self,
        dsn: str,
        connect_timeout_seconds: float = 10.0,
        expected_database: str = GX_SOURCE_NAME,
        statement_timeout_ms: int = 15_000,
        connector: Any | None = None,
    ):
        if not dsn or not str(dsn).strip():
            raise GxMarketInfoNotConfiguredError("GX PostgreSQL transport requires dsn")
        if not expected_database or not str(expected_database).strip():
            raise GxMarketInfoNotConfiguredError(
                "GX PostgreSQL transport requires expected_database"
            )
        if int(statement_timeout_ms) <= 0:
            raise ValueError("statement_timeout_ms must be positive")
        self.dsn = str(dsn)
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.expected_database = str(expected_database)
        self.statement_timeout_ms = int(statement_timeout_ms)
        self._connector = connector
        self.last_meta: dict[str, Any] = {}

    def _set_meta(self, **values: Any) -> None:
        self.last_meta = {
            "source": GX_SOURCE_NAME,
            "transport": "postgres",
            "timezone": GX_TIMEZONE,
            **values,
        }

    def _connect(self):
        connect_options = {
            "connect_timeout": self.connect_timeout_seconds,
            "application_name": "tradingagents-gx",
            "options": (
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={self.statement_timeout_ms}"
            ),
        }
        if self._connector is not None:
            return self._connector(self.dsn, **connect_options)
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise GxMarketInfoNotConfiguredError(
                "GX PostgreSQL transport requires the optional 'psycopg[binary]' dependency"
            ) from exc
        return psycopg.connect(
            self.dsn,
            **connect_options,
            row_factory=dict_row,
        )

    def _validate_connection(self, connection) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database() AS database_name, "
                "current_setting('transaction_read_only') AS transaction_read_only"
            )
            row = cursor.fetchone()
        if isinstance(row, Mapping):
            database_name = row.get("database_name")
            read_only = row.get("transaction_read_only")
        else:
            database_name, read_only = row[0], row[1]
        if database_name != self.expected_database:
            raise GxMarketInfoNotConfiguredError(
                "GX PostgreSQL connection points to an unexpected database"
            )
        if str(read_only).lower() not in {"on", "true", "1"}:
            raise GxMarketInfoNotConfiguredError(
                "GX PostgreSQL connection is not transaction read-only"
            )

    def _query(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        normalized_sql = " ".join(sql.lower().split())
        operation = next(
            (
                name
                for name in (
                    "stocks_info_ext",
                    "fiin_incomestatement",
                    "fiin_balancesheet",
                    "fiin_cashflow",
                    "fiin_ratiottmdaily",
                    "fiin_ratiottm",
                    "fiin_event",
                    "index_history",
                    "stocks_price",
                    "stocks_info",
                    "calendar",
                    "get_tradingview_candles",
                )
                if name in normalized_sql
            ),
            "read",
        )
        try:
            with self._connect() as connection:
                if hasattr(connection, "read_only"):
                    connection.read_only = True
                self._validate_connection(connection)
                with connection.cursor() as cursor:
                    cursor.execute(sql, tuple(params))
                    rows = cursor.fetchall()
                    columns = [item.name if hasattr(item, "name") else item[0] for item in cursor.description]
        except GxMarketInfoNotConfiguredError:
            raise
        except Exception:  # psycopg is optional, avoid importing its exception hierarchy
            # Do not include the driver exception in the public message: it may
            # contain a DSN, host, username, or server diagnostics.
            raise GxMarketInfoError(
                f"GX PostgreSQL query failed during {operation}"
            ) from None
        result = []
        for row in rows:
            if isinstance(row, Mapping):
                result.append(dict(row))
            else:
                result.append(dict(zip(columns, row, strict=False)))
        return result

    def _query_json_records(
        self, sql: str, params: Sequence[Any]
    ) -> list[dict[str, Any]]:
        """Read wide GX rows without decoding PostgreSQL infinity timestamps.

        Several production FIIN tables contain ``-infinity`` in auxiliary
        timestamp columns such as ``integrateddate``. Psycopg correctly refuses
        to coerce those values into Python's bounded ``datetime``. PostgreSQL's
        JSON serializer keeps them as strings, so retrieve wide records as JSON
        text, parse decimals without binary-float loss, and canonicalize special
        timestamp sentinels to missing values at the datasource boundary.

        The flat-row branch keeps connector fakes and older compatible database
        facades working while production SQL always returns ``payload``.
        """
        records: list[dict[str, Any]] = []
        for row in self._query(sql, params):
            payload: Any = row.get("payload", row)
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload, parse_float=Decimal)
                except (TypeError, ValueError) as exc:
                    raise GxMarketInfoError(
                        "GX PostgreSQL returned an invalid JSON record"
                    ) from exc
            if not isinstance(payload, Mapping):
                raise GxMarketInfoError(
                    "GX PostgreSQL returned a non-object JSON record"
                )
            records.append(self._normalize_postgres_sentinels(dict(payload)))
        return records

    @classmethod
    def _normalize_postgres_sentinels(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: cls._normalize_postgres_sentinels(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._normalize_postgres_sentinels(item) for item in value]
        if isinstance(value, str) and value.strip().lower() in {
            "-infinity",
            "infinity",
            "nan",
        }:
            return None
        return value

    def get_ohlcv(
        self,
        symbol: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
        resolution: str = "1D",
    ) -> pd.DataFrame:
        symbol = _normalize_symbol(symbol)
        requested_cutoff = _explicit_as_of_datetime(end_date)
        start, end = _date_window(start_date, end_date)
        end = _bound_to_last_completed_session(self, end, as_of=requested_cutoff)
        if start > end:
            raise NoMarketDataError(symbol, symbol, "no completed session in requested range")
        if resolution not in _ALLOWED_RESOLUTIONS:
            raise ValueError(f"unsupported GX resolution: {resolution}")
        if symbol in {"VNINDEX", "HNXINDEX", "HNXUPCOMINDEX"}:
            if resolution != "1D":
                raise ValueError("GX index history currently supports only resolution=1D")
            return self.get_index_history(symbol, start, requested_cutoff or end)
        from_ts, to_ts = _market_range_unix(start, end)
        rows = self._query(
            "SELECT t, o, h, l, c, v FROM public.get_tradingview_candles(%s, %s, %s, %s)",
            [symbol, from_ts, to_ts, resolution],
        )
        frame = _normalize_ohlcv(rows, symbol=symbol, end_date=end)
        self._set_meta(
            currency="VND",
            price_unit="VND",
            resolution=resolution,
            as_of=_as_of_iso(requested_cutoff or end),
            point_in_time_quality="exact",
        )
        frame.attrs["gx_provenance"] = deepcopy(self.last_meta)
        return frame

    def get_profile(self, symbol: str, as_of: str | date | datetime | None = None) -> dict[str, Any]:
        symbol = _normalize_symbol(symbol)
        rows = self._query(
            """
            SELECT si.ticker_code AS ticker, si.symbol AS isin, si.symbol_name AS name,
                   si.symbol_english_name AS name_en, si.market_id, si.security_exchange,
                   si.product_id, si.short_name, si.short_name_en,
                   si.security_group_id, si.product_grp_id AS product_group_id,
                   si.currency, si.listed_shares,
                   si.outstandingshare_daily AS outstanding_shares,
                   si.sector_type_code, si.first_trading_date,
                   si.security_trading_status, si.updated_at AS instrument_updated_at,
                   org.organcode AS organization_code, org.comtypecode AS company_type,
                   org.organname AS organization_name,
                   org.en_organname AS organization_name_en,
                   org.organshortname AS organization_short_name,
                   org.en_organshortname AS organization_short_name_en,
                   org.listingdate AS listing_date, org.chartercapital AS charter_capital,
                   org.freefloat AS free_float, org.freefloatrate AS free_float_rate,
                   org.issueshare AS issued_shares,
                   org.outstandingshare AS organization_outstanding_shares,
                   org.accountingperiod AS accounting_period,
                   org.companyprofile AS company_profile,
                   org.en_companyprofile AS company_profile_en,
                   org.businessline AS business_line,
                   org.en_businessline AS business_line_en,
                   org.primaryproduct AS primary_product,
                   org.en_primaryproduct AS primary_product_en,
                   org.icbcode AS industry_code, industry.icbname AS industry_name,
                   industry.en_icbname AS industry_name_en,
                   industry.icblevel AS industry_level,
                   org.updatedate AS organization_updated_at
            FROM stocks_info si
            LEFT JOIN fiin_organization org
              ON org.ticker = si.ticker_code AND org.status = 1
            LEFT JOIN fiin_icbindustry industry
              ON industry.icbcode = org.icbcode AND industry.status = 1
            WHERE si.ticker_code = %s AND si.is_active = true
            ORDER BY org.updatedate DESC NULLS LAST
            LIMIT 1
            """,
            [symbol],
        )
        if not rows:
            raise NoMarketDataError(symbol, symbol, "GX profile unavailable")
        self._set_meta(
            ticker=symbol,
            price_unit="VND",
            point_in_time_quality="current_only",
            as_of=_as_of_iso(as_of),
        )
        return _to_native(rows[0])

    def get_quote(self, symbol: str, as_of: str | date | datetime | None = None) -> dict[str, Any]:
        symbol = _normalize_symbol(symbol)
        cutoff = _as_of_datetime(as_of).astimezone(_UTC).replace(tzinfo=None)
        rows = self._query(
            """
            SELECT si.ticker_code AS ticker, si.symbol AS isin, si.symbol_name AS name,
                   si.market_id,
                   COALESCE(si.currency, 'VND') AS currency, sp.board_id,
                   sp.trading_session_id, si.reference_price, si.ceiling_price,
                   si.floor_price, si.previous_close_price,
                   sp.last_matched_price AS last_price,
                   sp.last_matched_volume AS last_volume,
                   sp.last_matched_change AS price_change,
                   sp.price_change_percentage, sp.opening_price_day AS open_price,
                   sp.highest_price_day AS high_price, sp.lowest_price_day AS low_price,
                   sp.total_matched_volume, sp.total_matched_value,
                   sp.bid_price_1, sp.bid_volume_1, sp.bid_price_2, sp.bid_volume_2,
                   sp.bid_price_3, sp.bid_volume_3,
                   sp.ask_price_1, sp.ask_volume_1, sp.ask_price_2, sp.ask_volume_2,
                   sp.ask_price_3, sp.ask_volume_3,
                   sp.foreign_buy_volume_board AS foreign_buy_volume,
                   sp.foreign_sell_volume_board AS foreign_sell_volume,
                   sp.foreign_buy_value_board AS foreign_buy_value,
                   sp.foreign_sell_value_board AS foreign_sell_value,
                   sp.last_sequence_x AS sequence,
                   sp.last_updated AS source_updated_at
            FROM stocks_price sp
            JOIN stocks_info si ON sp.symbol = si.symbol
            WHERE si.ticker_code = %s AND si.is_active = true
              AND sp.board_id = 'G1' AND sp.last_updated IS NOT NULL
              AND sp.last_updated <= %s
            ORDER BY sp.last_updated DESC LIMIT 1
            """,
            [symbol, cutoff],
        )
        if not rows:
            raise NoMarketDataError(symbol, symbol, "GX quote unavailable at cutoff")
        self._set_meta(
            ticker=symbol,
            currency="VND",
            price_unit="VND",
            point_in_time_quality="partial",
            as_of=_as_of_iso(as_of),
        )
        result = _with_quote_finality(_to_native(rows[0]), as_of)
        for field in (
            "reference_price", "ceiling_price", "floor_price", "previous_close_price",
            "last_price", "price_change", "open_price", "high_price", "low_price",
        ):
            value = _coerce_decimal(result.get(field))
            if value is not None:
                result[field] = str(value)
        return result

    @staticmethod
    def _frequency_lengths(frequency: str) -> tuple[int, ...]:
        if frequency == "quarterly":
            return (1, 2, 3, 4)
        if frequency == "annual":
            return (5,)
        if frequency == "all":
            return (1, 2, 3, 4, 5)
        raise ValueError("frequency must be quarterly, annual or all")

    def get_fundamentals(
        self,
        symbol: str,
        as_of: str | date | datetime | None = None,
        frequency: str = "quarterly",
        limit: int = 8,
    ) -> dict[str, Any]:
        symbol = _normalize_symbol(symbol)
        limit = _validate_statement_limit(limit)
        cutoff = _as_of_datetime(as_of).replace(tzinfo=None)
        lengths = self._frequency_lengths(frequency)
        overview = self._query_json_records(
            """
            SELECT to_jsonb(overview_row)::text AS payload
            FROM stocks_info_ext AS overview_row
            WHERE ticker_code = %s AND trading_date <= %s
            ORDER BY trading_date DESC, year_report_cal DESC NULLS LAST LIMIT 1
            """,
            [symbol, cutoff.date()],
        )
        income = self._query_json_records(
            """
            SELECT to_jsonb(income_row)::text AS payload
            FROM fiin_incomestatement AS income_row
            WHERE ticker = %s AND status = 1 AND publicdate IS NOT NULL
              AND publicdate <= %s
              AND createdate IS NOT NULL AND createdate <= %s
              AND (updatedate IS NULL OR updatedate <= %s)
              AND lengthreport = ANY(%s)
            ORDER BY yearreport DESC, lengthreport DESC, publicdate DESC,
                     updatedate DESC NULLS LAST LIMIT %s
            """,
            [symbol, cutoff, cutoff, cutoff, list(lengths), limit * 8],
        )
        balance = self._query_json_records(
            """
            SELECT to_jsonb(balance_row)::text AS payload
            FROM fiin_balancesheet AS balance_row
            WHERE ticker = %s AND status = 1 AND publicdate IS NOT NULL
              AND publicdate <= %s
              AND createdate IS NOT NULL AND createdate <= %s
              AND (updatedate IS NULL OR updatedate <= %s)
              AND lengthreport = ANY(%s)
            ORDER BY yearreport DESC, lengthreport DESC, publicdate DESC,
                     updatedate DESC NULLS LAST LIMIT %s
            """,
            [symbol, cutoff, cutoff, cutoff, list(lengths), limit * 8],
        )
        cash_flows = self._query_cashflow_rows(symbol, cutoff, lengths, limit)
        income = self._deduplicate_statement_periods(income, limit)
        balance = self._deduplicate_statement_periods(balance, limit)
        ratio_ttm = self._query_json_records(
            """
            SELECT to_jsonb(ratio_row)::text AS payload
            FROM fiin_ratiottm AS ratio_row
            WHERE ticker = %s AND status = 1 AND indexbasis = 'TTM'
              AND tradingdate IS NOT NULL AND tradingdate <= %s
              AND updatedate IS NOT NULL AND updatedate <= %s
            ORDER BY yearreportcal DESC NULLS LAST,
                     lengthreportcal DESC NULLS LAST,
                     yearreport DESC, lengthreport DESC,
                     tradingdate DESC, updatedate DESC
            LIMIT %s
            """,
            [symbol, cutoff, cutoff, limit * 8],
        )
        ratio_daily = self._query_json_records(
            """
            SELECT to_jsonb(ratio_daily_row)::text AS payload
            FROM fiin_ratiottmdaily AS ratio_daily_row
            WHERE ticker = %s AND status = 1 AND indexbasis = 'TTM'
              AND tradingdate IS NOT NULL AND tradingdate <= %s
              AND updatedate IS NOT NULL AND updatedate <= %s
            ORDER BY yearreportcal DESC NULLS LAST,
                     lengthreportcal DESC NULLS LAST,
                     yearreport DESC, lengthreport DESC,
                     tradingdate DESC, updatedate DESC
            LIMIT %s
            """,
            [symbol, cutoff.date(), cutoff, limit * 8],
        )
        ratio_ttm = self._deduplicate_ratio_periods(ratio_ttm, limit)
        ratio_daily = self._deduplicate_ratio_periods(ratio_daily, limit)
        self._set_meta(
            ticker=symbol,
            currency="VND",
            price_unit="VND",
            point_in_time_quality="partial",
            cash_flow_monetary_unit="source_reported; not inferred",
            cash_flow_data_dictionary="Navisoft (APG) API description 2024-12-26",
            as_of=_as_of_iso(as_of),
        )
        unavailable = {}
        if not cash_flows:
            unavailable["cash_flow"] = {
                "code": "NO_DATA_AT_CUTOFF",
                "reason": "No eligible fiin_cashflow row was published by the requested cutoff.",
            }
        return _to_native(
            {
                "overview": overview[0] if overview else None,
                "income_statements": income,
                "balance_sheets": balance,
                "cash_flow_statements": cash_flows,
                "ratios": {"ttm": ratio_ttm, "daily": ratio_daily},
                "unavailable": unavailable,
            }
        )

    def get_cashflow(
        self,
        symbol: str,
        as_of: str | date | datetime | None = None,
        frequency: str = "quarterly",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        symbol = _normalize_symbol(symbol)
        limit = _validate_statement_limit(limit)
        cutoff = _as_of_datetime(as_of).replace(tzinfo=None)
        lengths = self._frequency_lengths(frequency)
        rows = self._query_cashflow_rows(symbol, cutoff, lengths, limit)
        if not rows:
            raise NoMarketDataError(
                symbol,
                symbol,
                "no eligible fiin_cashflow row was published by the requested cutoff",
            )
        self._set_cashflow_meta(symbol, as_of)
        return _to_native([_normalize_cash_flow(row) for row in rows])

    def _query_cashflow_rows(
        self,
        symbol: str,
        cutoff: datetime,
        lengths: Sequence[int],
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = self._query_json_records(
            """
            SELECT to_jsonb(cashflow_row)::text AS payload
            FROM fiin_cashflow AS cashflow_row
            WHERE cashflow_row.ticker = %s AND cashflow_row.status = 1
              AND cashflow_row.publicdate IS NOT NULL
              AND isfinite(cashflow_row.publicdate)
              AND cashflow_row.publicdate <= %s
              AND cashflow_row.createdate IS NOT NULL
              AND isfinite(cashflow_row.createdate)
              AND cashflow_row.createdate <= %s
              AND (
                    cashflow_row.updatedate IS NULL
                    OR (
                        isfinite(cashflow_row.updatedate)
                        AND cashflow_row.updatedate <= %s
                    )
                  )
              AND cashflow_row.lengthreport = ANY(%s)
            ORDER BY cashflow_row.yearreport DESC,
                     cashflow_row.lengthreport DESC,
                     cashflow_row.publicdate DESC,
                     cashflow_row.updatedate DESC NULLS LAST,
                     cashflow_row.createdate DESC NULLS LAST,
                     cashflow_row.cashflowid DESC
            LIMIT %s
            """,
            [symbol, cutoff, cutoff, cutoff, list(lengths), limit * 8],
        )
        return self._deduplicate_statement_periods(rows, limit)

    def _set_cashflow_meta(
        self,
        symbol: str,
        as_of: str | date | datetime | None,
    ) -> None:
        self._set_meta(
            ticker=symbol,
            currency="VND",
            monetary_unit="source_reported; not inferred",
            point_in_time_quality="partial",
            point_in_time_detail=(
                "public_created_updated_at_or_before_as_of; latest_revision_only"
            ),
            as_of=_as_of_iso(as_of),
            data_dictionary="Navisoft (APG) API description 2024-12-26",
        )

    @staticmethod
    def _deduplicate_statement_periods(
        rows: Sequence[Mapping[str, Any]], limit: int
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any]] = set()
        for row in rows:
            key = (row.get("yearreport"), row.get("lengthreport"))
            if key in seen:
                continue
            seen.add(key)
            result.append(dict(row))
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _deduplicate_ratio_periods(
        rows: Sequence[Mapping[str, Any]], limit: int
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any]] = set()
        for row in rows:
            key = (
                row.get("yearreportcal") or row.get("yearreport"),
                row.get("lengthreportcal") or row.get("lengthreport"),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(dict(row))
            if len(result) >= limit:
                break
        return result

    def get_events(
        self,
        symbol: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        symbol = _normalize_symbol(symbol)
        requested_cutoff = _explicit_as_of_datetime(end_date)
        start, end = _date_window(start_date, end_date)
        cutoff = _as_of_datetime(requested_cutoff or end).replace(tzinfo=None)
        rows = self._query(
            """
            SELECT eventid, ticker, eventtitle, en_eventtitle, eventdescription,
                   en_eventdescription, publicdate, recorddate, exrightdate,
                   issuedate, eventlistcode, ratio, value, sourceurl
            FROM fiin_event
            WHERE ticker = %s AND status = 1 AND publicdate IS NOT NULL
              AND publicdate >= %s AND publicdate <= %s
              AND createdate IS NOT NULL AND createdate <= %s
              AND (updatedate IS NULL OR updatedate <= %s)
            ORDER BY publicdate DESC, updatedate DESC NULLS LAST, eventid DESC
            LIMIT %s
            """,
            [
                symbol,
                datetime.combine(start, time.min),
                cutoff,
                cutoff,
                cutoff,
                int(limit),
            ],
        )
        self._set_meta(
            ticker=symbol,
            # Filtering creation/update timestamps prevents future rows, but
            # the source stores only the latest revision rather than a full
            # event revision history.
            point_in_time_quality="partial",
            point_in_time_detail="public_created_updated_at_or_before_as_of; latest_revision_only",
            as_of=_as_of_iso(requested_cutoff or end),
        )
        return _to_native(rows)

    def get_index_history(
        self,
        index_code: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
    ) -> pd.DataFrame:
        index_code = _normalize_symbol(index_code)
        requested_cutoff = _explicit_as_of_datetime(end_date)
        start, end = _date_window(start_date, end_date)
        end = _bound_to_last_completed_session(self, end, as_of=requested_cutoff)
        if start > end:
            raise NoMarketDataError(index_code, index_code, "no completed session in requested range")
        start_utc = datetime.combine(start, time.min, tzinfo=_VN_TZ).astimezone(_UTC)
        cutoff_utc = _as_of_datetime(requested_cutoff or end).astimezone(_UTC)
        rows = self._query(
            """
            SELECT DISTINCT ON (ih.trading_date)
                   ih.trading_date AS "Date", ih.index_value AS "Close",
                   ih.total_volume_traded AS "Volume"
            FROM index_history ih
            LEFT JOIN stock_groups sg
              ON ih.index_code = sg.group_code AND ih.market_id = sg.market_id
            WHERE (upper(ih.index_code) = %s OR upper(sg.group_code) = %s
                   OR upper(sg.group_name) = %s)
              AND ih.trading_date BETWEEN %s AND %s
              AND ih.transact_time IS NOT NULL
              AND ih.transact_time >= %s AND ih.transact_time <= %s
            ORDER BY ih.trading_date ASC, ih.transact_time DESC, ih.msg_seq_num DESC
            """,
            [index_code, index_code, index_code, start, end, start_utc, cutoff_utc],
        )
        frame = _normalize_ohlcv(
            rows, symbol=index_code, end_date=end, price_multiplier=Decimal("1")
        )
        self._set_meta(
            index_code=index_code,
            price_unit="index_points",
            resolution="1D",
            point_in_time_quality="exact",
            as_of=_as_of_iso(requested_cutoff or end),
        )
        frame.attrs["gx_provenance"] = deepcopy(self.last_meta)
        return frame

    def get_last_trading_session(
        self, as_of: str | date | datetime | None = None
    ) -> str | None:
        cutoff = _as_of_datetime(as_of)
        market_date = cutoff.date()
        if cutoff.time() < time(15, 0):
            market_date -= timedelta(days=1)
        rows = self._query(
            """
            SELECT trade_date AS trading_date FROM calendar
            WHERE trade_date <= %s AND holiday = 'N'
            ORDER BY trade_date DESC LIMIT 1
            """,
            [market_date],
        )
        self._set_meta(point_in_time_quality="exact", as_of=_as_of_iso(as_of))
        return str(rows[0]["trading_date"]) if rows else None


class GxMarketInfoClient:
    """Transport-neutral GX client used by TradingAgents and diagnostics."""

    def __init__(self, source: GxDataSource, settings: Mapping[str, Any] | None = None):
        if not isinstance(source, GxDataSource):
            raise TypeError("source must implement GxDataSource")
        self.source = source
        self.settings = dict(settings or {})

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> GxMarketInfoClient:
        active = deepcopy(dict(config)) if config is not None else get_config()
        settings = dict(active.get("gx_market_info") or {})
        transport = str(
            settings.get("transport")
            or os.getenv("GX_DATA_TRANSPORT")
            or os.getenv("GX_MARKET_INFO_TRANSPORT")
            or "api"
        ).strip().lower()
        if transport not in _ALLOWED_TRANSPORTS:
            raise ValueError(
                f"gx_market_info.transport must be one of {sorted(_ALLOWED_TRANSPORTS)}, got {transport!r}"
            )
        timeout = float(
            settings.get("timeout_seconds")
            or os.getenv("GX_DATA_TIMEOUT_SECONDS")
            or os.getenv("GX_MARKET_INFO_TIMEOUT_SECONDS")
            or 10.0
        )
        if transport == "api":
            source: GxDataSource = GxHttpDataSource(
                base_url=settings.get("base_url") or os.getenv("GX_MARKET_INFO_BASE_URL", "http://localhost:5005"),
                api_version=settings.get("api_version") or os.getenv("GX_MARKET_INFO_API_VERSION", "v1.0.7"),
                analysis_token=settings.get("analysis_token") or os.getenv("GX_ANALYSIS_DATA_API_KEY"),
                tradingview_token=settings.get("tradingview_token")
                or os.getenv("GX_MARKET_INFO_TV_TOKEN")
                or os.getenv("GX_TRADINGVIEW_API_KEY"),
                timeout_seconds=timeout,
            )
        else:
            dsn = (
                settings.get("postgres_dsn")
                or os.getenv("GX_MARKET_INFO_DATABASE_URL")
                or os.getenv("GX_MARKET_INFO_POSTGRES_DSN")
            )
            source = GxPostgresDataSource(
                dsn=dsn or "",
                connect_timeout_seconds=timeout,
                expected_database=(
                    settings.get("expected_database")
                    or os.getenv("GX_MARKET_INFO_EXPECTED_DB")
                    or GX_SOURCE_NAME
                ),
                statement_timeout_ms=int(settings.get("statement_timeout_ms", 15_000)),
            )
        return cls(source, settings)

    def get_ohlcv(
        self,
        symbol: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
        resolution: str = "1D",
    ) -> pd.DataFrame:
        return self.source.get_ohlcv(symbol, start_date, end_date, resolution)

    def get_last_trading_session(
        self, as_of: str | date | datetime | None = None
    ) -> str | None:
        return self.source.get_last_trading_session(as_of)

    def get_quote(
        self, symbol: str, as_of: str | date | datetime | None = None
    ) -> dict[str, Any]:
        return self.source.get_quote(symbol, as_of)

    def get_instrument_identity(
        self, symbol: str, as_of: str | date | datetime | None = None
    ) -> dict[str, str]:
        profile = self.source.get_profile(symbol, as_of)
        identity: dict[str, str] = {}
        for keys, target in (
            (("organization_name", "name", "short_name"), "company_name"),
            (("industry_name", "business_line", "sector_type_code"), "industry"),
            (("security_exchange", "market_id"), "exchange"),
            (("security_group_id",), "quote_type"),
        ):
            value = next((profile.get(key) for key in keys if profile.get(key)), None)
            if value is not None:
                identity[target] = str(value)
        return identity

    def get_instrument_aliases(
        self, symbol: str, as_of: str | date | datetime | None = None
    ) -> list[str]:
        """Return deterministic GX names suitable for Vietnamese-news matching.

        Profile rows are current-state metadata, so callers must retain GX's
        ``current_only``/proxy provenance for historical analysis.  This helper
        deliberately returns names only; the canonical ticker remains a
        separate, exchange-aware matching input.
        """
        profile = self.source.get_profile(symbol, as_of)
        aliases: list[str] = []
        seen: set[str] = set()
        for key in (
            "organization_name",
            "organization_short_name",
            "name",
            "short_name",
            "organization_name_en",
            "organization_short_name_en",
            "name_en",
            "short_name_en",
        ):
            raw = profile.get(key)
            if raw is None:
                continue
            value = " ".join(str(raw).split())
            if not value or value.casefold() in {"none", "n/a", "null", "nan"}:
                continue
            marker = value.casefold()
            if marker not in seen:
                seen.add(marker)
                aliases.append(value)
        return aliases

    def get_fundamentals(
        self,
        symbol: str,
        as_of: str | date | datetime | None = None,
        frequency: str = "quarterly",
        limit: int = 8,
    ) -> dict[str, Any]:
        payload = self.source.get_fundamentals(symbol, as_of, frequency, limit)
        return _canonicalize_fundamentals(payload)

    def get_cashflow(
        self,
        symbol: str,
        as_of: str | date | datetime | None = None,
        frequency: str = "quarterly",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        rows = self.source.get_cashflow(symbol, as_of, frequency, limit)
        return _to_native(
            [
                row
                if row.get("normalization_profile")
                else _normalize_cash_flow(row)
                for row in rows
            ]
        )

    def get_index_history(
        self,
        index_code: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
    ) -> pd.DataFrame:
        return self.source.get_index_history(index_code, start_date, end_date)

    @property
    def provenance(self) -> dict[str, Any]:
        """Metadata for the most recent call, including source and transport."""
        return deepcopy(getattr(self.source, "last_meta", {}))

    def get_events(
        self,
        symbol: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self.source.get_events(symbol, start_date, end_date, limit)


def get_gx_market_info_client(config: Mapping[str, Any] | None = None) -> GxMarketInfoClient:
    """Build a client from active TradingAgents config (no process-global cache)."""
    return GxMarketInfoClient.from_config(config)


def get_ohlcv_frame(
    symbol: str,
    start_date: str | date | datetime,
    end_date: str | date | datetime,
    resolution: str = "1D",
) -> pd.DataFrame:
    return get_gx_market_info_client().get_ohlcv(symbol, start_date, end_date, resolution)


def get_instrument_identity(symbol: str, as_of: str | date | datetime | None = None) -> dict[str, str]:
    return get_gx_market_info_client().get_instrument_identity(symbol, as_of)


def get_instrument_aliases(
    symbol: str, as_of: str | date | datetime | None = None
) -> list[str]:
    """Return GX company-name aliases without exposing profile credentials/data."""
    return get_gx_market_info_client().get_instrument_aliases(symbol, as_of)


def get_stock_data(
    symbol: str,
    start_date: str,
    end_date: str,
) -> str:
    frame = get_ohlcv_frame(symbol, start_date, end_date)
    header = (
        f"# GX stock data for {_normalize_symbol(symbol)} from {start_date} to {end_date}\n"
        f"# Currency: VND; source: {GX_SOURCE_NAME}; total records: {len(frame)}\n\n"
    )
    return header + frame.to_csv(index=False)


_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50-day simple moving average",
    "close_200_sma": "200-day simple moving average",
    "close_10_ema": "10-day exponential moving average",
    "macd": "Moving Average Convergence Divergence",
    "macds": "MACD signal line",
    "macdh": "MACD histogram",
    "rsi": "Relative Strength Index",
    "boll": "Bollinger middle band",
    "boll_ub": "Bollinger upper band",
    "boll_lb": "Bollinger lower band",
    "atr": "Average True Range",
    "vwma": "Volume-weighted moving average",
    "mfi": "Money Flow Index",
}


def get_indicators(
    symbol: str, indicator: str, curr_date: str, look_back_days: int = 30
) -> str:
    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Choose from: {sorted(_INDICATOR_DESCRIPTIONS)}"
        )
    requested_cutoff = _explicit_as_of_datetime(curr_date)
    end = _parse_date(curr_date, "curr_date")
    # Indicator warm-up needs substantially more history than the display window.
    start = end - timedelta(days=max(400, int(look_back_days) * 4))
    frame = get_gx_market_info_client().get_ohlcv(
        symbol,
        start,
        requested_cutoff or end,
    )
    stock = wrap(frame.copy())
    stock[indicator]
    stock["Date"] = pd.to_datetime(stock["Date"]).dt.strftime("%Y-%m-%d")
    display_start = end - timedelta(days=int(look_back_days))
    selected = stock[pd.to_datetime(stock["Date"]).dt.date >= display_start][["Date", indicator]]
    values = "\n".join(
        f"{row['Date']}: {'N/A' if pd.isna(row[indicator]) else row[indicator]}"
        for _, row in selected.iterrows()
    )
    return (
        f"## {indicator} values from {display_start} to {end}\n\n{values}\n\n"
        f"{_INDICATOR_DESCRIPTIONS[indicator]}. Values are computed from GX VND OHLCV."
    )


def _render_json_report(title: str, symbol: str, payload: Any, as_of: str | None = None) -> str:
    cutoff = f" as of {as_of}" if as_of else ""
    return (
        f"# {title} for {_normalize_symbol(symbol)}{cutoff}\n"
        f"# Source: {GX_SOURCE_NAME}; currency: VND\n\n"
        + json.dumps(_to_native(payload), ensure_ascii=False, indent=2)
    )


def get_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    client = get_gx_market_info_client()
    payload = client.get_fundamentals(ticker, curr_date, "quarterly", 8)
    financials_meta = client.provenance
    explicit_cutoff = _explicit_as_of_datetime(curr_date) if curr_date else None
    strict_historical = bool(
        client.settings.get("strict_point_in_time")
        and curr_date
        and (
            explicit_cutoff is not None
            or _parse_date(curr_date, "curr_date") < _now_vn().date()
        )
    )
    if strict_historical:
        profile_section: dict[str, Any] = {
            "data": None,
            "meta": {
                "source": GX_SOURCE_NAME,
                "point_in_time_quality": "current_only",
            },
            "unavailable": {
                "code": "CURRENT_ONLY",
                "reason": "GX profile history is unavailable in strict point-in-time mode.",
            },
        }
    else:
        profile = client.source.get_profile(_normalize_symbol(ticker), curr_date)
        profile_section = {
            "data": profile,
            "meta": client.provenance,
            "warning": (
                "Company profile is current-state/current_only and is not a "
                "historical snapshot at the analysis date."
            ),
        }
    return _render_json_report(
        "GX company fundamentals",
        ticker,
        {
            "profile": profile_section,
            "financials": {"data": payload, "meta": financials_meta},
        },
        curr_date,
    )


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    payload = get_gx_market_info_client().get_fundamentals(ticker, curr_date, freq, 8)
    rows = payload.get("balance_sheets", [])
    if not rows:
        raise NoMarketDataError(ticker, _normalize_symbol(ticker), "no balance sheet published by cutoff")
    return _render_json_report("GX balance sheet", ticker, rows, curr_date)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    payload = get_gx_market_info_client().get_fundamentals(ticker, curr_date, freq, 8)
    rows = payload.get("income_statements", [])
    if not rows:
        raise NoMarketDataError(ticker, _normalize_symbol(ticker), "no income statement published by cutoff")
    return _render_json_report("GX income statement", ticker, rows, curr_date)


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    client = get_gx_market_info_client()
    rows = client.get_cashflow(ticker, curr_date, freq, 8)
    return _render_json_report(
        "GX cash-flow statement",
        ticker,
        {
            "data": rows,
            "meta": {
                **client.provenance,
                "normalization": (
                    "For comtypecode=CT, free_cash_flow = CFA18 + CFA19; "
                    "CFA19 is the signed purchase-of-fixed-assets cash flow."
                ),
            },
        },
        curr_date,
    )


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    events = get_gx_market_info_client().get_events(ticker, start_date, end_date, 50)
    if not events:
        raise NoMarketDataError(
            ticker,
            _normalize_symbol(ticker),
            f"no GX corporate disclosures between {start_date} and {end_date}",
        )
    lines = [
        f"## GX corporate disclosures for {_normalize_symbol(ticker)}, {start_date} to {end_date}",
        "",
        "These are exchange/company disclosures, not a general-news or social-media feed.",
        "",
    ]
    for event in events:
        title = event.get("eventtitle") or event.get("en_eventtitle") or "Corporate event"
        published = event.get("publicdate") or "date unavailable"
        description = event.get("eventdescription") or event.get("en_eventdescription") or ""
        lines.append(f"### {title} ({published})")
        if description:
            lines.append(str(description))
        if event.get("sourceurl"):
            lines.append(f"Source: {event['sourceurl']}")
        lines.append("")
    return "\n".join(lines)
