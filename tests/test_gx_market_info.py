"""Focused contract tests for the GX market-info adapter.

All transports are fake: these tests never need a GX service, PostgreSQL, or
Yahoo connection.
"""

from __future__ import annotations

import json
import traceback
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from tradingagents.dataflows import (
    config as config_module,
    gx_market_info as gx,
    interface,
    polymarket,
    reddit,
    stockstats_utils,
    stocktwits,
    y_finance,
)
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.default_config import DEFAULT_CONFIG, apply_gx_market_info_defaults


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)


def envelope(data, **meta):
    return {
        "schema_version": 1,
        "data": data,
        "meta": {
            "source": "g_market_info_1229",
            "timezone": "Asia/Ho_Chi_Minh",
            "point_in_time_quality": "exact",
            **meta,
        },
    }


@pytest.mark.unit
def test_date_only_cutoff_is_vietnam_market_close():
    assert gx._as_of_iso("2025-01-02") == "2025-01-02T15:00:00+07:00"


@pytest.mark.unit
def test_exact_live_cutoff_preserves_timezone_and_microseconds():
    cutoff = "2026-08-19T16:05:31.123456+07:00"

    assert gx._as_of_iso(cutoff) == cutoff
    assert gx._parse_date(cutoff, "cutoff").isoformat() == "2026-08-19"


@pytest.mark.unit
def test_instrument_aliases_use_all_gx_name_variants_and_deduplicate():
    session = FakeSession(
        lambda _url, _kwargs: FakeResponse(
            envelope(
                {
                    "organization_name": "  Công ty CP Tập đoàn Hòa Phát  ",
                    "organization_short_name": "Hòa Phát",
                    "name": "Công ty CP Tập đoàn Hòa Phát",
                    "short_name": "HPG",
                    "organization_name_en": "Hoa Phat Group JSC",
                    "organization_short_name_en": "Hoa Phat",
                },
                point_in_time_quality="current_only",
            )
        )
    )
    client = gx.GxMarketInfoClient(gx.GxHttpDataSource("http://gx.test", session=session))

    assert client.get_instrument_aliases("HPG", "2025-01-02") == [
        "Công ty CP Tập đoàn Hòa Phát",
        "Hòa Phát",
        "HPG",
        "Hoa Phat Group JSC",
        "Hoa Phat",
    ]


@pytest.mark.unit
def test_http_ohlcv_scales_thousand_vnd_and_rejects_rows_after_cutoff():
    at_close = int(datetime(2025, 1, 2, 8, tzinfo=timezone.utc).timestamp())
    after_close = int(datetime(2025, 1, 2, 9, tzinfo=timezone.utc).timestamp())

    session = FakeSession(
        lambda _url, _kwargs: FakeResponse(
            {
                "s": "ok",
                "t": [at_close, after_close],
                "o": [24, 99],
                "h": [26, 99],
                "l": [23, 99],
                "c": [25, 99],
                "v": [1000, 1],
            }
        )
    )
    source = gx.GxHttpDataSource("http://gx.test", session=session)

    frame = source.get_ohlcv("HOSE:HPG", "2025-01-01", "2025-01-02")

    assert frame["Close"].tolist() == [25_000.0]
    assert frame["Open"].tolist() == [24_000.0]
    assert frame.attrs["gx_provenance"]["price_unit"] == "VND"


