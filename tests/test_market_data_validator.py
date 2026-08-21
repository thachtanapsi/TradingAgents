"""Tests for the deterministic market-data verification snapshot (#830/#881)."""

from __future__ import annotations

import pandas as pd
import pytest

import tradingagents.dataflows.market_data_validator as validator


def _sample_ohlcv() -> pd.DataFrame:
    dates = pd.bdate_range("2026-04-01", "2026-05-20")
    closes = [100 + i for i in range(len(dates))]
    return pd.DataFrame({
        "Date": dates,
        "Open": [c - 0.5 for c in closes],
        "High": [c + 1.0 for c in closes],
        "Low": [c - 1.0 for c in closes],
        "Close": closes,
        "Volume": [1_000_000 + i for i in range(len(dates))],
    })


@pytest.mark.unit
class TestVerifiedSnapshot:
    def test_price_reference_is_completed_close_with_pit_provenance(
        self, monkeypatch
    ):
        data = _sample_ohlcv()
        data.attrs["gx_provenance"] = {
            "source": "gx_market_info",
            "currency": "VND",
            "price_unit": "VND",
            "point_in_time_quality": "exact",
        }
        monkeypatch.setattr(validator, "load_ohlcv", lambda _s, _d: data)

        reference = validator.get_verified_price_reference(
            "mwg", "2026-05-13T10:00:00+07:00"
        )

        assert reference == {
            "status": "available",
            "ticker": "MWG",
            "close": "130",
            "currency": "VND",
            "price_unit": "VND",
            "session_date": "2026-05-13",
            "analysis_cutoff": "2026-05-13T10:00:00+07:00",
            "source": "gx_market_info",
            "point_in_time_quality": "exact",
        }

    def test_price_reference_rejects_nonpositive_close(self, monkeypatch):
        data = _sample_ohlcv()
        data.loc[data.index[-1], "Close"] = 0
        monkeypatch.setattr(validator, "load_ohlcv", lambda _s, _d: data)

        with pytest.raises(ValueError, match="positive finite"):
            validator.get_verified_price_reference("MWG", "2026-05-20")

    def test_unprovenanced_fallback_frame_is_not_mislabeled_gx_exact(
        self, monkeypatch
    ):
        data = _sample_ohlcv()
        data["Close"] = data["Close"].astype(float)
        data.loc[data.index[-1], "Close"] = 63.3
        monkeypatch.setattr(validator, "load_ohlcv", lambda _s, _d: data)
        monkeypatch.setattr(
            "tradingagents.dataflows.config.get_config",
            lambda: {
                "tool_vendors": {
                    "get_stock_data": "gx_market_info,yfinance",
                }
            },
        )

        reference = validator.get_verified_price_reference("MWG", "2026-05-20")

        assert reference["close"] == "63.3"
        assert reference["currency"] is None
        assert reference["price_unit"] is None
        assert reference["point_in_time_quality"] == "partial"

    def test_excludes_future_rows(self, monkeypatch):
        data = pd.concat([
            _sample_ohlcv(),
            pd.DataFrame({"Date": [pd.Timestamp("2026-06-01")], "Open": [999.0],
                          "High": [999.0], "Low": [999.0], "Close": [999.0], "Volume": [999]}),
        ], ignore_index=True)
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: data)

        snap = validator.build_verified_market_snapshot("COF", "2026-05-13")
        assert "Verified market data snapshot for COF" in snap
        assert "Requested analysis date: 2026-05-13" in snap
        assert "Latest trading row used: 2026-05-13" in snap
        assert "999.00" not in snap          # future row excluded
        assert "boll_lb" in snap             # indicators present

    def test_uses_previous_trading_day_when_date_is_weekend(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        # 2026-05-16 is a Saturday; latest row should be Fri 2026-05-15
        snap = validator.build_verified_market_snapshot("COF", "2026-05-16")
        assert "Latest trading row used: 2026-05-15" in snap
        assert "Recent verified closes" in snap

    def test_includes_completed_candle_timestamped_at_same_day_close(
        self, monkeypatch
    ):
        data = pd.DataFrame(
            {
                "Date": pd.to_datetime(
                    ["2026-05-12 15:00:00", "2026-05-13 15:00:00"]
                ),
                "Open": [100.0, 101.0],
                "High": [102.0, 103.0],
                "Low": [99.0, 100.0],
                "Close": [101.0, 102.0],
                "Volume": [1_000, 2_000],
            }
        )
        monkeypatch.setattr(validator, "load_ohlcv", lambda _s, _d: data)

        snap = validator.build_verified_market_snapshot("COF", "2026-05-13")

        assert "Latest trading row used: 2026-05-13" in snap
        assert "| Close | 102.00 |" in snap

    def test_raises_when_no_rows_on_or_before_date(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        with pytest.raises(ValueError):
            validator.build_verified_market_snapshot("COF", "2020-01-01")

    def test_raises_on_empty_data(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: pd.DataFrame())
        with pytest.raises(ValueError):
            validator.build_verified_market_snapshot("COF", "2026-05-13")

    def test_look_back_window_capped_at_30(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        snap = validator.build_verified_market_snapshot("COF", "2026-05-20", look_back_days=999)
        # last-N closes table has at most 30 data rows
        close_rows = [ln for ln in snap.splitlines() if ln.startswith("| 2026-")]
        assert 0 < len(close_rows) <= 30

    def test_live_quote_is_separate_and_never_changes_daily_indicators(
        self, monkeypatch
    ):
        monkeypatch.setattr(validator, "load_ohlcv", lambda _s, _d: _sample_ohlcv())
        monkeypatch.setattr(
            "tradingagents.dataflows.config.get_config",
            lambda: {"tool_vendors": {"get_stock_data": "gx_market_info"}},
        )

        class Client:
            provenance = {"source_timestamp": "2026-05-20T09:05:00Z"}

            def get_quote(self, symbol, cutoff):
                assert symbol == "COF"
                assert cutoff == "2026-05-20T16:05:00+07:00"
                return {
                    "last_price": "999999",
                    "price_change": "1000",
                    "is_final": False,
                    "source_updated_at": "2026-05-20T09:05:00Z",
                }

        monkeypatch.setattr(
            "tradingagents.dataflows.gx_market_info.get_gx_market_info_client",
            lambda: Client(),
        )

        snap = validator.build_verified_market_snapshot(
            "COF",
            "2026-05-20T16:05:00+07:00",
            include_live_quote=True,
        )

        assert "Live quote at frozen cutoff (not used in daily indicators)" in snap
        assert "| last_price | 999999 |" in snap
        assert "is_final: false" in snap
        # The completed daily close remains the verified row, not the quote.
        assert "| Close | 135 |" in snap


@pytest.mark.unit
class TestTool:
    def test_tool_delegates_to_builder(self, monkeypatch):
        from tradingagents.agents.utils.market_data_validation_tools import (
            get_verified_market_snapshot,
        )
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        out = get_verified_market_snapshot.invoke(
            {"symbol": "COF", "curr_date": "2026-05-20"}
        )
        assert "Verified market data snapshot for COF" in out
