from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from ic_engine.commands.performance_window import build_performance_window
from ic_engine.commands.performance_window_cache import update_and_slice_panel


def _holdings_file(tmp_path: Path, symbols=("AAA", "BBB")) -> Path:
    holdings = {
        "holdings": [
            {"symbol": sym, "shares": 10 if sym == "AAA" else 5, "asset_type": "equity"}
            for sym in symbols
        ]
    }
    path = tmp_path / ("holdings_" + "_".join(symbols) + ".json")
    path.write_text(json.dumps(holdings), encoding="utf-8")
    return path


class _FakeAnalyzer:
    def __init__(
        self,
        *,
        dividends: dict[str, dict[str, float]] | None = None,
        empty_symbols: set[str] | None = None,
    ):
        self.calls: list[tuple[tuple[str, ...], str, str]] = []
        self.frames: dict[str, pd.DataFrame] = {}
        self.dividends = dividends or {}
        self.empty_symbols = empty_symbols or set()

    @staticmethod
    def _close(sym: str, day: pd.Timestamp) -> float:
        base = {"AAA": 10.0, "BBB": 20.0, "CCC": 30.0}.get(sym, 40.0)
        return base + (day.date() - date(2026, 6, 1)).days

    def fetch_equity_data(self, symbols, start_date, end_date):
        syms = tuple(str(s).upper() for s in symbols)
        self.calls.append((syms, start_date, end_date))
        dates = pd.date_range(start_date, end_date, freq="D")
        frame = pd.DataFrame({"Date": dates})
        fetched = []
        dividends = {}
        for sym in syms:
            dividends[sym] = sum(
                amount
                for event_date, amount in self.dividends.get(sym, {}).items()
                if start_date <= event_date <= end_date
            )
            if sym in self.empty_symbols:
                continue
            fetched.append(sym)
            closes = [self._close(sym, day) for day in dates]
            frame[f"Open_{sym}"] = closes
            frame[f"High_{sym}"] = [x + 0.5 for x in closes]
            frame[f"Low_{sym}"] = [x - 0.5 for x in closes]
            frame[f"Close_{sym}"] = closes
            frame[f"Volume_{sym}"] = 1000
        return pl.from_pandas(frame), dividends, fetched

    def calculate_returns(self, price_data, symbol, annual_dividend=0.0):
        price_pd = price_data.to_pandas()
        col = f"Close_{symbol}"
        if col not in price_pd.columns:
            col = "Close"
        prices = price_pd[col].to_numpy(dtype=float)
        returns = np.diff(prices) / prices[:-1]
        if annual_dividend > 0 and len(prices) > 0:
            returns = returns + (annual_dividend / np.mean(prices)) / 252
        return returns


def test_delta_fetch_only_requests_missing_dates(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    analyzer = _FakeAnalyzer()

    update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-10")
    assert analyzer.calls == [(("AAA",), "2026-06-01", "2026-06-10")]

    update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-14")
    assert analyzer.calls[-1] == (("AAA",), "2026-06-11", "2026-06-14")
    assert len(analyzer.calls) == 2


def test_window_slice_from_cached_panel_matches_uncached_full_fetch_baseline(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "perf-window-incremental")
    analyzer = _FakeAnalyzer()

    # Seed a wider persistent panel, then force the public builder to use it
    # without refetching the whole requested window.
    update_and_slice_panel(analyzer, ["AAA", "BBB"], "2026-06-01", "2026-06-14")

    fake = _FakeAnalyzer()
    monkeypatch.setattr("ic_engine.commands.performance_window.PerformanceAnalyzer", lambda: fake)
    cached = build_performance_window(
        _holdings_file(tmp_path), period="1w", today=date(2026, 6, 14)
    )
    assert fake.calls == []

    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "uncached-panel-baseline"))
    full = _FakeAnalyzer()
    monkeypatch.setattr("ic_engine.commands.performance_window.PerformanceAnalyzer", lambda: full)
    uncached = build_performance_window(
        _holdings_file(tmp_path), period="1w", today=date(2026, 6, 14)
    )
    assert full.calls

    assert (
        cached["sections"]["performance_window"]["holdings"]
        == uncached["sections"]["performance_window"]["holdings"]
    )
    assert (
        cached["sections"]["performance_window"]["totals"]
        == uncached["sections"]["performance_window"]["totals"]
    )