@pytest.mark.unit
def test_http_index_accepts_timestamp_and_trading_date_without_duplicate_date():
    row = {
        "index_code": "VNINDEX",
        "trading_date": "2025-01-02",
        "timestamp": "2025-01-02T08:00:00Z",
        "close": "1250.25",
        "total_volume": 42,
    }
    session = FakeSession(lambda _url, _kwargs: FakeResponse(envelope([row])))
    source = gx.GxHttpDataSource("http://gx.test", session=session)

    frame = source.get_index_history("VNINDEX", "2025-01-01", "2025-01-02")

    assert list(frame.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert frame.loc[0, "Date"] == pd.Timestamp("2025-01-02")
    assert frame.loc[0, "Close"] == pytest.approx(1250.25)
    assert source.last_meta["transport"] == "api"


@pytest.mark.unit
def test_current_preclose_ohlcv_clamps_to_last_completed_session(monkeypatch):
    now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone(timedelta(hours=7)))
    monkeypatch.setattr(gx, "_now_vn", lambda: now)

    def handler(url, _kwargs):
        if url.endswith("calendar/last-session"):
            return FakeResponse(envelope({"trading_date": "2026-08-12"}))
        if url.endswith("tradingview/datafeed/history"):
            ts = int(datetime(2026, 8, 12, 8, tzinfo=timezone.utc).timestamp())
            return FakeResponse({"s": "ok", "t": [ts], "o": [20], "h": [21], "l": [19], "c": [20], "v": [1]})
        raise AssertionError(url)

    session = FakeSession(handler)
    source = gx.GxHttpDataSource("http://gx.test", session=session)
    source.get_ohlcv("HPG", "2026-08-11", "2026-08-13")

    history_call = next(call for call in session.calls if call[0].endswith("/history"))
    expected_to = int(datetime(2026, 8, 12, 15, 0, tzinfo=timezone(timedelta(hours=7))).timestamp())
    assert history_call[1]["params"]["to"] == expected_to


@pytest.mark.unit
def test_frozen_live_preclose_cutoff_does_not_use_resume_wall_clock(monkeypatch):
    # Simulate resuming the next day after close. The persisted 10:00 cutoff
    # must still clamp to the session completed on 2026-08-18.
    monkeypatch.setattr(
        gx,
        "_now_vn",
        lambda: datetime(
            2026, 8, 20, 16, 0, tzinfo=timezone(timedelta(hours=7))
        ),
    )

    def handler(url, kwargs):
        if url.endswith("calendar/last-session"):
            assert kwargs["params"]["as_of"] == "2026-08-19T10:00:00+07:00"
            return FakeResponse(envelope({"trading_date": "2026-08-18"}))
        if url.endswith("tradingview/datafeed/history"):
            ts = int(datetime(2026, 8, 18, 8, tzinfo=timezone.utc).timestamp())
            return FakeResponse(
                {"s": "ok", "t": [ts], "o": [20], "h": [21], "l": [19], "c": [20], "v": [1]}
            )
        raise AssertionError(url)

    session = FakeSession(handler)
    source = gx.GxHttpDataSource("http://gx.test", session=session)
    source.get_ohlcv(
        "HPG",
        "2026-08-11",
        "2026-08-19T10:00:00+07:00",
    )

    history_call = next(call for call in session.calls if call[0].endswith("/history"))
    expected_to = int(
        datetime(
            2026, 8, 18, 15, 0, tzinfo=timezone(timedelta(hours=7))
        ).timestamp()
    )
    assert history_call[1]["params"]["to"] == expected_to
    assert source.last_meta["as_of"] == "2026-08-19T10:00:00+07:00"


@pytest.mark.unit
def test_live_after_close_uses_current_completed_daily_candle(monkeypatch):
    monkeypatch.setattr(
        gx,
        "_now_vn",
        lambda: datetime(
            2026, 8, 20, 10, 0, tzinfo=timezone(timedelta(hours=7))
        ),
    )

    def handler(url, _kwargs):
        if url.endswith("calendar/last-session"):
            raise AssertionError("an after-close frozen cutoff must not use the wall clock")
        if url.endswith("tradingview/datafeed/history"):
            ts = int(datetime(2026, 8, 19, 8, tzinfo=timezone.utc).timestamp())
            return FakeResponse(
                {"s": "ok", "t": [ts], "o": [20], "h": [21], "l": [19], "c": [20], "v": [1]}
            )
        raise AssertionError(url)

    source = gx.GxHttpDataSource(
        "http://gx.test", session=FakeSession(handler)
    )
    frame = source.get_ohlcv(
        "HPG",
        "2026-08-11",
        "2026-08-19T16:00:00+07:00",
    )

    assert frame.iloc[-1]["Date"] == pd.Timestamp("2026-08-19 15:00:00")
    assert source.last_meta["as_of"] == "2026-08-19T16:00:00+07:00"


@pytest.mark.unit
def test_http_events_and_fundamentals_receive_exact_live_cutoff():
    session = FakeSession(lambda _url, _kwargs: FakeResponse(envelope([])))
    source = gx.GxHttpDataSource("http://gx.test", session=session)
    cutoff = "2026-08-19T16:05:31.123456+07:00"

    source.get_events("CTG", "2026-08-12T16:05:31.123456+07:00", cutoff)
    event_call = session.calls[-1]
    assert event_call[1]["params"]["as_of"] == cutoff
    assert event_call[1]["params"]["from"] == "2026-08-12"

    session.handler = lambda _url, _kwargs: FakeResponse(envelope({}))
    source.get_fundamentals("CTG", cutoff)
    fundamental_call = session.calls[-1]
    assert fundamental_call[1]["params"]["as_of"] == cutoff


@pytest.mark.unit
def test_http_quote_finality_uses_frozen_cutoff_and_source_session():
    cutoff = "2026-08-19T16:05:31.123456+07:00"
    session = FakeSession(
        lambda _url, _kwargs: FakeResponse(
            envelope(
                {
                    "ticker": "CTG",
                    "last_price": "50000",
                    "trading_session_id": "99",
                    "source_updated_at": "2026-08-19T08:00:01Z",
                    # The older GX endpoint used to hard-code this false. The
                    # adapter recomputes it from immutable source evidence.
                    "is_final": False,
                },
                source_timestamp="2026-08-19T08:00:01Z",
                session_completed_at_as_of=True,
            )
        )
    )
    source = gx.GxHttpDataSource("http://gx.test", session=session)

    quote = source.get_quote("CTG", cutoff)

    assert quote["is_final"] is True
    assert session.calls[0][1]["params"]["as_of"] == cutoff


@pytest.mark.unit
def test_quote_before_close_or_without_final_session_fails_closed():
    source_time = "2026-08-19T02:59:00Z"  # 09:59 Asia/Ho_Chi_Minh
    assert gx._quote_is_final(
        {
            "trading_session_id": "99",
            "source_updated_at": source_time,
        },
        "2026-08-19T10:00:00+07:00",
    ) is False
    assert gx._quote_is_final(
        {
            "trading_session_id": "40",
            "source_updated_at": "2026-08-19T08:01:00Z",
        },
        "2026-08-19T16:05:00+07:00",
    ) is False


@pytest.mark.unit
def test_postgres_quote_finality_understands_utc_naive_source_timestamp():
    class QuotePostgres(gx.GxPostgresDataSource):
        def __init__(self):
            super().__init__("postgresql://unused")

        def _query(self, _sql, params):
            assert params[1] == datetime(2026, 8, 19, 9, 5)
            return [
                {
                    "ticker": "CTG",
                    "last_price": "50000",
                    "trading_session_id": "00",
                    # GX stores this as UTC-naive.
                    "source_updated_at": datetime(2026, 8, 19, 8, 0, 1),
                }
            ]

    quote = QuotePostgres().get_quote(
        "CTG", "2026-08-19T16:05:00+07:00"
    )

    assert quote["is_final"] is True


class FakePostgres(gx.GxPostgresDataSource):
    def __init__(self, records):
        super().__init__("postgresql://unused")
        self.records = records
        self.queries = []

    def _query(self, sql, params):
        self.queries.append((sql, list(params)))
        if "stocks_info_ext" in sql:
            return [self.records["overview"]]
        if "fiin_incomestatement" in sql:
            return [self.records["income"]]
        if "fiin_balancesheet" in sql:
            return [self.records["balance"]]
        if "fiin_cashflow" in sql:
            return [self.records["cash_flow"]]
        if "fiin_ratiottmdaily" in sql:
            return [self.records["daily"]]
        if "fiin_ratiottm" in sql:
            return [self.records["ttm"]]
        raise AssertionError(sql)