def test_sparse_repair_fetches_only_missing_holes(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    analyzer = _FakeAnalyzer()

    update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-03")
    update_and_slice_panel(analyzer, ["AAA"], "2026-06-05", "2026-06-06")
    analyzer.calls.clear()

    update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-06")
    assert analyzer.calls == [(("AAA",), "2026-06-04", "2026-06-04")]


def test_empty_panel_records_attempted_through_and_does_not_refetch(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    analyzer = _FakeAnalyzer(empty_symbols={"ZZZ"})

    update_and_slice_panel(analyzer, ["ZZZ"], "2026-06-01", "2026-06-03")
    update_and_slice_panel(analyzer, ["ZZZ"], "2026-06-01", "2026-06-03")
    assert analyzer.calls == [(("ZZZ",), "2026-06-01", "2026-06-03")]


def test_cached_window_with_dividend_matches_uncached_full_window(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "perf-window-incremental")
    dividends = {"AAA": {"2026-06-14": 0.42}}

    def fake_events(symbol, start_date, end_date, aggregate_value):
        return [
            {"date": event_date, "amount": amount, "source": "test"}
            for event_date, amount in dividends.get(symbol, {}).items()
            if start_date <= event_date <= end_date
        ]

    monkeypatch.setattr(
        "ic_engine.commands.performance_window_cache._fetch_dividend_events", fake_events
    )
    seed = _FakeAnalyzer(dividends=dividends)
    update_and_slice_panel(seed, ["AAA", "BBB"], "2026-06-01", "2026-06-14")

    cached_analyzer = _FakeAnalyzer(dividends=dividends)
    monkeypatch.setattr(
        "ic_engine.commands.performance_window.PerformanceAnalyzer", lambda: cached_analyzer
    )
    cached = build_performance_window(
        _holdings_file(tmp_path), period="1w", today=date(2026, 6, 14)
    )
    assert cached_analyzer.calls == []

    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "uncached-panel"))
    full_analyzer = _FakeAnalyzer(dividends=dividends)
    monkeypatch.setattr(
        "ic_engine.commands.performance_window.PerformanceAnalyzer", lambda: full_analyzer
    )
    uncached = build_performance_window(
        _holdings_file(tmp_path), period="1w", today=date(2026, 6, 14)
    )
    assert full_analyzer.calls

    cached_section = cached["sections"]["performance_window"]
    uncached_section = uncached["sections"]["performance_window"]
    assert cached_section["holdings"] == uncached_section["holdings"]
    assert cached_section["totals"] == uncached_section["totals"]
    by_symbol = {row["symbol"]: row for row in cached_section["holdings"]}
    assert by_symbol["AAA"]["dividend_per_share"] == 0.42


def test_build_performance_window_new_day_fetches_gap_only(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "perf-window-incremental")
    holdings = _holdings_file(tmp_path)

    first = _FakeAnalyzer()
    monkeypatch.setattr("ic_engine.commands.performance_window.PerformanceAnalyzer", lambda: first)
    build_performance_window(holdings, period="1w", today=date(2026, 6, 14))
    assert first.calls

    second = _FakeAnalyzer()
    monkeypatch.setattr("ic_engine.commands.performance_window.PerformanceAnalyzer", lambda: second)
    build_performance_window(holdings, period="1w", today=date(2026, 6, 15))
    assert second.calls == [
        (("AAA",), "2026-06-15", "2026-06-15"),
        (("BBB",), "2026-06-15", "2026-06-15"),
    ]


def test_same_day_repeat_uses_signed_result_cache_no_provider_call(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "perf-window-incremental")
    first = _FakeAnalyzer()
    monkeypatch.setattr("ic_engine.commands.performance_window.PerformanceAnalyzer", lambda: first)

    holdings = _holdings_file(tmp_path)
    one = build_performance_window(holdings, period="1w", today=date(2026, 6, 14))
    assert first.calls

    second = _FakeAnalyzer()
    monkeypatch.setattr("ic_engine.commands.performance_window.PerformanceAnalyzer", lambda: second)
    two = build_performance_window(holdings, period="1w", today=date(2026, 6, 14))
    assert second.calls == []
    assert two["ic_result"]["hmac"] == one["ic_result"]["hmac"]


def test_new_symbol_triggers_its_fetch_only(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    analyzer = _FakeAnalyzer()

    update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-14")
    assert analyzer.calls == [(("AAA",), "2026-06-01", "2026-06-14")]

    update_and_slice_panel(analyzer, ["AAA", "CCC"], "2026-06-01", "2026-06-14")
    assert analyzer.calls[-1] == (("CCC",), "2026-06-01", "2026-06-14")
    assert len(analyzer.calls) == 2