@pytest.mark.unit
def test_api_and_postgres_fundamentals_share_canonical_contract():
    records = {
        "overview": {"ticker_code": "HPG", "trading_date": "2025-01-02"},
        "income": {
            "ticker": "HPG",
            "yearreport": 2024,
            "lengthreport": 4,
            "publicdate": "2025-01-02T08:00:00",
            "comtypecode": "CT",
            "isa3": "123456",
            "isa20": "12000",
        },
        "balance": {
            "ticker": "HPG",
            "yearreport": 2024,
            "lengthreport": 4,
            "publicdate": "2025-01-02T08:00:00",
            "comtypecode": "CT",
            "bsa53": "500000",
            "bsa78": "200000",
        },
        "cash_flow": {
            "cashflowid": 42,
            "ticker": "HPG",
            "yearreport": 2024,
            "lengthreport": 4,
            "lengthseries": 3,
            "publicdate": "2025-01-02T08:00:00",
            "createdate": "2025-01-02T08:00:01",
            "updatedate": "2025-01-02T08:00:02",
            "comtypecode": "CT",
            "isaudit": False,
            "isdirect": False,
            "reportformtypecode": "S",
            "status": 1,
            "cfa18": "100000",
            "cfa19": "-25000",
            "cfa26": "-20000",
            "cfa34": "-10000",
            "cfa35": "70000",
            "cfa36": "30000",
            "cfa38": "100000",
        },
        "ttm": {
            "ticker": "HPG",
            "yearreport": 2024,
            "lengthreport": 4,
            "rtq29": "0.097",
        },
        "daily": {
            "ticker": "HPG",
            "yearreport": 2024,
            "lengthreport": 4,
            "rtd14": "2500",
            "rtd21": "8.5",
        },
    }
    raw = {
        "overview": records["overview"],
        "income_statements": [records["income"]],
        "balance_sheets": [records["balance"]],
        "cash_flow_statements": [records["cash_flow"]],
        "ratios": {"ttm": [records["ttm"]], "daily": [records["daily"]]},
        "unavailable": {},
    }
    session = FakeSession(lambda _url, _kwargs: FakeResponse(envelope(raw)))
    http_client = gx.GxMarketInfoClient(
        gx.GxHttpDataSource("http://gx.test", session=session)
    )
    postgres = FakePostgres(records)
    pg_client = gx.GxMarketInfoClient(postgres)

    http = http_client.get_fundamentals("HPG", "2025-01-03")
    pg = pg_client.get_fundamentals("HPG", "2025-01-03")

    assert http == pg
    assert pg["income_statements"][0]["normalized"]["revenue"] == "123456"
    assert pg["cash_flow_statements"][0]["normalized"] == {
        "net_cash_from_operating_activities": "100000",
        "capital_expenditures": "-25000",
        "net_cash_from_investing_activities": "-20000",
        "net_cash_from_financing_activities": "-10000",
        "net_change_in_cash": "70000",
        "cash_and_cash_equivalents_beginning": "30000",
        "foreign_exchange_effect": None,
        "cash_and_cash_equivalents_ending": "100000",
        "free_cash_flow": "75000",
    }
    assert pg["coverage"]["cash_flow"] is True
    assert "cash_flow" not in pg["unavailable"]
    assert pg["ratios"]["daily"][0]["normalized"]["pe"] == "8.5"
    ratio_sql = "\n".join(sql for sql, _params in postgres.queries if "fiin_ratio" in sql)
    assert "tradingdate <= %s" in ratio_sql
    assert "updatedate <= %s" in ratio_sql
    statement_sql = "\n".join(
        sql
        for sql, _params in postgres.queries
        if "fiin_incomestatement" in sql or "fiin_balancesheet" in sql
    )
    assert statement_sql.count("createdate <= %s") == 2
    assert statement_sql.count("updatedate <= %s") == 2
    statement_sql = "\n".join(
        sql
        for sql, _params in postgres.queries
        if "fiin_incomestatement" in sql or "fiin_balancesheet" in sql
    )
    assert statement_sql.count("createdate IS NOT NULL AND createdate <= %s") == 2
    cashflow_sql = next(sql for sql, _params in postgres.queries if "fiin_cashflow" in sql)
    assert "cashflow_row.status = 1" in cashflow_sql
    assert "isfinite(cashflow_row.publicdate)" in cashflow_sql
    assert "cashflow_row.createdate <= %s" in cashflow_sql
    assert "cashflow_row.updatedate <= %s" in cashflow_sql


@pytest.mark.unit
def test_postgres_quote_converts_vietnam_cutoff_to_utc_naive():
    class QuotePostgres(gx.GxPostgresDataSource):
        def __init__(self):
            super().__init__("postgresql://unused")
            self.params = None

        def _query(self, _sql, params):
            self.params = list(params)
            return [{"ticker": "HPG", "last_price": "25000"}]

    source = QuotePostgres()
    source.get_quote("HPG", "2025-01-02")
    assert source.params[1] == datetime(2025, 1, 2, 8, 0)


@pytest.mark.unit
def test_postgres_wide_json_rows_preserve_decimals_and_null_infinity():
    class JsonPostgres(gx.GxPostgresDataSource):
        def __init__(self):
            super().__init__("postgresql://unused")

        def _query(self, _sql, _params):
            return [
                {
                    "payload": (
                        '{"integrateddate":"-infinity",'
                        '"updatedate":"2025-01-02T08:00:00",'
                        '"ratio":12345678901234567890.1234}'
                    )
                }
            ]

    rows = JsonPostgres()._query_json_records("SELECT payload", [])

    assert rows[0]["integrateddate"] is None
    assert rows[0]["updatedate"] == "2025-01-02T08:00:00"
    assert rows[0]["ratio"] == gx.Decimal("12345678901234567890.1234")


@pytest.mark.unit
def test_postgres_cashflow_is_strict_as_of_deduplicated_and_normalized():
    class CashFlowPostgres(gx.GxPostgresDataSource):
        def __init__(self):
            super().__init__("postgresql://unused")
            self.sql = ""
            self.params = []

        def _query(self, sql, params):
            self.sql = sql
            self.params = list(params)
            return [
                {
                    "ticker": "HPG",
                    "cashflowid": 3,
                    "comtypecode": "CT",
                    "yearreport": 2025,
                    "lengthreport": 4,
                    "publicdate": "2026-01-30T00:00:00",
                    "createdate": "2026-01-30T00:00:01",
                    "updatedate": "2026-01-30T00:00:02",
                    "status": 1,
                    "cfa18": gx.Decimal("6816755450021.0000"),
                    "cfa19": gx.Decimal("-5489839982929.0000"),
                    "cfa26": gx.Decimal("-2921294017214.0000"),
                    "cfa34": gx.Decimal("-767794471020.0000"),
                    "cfa35": gx.Decimal("3127666961787.0000"),
                    "cfa36": gx.Decimal("8325103342897.0000"),
                    "cfa37": gx.Decimal("2460733821.0000"),
                    "cfa38": gx.Decimal("11455231038505.0000"),
                    "integrateddate": None,
                },
                # An older eligible revision of the same reporting period is
                # ignored deterministically by the period deduplicator.
                {
                    "ticker": "HPG",
                    "cashflowid": 2,
                    "comtypecode": "CT",
                    "yearreport": 2025,
                    "lengthreport": 4,
                    "publicdate": "2026-01-29T00:00:00",
                    "status": 1,
                    "cfa18": gx.Decimal("1.0000"),
                },
                {
                    "ticker": "HPG",
                    "cashflowid": 1,
                    "comtypecode": "CT",
                    "yearreport": 2025,
                    "lengthreport": 3,
                    "publicdate": "2025-10-30T00:00:00",
                    "status": 1,
                    "cfa18": None,
                    "cfa19": gx.Decimal("-10.0000"),
                },
            ]

    source = CashFlowPostgres()
    rows = source.get_cashflow("hpg.vn", "2026-02-02", "quarterly", 8)

    assert [(row["yearreport"], row["lengthreport"]) for row in rows] == [
        (2025, 4),
        (2025, 3),
    ]
    latest = rows[0]
    assert latest["normalized"]["net_cash_from_operating_activities"] == (
        "6816755450021.0000"
    )
    assert latest["normalized"]["capital_expenditures"] == "-5489839982929.0000"
    assert latest["normalized"]["foreign_exchange_effect"] == "2460733821.0000"
    assert latest["normalized"]["free_cash_flow"] == "1326915467092.0000"
    assert rows[1]["normalized"]["free_cash_flow"] is None
    assert "integrateddate" not in latest
    assert source.params[0] == "HPG"
    assert source.params[1:4] == [datetime(2026, 2, 2, 15, 0)] * 3
    assert source.params[4] == [1, 2, 3, 4]
    assert "cashflow_row.status = 1" in source.sql
    assert source.sql.count("isfinite(") == 3
    assert "cashflow_row.publicdate <= %s" in source.sql
    assert "cashflow_row.createdate <= %s" in source.sql
    assert "cashflow_row.updatedate <= %s" in source.sql
    assert source.last_meta["point_in_time_quality"] == "partial"
    assert source.last_meta["monetary_unit"] == "source_reported; not inferred"

    source.get_cashflow("HPG", "2026-02-02", "annual", 2)
    assert source.params[4] == [5]
    assert source.params[5] == 16


@pytest.mark.unit
def test_cashflow_ct_mapping_is_not_applied_to_specialized_company_types():
    row = gx._normalize_cash_flow(
        {
            "ticker": "SSI",
            "comtypecode": "CK",
            "yearreport": 2025,
            "lengthreport": 4,
            "cfa18": "999",
            "cfs140": "123",
        }
    )

    assert row["normalization_profile"] == "raw_only"
    assert row["normalized"] == {}
    assert row["cfs140"] == "123"


@pytest.mark.unit
def test_http_cashflow_not_modeled_is_typed_no_data_without_other_transport():
    raw = {
        "income_statements": [],
        "balance_sheets": [],
        "ratios": {"ttm": [], "daily": []},
        "unavailable": {
            "cash_flow": {
                "code": "NOT_MODELED",
                "reason": "GX API cash-flow endpoint is not modeled.",
            }
        },
    }
    session = FakeSession(lambda _url, _kwargs: FakeResponse(envelope(raw)))
    client = gx.GxMarketInfoClient(gx.GxHttpDataSource("http://gx.test", session=session))

    with pytest.raises(NoMarketDataError, match="not modeled"):
        client.get_cashflow("HPG", "2025-01-02")
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/stocks/HPG/fundamentals")


@pytest.mark.unit
def test_postgres_events_filter_creation_and_revision_timestamps():
    class EventsPostgres(gx.GxPostgresDataSource):
        def __init__(self):
            super().__init__("postgresql://unused")
            self.sql = ""
            self.params = []

        def _query(self, sql, params):
            self.sql = sql
            self.params = list(params)
            return []

    source = EventsPostgres()
    assert source.get_events("HPG", "2025-01-01", "2025-01-02") == []
    assert "createdate <= %s" in source.sql
    assert "updatedate <= %s" in source.sql
    assert source.last_meta["point_in_time_quality"] == "partial"


@pytest.mark.unit
def test_transport_errors_redact_secrets():
    secret = "postgresql://secret-user:secret-password@private-host/db"

    def fail_http(_url, **_kwargs):
        raise requests.ConnectionError(f"failed with token=top-secret at {_url}")

    http = gx.GxHttpDataSource("https://private-host", session=FakeSession(lambda *_: None))
    http.session.get = fail_http
    with pytest.raises(gx.GxMarketInfoError) as http_error:
        http.get_profile("HPG", "2025-01-02")
    assert "private-host" not in str(http_error.value)
    assert "top-secret" not in str(http_error.value)

    def fail_postgres(_dsn, **_kwargs):
        raise RuntimeError(secret)

    postgres = gx.GxPostgresDataSource(secret, connector=fail_postgres)
    with pytest.raises(gx.GxMarketInfoError) as pg_error:
        postgres._query("SELECT 1", [])
    assert secret not in str(pg_error.value)
    assert "secret-password" not in str(pg_error.value)
    rendered_traceback = "".join(
        traceback.format_exception(pg_error.type, pg_error.value, pg_error.tb)
    )
    assert secret not in rendered_traceback
    assert "secret-password" not in rendered_traceback
    assert pg_error.value.__cause__ is None


@pytest.mark.unit
@pytest.mark.parametrize("limit", [0, -1, 21, 1.5, "many"])
def test_postgres_cashflow_rejects_invalid_limits_before_connecting(limit):
    source = gx.GxPostgresDataSource(
        "postgresql://unused",
        connector=lambda *_args, **_kwargs: pytest.fail(
            "invalid limit must fail before connecting"
        ),
    )

    with pytest.raises(ValueError, match="between 1 and 20"):
        source.get_cashflow("HPG", "2026-02-02", limit=limit)


@pytest.mark.unit
def test_postgres_cashflow_unknown_ticker_is_typed_no_data():
    class EmptyCashFlowPostgres(gx.GxPostgresDataSource):
        def __init__(self):
            super().__init__("postgresql://unused")

        def _query(self, _sql, _params):
            return []

    with pytest.raises(NoMarketDataError, match="no eligible fiin_cashflow row"):
        EmptyCashFlowPostgres().get_cashflow("ZZZZ", "2026-02-02")


@pytest.mark.unit
def test_postgres_connection_is_read_only_and_database_pinned():
    seen = {}

    class Cursor:
        description = [("value",)]

        def __init__(self, validation):
            self.validation = validation

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=()):
            return None

        def fetchone(self):
            return {
                "database_name": "g_market_info_1229",
                "transaction_read_only": "on",
            }

        def fetchall(self):
            return [{"value": 1}]

    class Connection:
        read_only = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor(True)

    def connector(dsn, **kwargs):
        seen.update({"dsn": dsn, **kwargs})
        return Connection()

    source = gx.GxPostgresDataSource("postgresql://safe", connector=connector)
    assert source._query("SELECT 1 AS value", []) == [{"value": 1}]
    assert seen["application_name"] == "tradingagents-gx"
    assert "default_transaction_read_only=on" in seen["options"]
    assert "statement_timeout=15000" in seen["options"]


@pytest.mark.unit
def test_gx_routing_does_not_touch_yahoo(monkeypatch):
    profile = apply_gx_market_info_defaults(deepcopy(DEFAULT_CONFIG))
    set_config(profile)
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(
                ["2025-01-02 15:00:00", "2025-01-03 15:00:00"]
            ),
            "Open": [20_000, 21_000],
            "High": [21_000, 22_000],
            "Low": [19_000, 20_000],
            "Close": [20_500, 21_500],
            "Volume": [1, 2],
        }
    )
    monkeypatch.setattr(gx, "get_ohlcv_frame", lambda *_args, **_kwargs: frame)
    monkeypatch.setattr(
        stockstats_utils.yf,
        "download",
        lambda *_args, **_kwargs: pytest.fail("Yahoo must not run under GX profile"),
    )

    result = stockstats_utils.load_ohlcv("HPG", "2025-01-03")
    assert result["Close"].tolist() == [20_500, 21_500]


@pytest.mark.unit
def test_strict_historical_cashflow_fails_before_yahoo(monkeypatch):
    profile = apply_gx_market_info_defaults(deepcopy(DEFAULT_CONFIG))
    set_config(profile)
    monkeypatch.setattr(
        y_finance.yf,
        "Ticker",
        lambda *_args, **_kwargs: pytest.fail("Yahoo must not run for historical cashflow"),
    )

    with pytest.raises(NoMarketDataError, match="historical cash flow"):
        y_finance.get_cashflow("HPG", curr_date="2025-01-02")


@pytest.mark.unit
def test_gx_profile_routes_cashflow_to_postgres_vendor_without_yahoo(monkeypatch):
    profile = apply_gx_market_info_defaults(deepcopy(DEFAULT_CONFIG))
    set_config(profile)
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_cashflow"],
        "gx_market_info",
        lambda *_args, **_kwargs: "GX_DB_CASH_FLOW",
    )
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_cashflow"],
        "yfinance",
        lambda *_args, **_kwargs: pytest.fail("Yahoo cash-flow must not be called"),
    )
    result = interface.route_to_vendor(
        "get_cashflow", "HPG", "quarterly", "2025-01-02"
    )

    assert result == "GX_DB_CASH_FLOW"


@pytest.mark.unit
def test_cashflow_report_contains_provenance_and_normalized_totals(monkeypatch):
    class FakeClient:
        provenance = {
            "source": "g_market_info_1229",
            "transport": "postgres",
            "point_in_time_quality": "partial",
        }

        def get_cashflow(self, *_args):
            return [
                gx._normalize_cash_flow(
                    {
                        "ticker": "HPG",
                        "comtypecode": "CT",
                        "yearreport": 2025,
                        "lengthreport": 4,
                        "cfa18": "100",
                        "cfa19": "-25",
                    }
                )
            ]

    monkeypatch.setattr(gx, "get_gx_market_info_client", lambda: FakeClient())
    report = gx.get_cashflow("HPG", "quarterly", "2026-02-02")
    payload = json.loads(report.split("\n\n", 1)[1])

    assert payload["data"][0]["normalized"]["free_cash_flow"] == "75"
    assert payload["meta"]["transport"] == "postgres"
    assert payload["meta"]["point_in_time_quality"] == "partial"


@pytest.mark.unit
def test_historical_social_feeds_exclude_current_or_undated_rows(monkeypatch):
    class StocktwitsResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"messages":[{"created_at":"2026-08-13T03:00:00Z","body":"future"},{"body":"undated"}]}'

    monkeypatch.setattr(stocktwits, "urlopen", lambda *_args, **_kwargs: StocktwitsResponse())
    stocktwits_result = stocktwits.fetch_stocktwits_messages(
        "HPG", as_of="2025-01-02"
    )
    assert "historical StockTwits unavailable" in stocktwits_result
    assert "future" not in stocktwits_result

    monkeypatch.setattr(
        reddit,
        "_fetch_subreddit",
        lambda *_args, **_kwargs: [
            {"title": "future", "created_utc": datetime(2026, 8, 13, tzinfo=timezone.utc).timestamp()},
            {"title": "undated", "created_utc": None},
        ],
    )
    reddit_result = reddit.fetch_reddit_posts(
        "HPG", subreddits=("stocks",), inter_request_delay=0, as_of="2025-01-02"
    )
    assert "historical Reddit unavailable" in reddit_result
    assert "future" not in reddit_result


@pytest.mark.unit
def test_historical_polymarket_returns_unavailable_without_live_request(monkeypatch):
    monkeypatch.setattr(
        polymarket,
        "_request",
        lambda *_args, **_kwargs: pytest.fail("historical request must not call live API"),
    )
    result = polymarket.get_prediction_markets("rates", as_of="2025-01-02")
    assert "current-only" in result
    assert "do not substitute live probabilities" in result


@pytest.mark.unit
def test_profile_is_opt_in_and_transport_is_strict():
    assert DEFAULT_CONFIG["data_vendors"]["core_stock_apis"] == "yfinance"
    profile = apply_gx_market_info_defaults(deepcopy(DEFAULT_CONFIG))
    assert profile["data_vendors"]["core_stock_apis"] == "gx_market_info"
    assert profile["benchmark_ticker"] == "VNINDEX"
    assert profile["gx_market_info"]["strict_point_in_time"] is True
    assert profile["tool_vendors"]["get_cashflow"] == "gx_market_info"
    assert profile["output_language"] == "Vietnamese"

    invalid = deepcopy(profile)
    invalid["gx_market_info"]["transport"] = "auto"
    with pytest.raises(ValueError, match="transport"):
        gx.GxMarketInfoClient.from_config(invalid)


@pytest.mark.unit
def test_strict_historical_fundamentals_omit_current_only_profile(monkeypatch):
    class FakeClient:
        settings = {"strict_point_in_time": True}
        provenance = {
            "source": "g_market_info_1229",
            "point_in_time_quality": "partial",
        }

        def get_fundamentals(self, *_args):
            return {"income_statements": [], "balance_sheets": []}

        class source:
            @staticmethod
            def get_profile(*_args):
                pytest.fail("strict historical report must not fetch current profile")

    monkeypatch.setattr(gx, "get_gx_market_info_client", lambda: FakeClient())
    report = gx.get_fundamentals("HPG", "2025-01-02")
    payload = json.loads(report.split("\n\n", 1)[1])

    assert payload["profile"]["data"] is None
    assert payload["profile"]["unavailable"]["code"] == "CURRENT_ONLY"
    assert payload["financials"]["meta"]["point_in_time_quality"] == "partial"


@pytest.mark.unit
def test_strict_live_cutoff_omits_current_only_profile_independent_of_wall_clock(
    monkeypatch,
):
    class FakeClient:
        settings = {"strict_point_in_time": True}
        provenance = {
            "source": "g_market_info_1229",
            "point_in_time_quality": "partial",
        }

        def get_fundamentals(self, *_args):
            return {"income_statements": [], "balance_sheets": []}

        class source:
            @staticmethod
            def get_profile(*_args):
                pytest.fail("a frozen live cutoff must not fetch a current-only profile")

    monkeypatch.setattr(gx, "get_gx_market_info_client", lambda: FakeClient())
    monkeypatch.setattr(
        gx,
        "_now_vn",
        lambda: datetime(2026, 8, 19, 10, 1, tzinfo=timezone(timedelta(hours=7))),
    )
    report = gx.get_fundamentals("HPG", "2026-08-19T10:00:00+07:00")
    payload = json.loads(report.split("\n\n", 1)[1])

    assert payload["profile"]["data"] is None
    assert payload["profile"]["unavailable"]["code"] == "CURRENT_ONLY"


@pytest.fixture(autouse=True)
def restore_config():
    original = deepcopy(config_module._config)
    yield
    config_module._config = original
