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

    def fetch_equity_data(self, symbols, start_date, end_date, *, exact_range=False):
        syms = tuple(str(s).upper() for s in symbols)
        self.calls.append((syms, start_date, end_date))
        self.exact_range_calls = getattr(self, "exact_range_calls", [])
        self.exact_range_calls.append(exact_range)
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
        ], True

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
    # Per-symbol fetches run concurrently, so compare order-insensitively.
    assert sorted(second.calls) == sorted(
        [
            (("AAA",), "2026-06-15", "2026-06-15"),
            (("BBB",), "2026-06-15", "2026-06-15"),
        ]
    )


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


def test_fetch_equity_data_exact_range_requests_tight_provider_days(monkeypatch):
    """Guard the pure-delta seam at the PROVIDER layer.

    The earlier blocker: the cache asked for a 1-day delta but the real
    analyzer inflated it to ``get_ohlcv_panel(days=max(..., 30))``, so the
    provider refetched a 30-day rolling window. This asserts the actual ``days``
    handed to the provider, not just the analyzer-wrapper range.
    """
    from ic_engine.commands import analyze_performance_polars as mod

    captured: dict[str, int | None] = {}

    def fake_panel(symbols, *, days=None, period=None, provider=None):
        captured["days"] = days
        end = pd.Timestamp.now().normalize()
        idx = pd.date_range(end=end, periods=max(int(days or 1), 1), freq="D")
        return pd.DataFrame(
            {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1}, index=idx
        )

    monkeypatch.setattr("ic_engine.providers.price_panel.get_ohlcv_panel", fake_panel)

    class _NoDividends:
        def get_dividends(self, *args, **kwargs):
            return []

    monkeypatch.setattr(
        "ic_engine.providers.price_provider.MassiveProvider", _NoDividends
    )

    class _Ticker:
        dividends = pd.Series(dtype="float64")

    monkeypatch.setattr(mod.yf, "Ticker", lambda *a, **k: _Ticker())

    today = pd.Timestamp.now().normalize()
    start = (today - pd.Timedelta(days=3)).date().isoformat()
    end = today.date().isoformat()
    analyzer = mod.PerformanceAnalyzer()

    # Pure-delta path: provider asked for ONLY the 4-day tail (start..today).
    analyzer.fetch_equity_data(["AAA"], start, end, exact_range=True)
    assert captured["days"] == 4

    # Default full-portfolio path keeps the padded 30-day floor.
    analyzer.fetch_equity_data(["AAA"], start, end)
    assert captured["days"] == 30


def test_panel_delta_uses_exact_range_path(monkeypatch, tmp_path):
    """The incremental panel must invoke the analyzer with ``exact_range=True``."""
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    analyzer = _FakeAnalyzer()
    update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-10")
    assert analyzer.exact_range_calls == [True]


def test_result_cache_version_miss_forces_recompute(monkeypatch, tmp_path):
    """An engine-version change must invalidate the result cache (no stale shape)."""
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "perf-window-incremental")
    holdings = _holdings_file(tmp_path)

    first = _FakeAnalyzer()
    monkeypatch.setattr("ic_engine.commands.performance_window.PerformanceAnalyzer", lambda: first)
    build_performance_window(holdings, period="1w", today=date(2026, 6, 14))
    assert first.calls

    # Bump the engine version both producers read; the prior cache entry is now
    # a version miss. The warm panel means no provider refetch, but the result
    # is re-signed with the new version rather than served stale.
    monkeypatch.setattr(
        "ic_engine.commands.performance_window._ic_engine_version", lambda: "test-bumped"
    )
    monkeypatch.setattr(
        "ic_engine.commands.performance_window_cache._engine_version", lambda: "test-bumped"
    )
    second = _FakeAnalyzer()
    monkeypatch.setattr("ic_engine.commands.performance_window.PerformanceAnalyzer", lambda: second)
    out = build_performance_window(holdings, period="1w", today=date(2026, 6, 14))
    # Recomputed under the new version (not the stale-version cache entry).
    assert out["section_meta"]["performance_window"]["engine_version"] == "test-bumped"


def test_yf_batch_fallback_uses_tight_date_range_not_year_period(monkeypatch):
    """B1: a 1-day delta must not pull a 1-year yfinance period at the provider."""
    import sys
    import types

    from ic_engine.providers import price_panel

    captured: dict[str, object] = {}

    fake_yf = types.ModuleType("yfinance")

    def fake_download(symbols, **kwargs):
        captured.update(kwargs)
        return pd.DataFrame()  # empty -> fallback returns {} cleanly

    fake_yf.download = fake_download
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    price_panel._yf_batch_fallback(["AAA"], days=1)
    assert "period" not in captured  # no coarse 1y bucket
    assert "start" in captured and "end" in captured
    span = (pd.Timestamp(captured["end"]) - pd.Timestamp(captured["start"])).days
    assert span <= 10  # tight tail, not ~365


def test_massive_get_history_limit_covers_span_for_one_day_delta(monkeypatch):
    """B2: days=1 must request enough rows to include today, not limit=1."""
    from ic_engine.providers.price_provider import MassiveProvider

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")

    class _FakeAggClient:
        def __init__(self):
            self.kwargs = None

        def list_aggs(self, **kwargs):
            self.kwargs = kwargs
            return []  # empty -> get_history returns [] before dividend adjust

    provider = MassiveProvider(api_key="test-key")
    fake_client = _FakeAggClient()
    provider._client = fake_client
    provider._with_retry = lambda _label, fn: fn()

    provider.get_history("AAA", days=1)
    assert fake_client.kwargs is not None
    # limit must cover the yesterday..today span (+pad), not be truncated to 1.
    assert fake_client.kwargs["limit"] >= 2


class _SplitAnalyzer(_FakeAnalyzer):
    """Exactly-2x price jump after the split date to trip split detection."""

    SPLIT_DATE = date(2026, 6, 11)

    def _close(self, sym: str, day: pd.Timestamp) -> float:  # type: ignore[override]
        return 200.0 if day.date() >= self.SPLIT_DATE else 100.0


def test_split_triggers_full_window_rebuild(monkeypatch, tmp_path):
    """M1: a split/adjustment discontinuity must rebuild the whole window on one
    basis rather than merge old-basis cached bars with new-basis fresh bars."""
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    from ic_engine.commands import performance_window_cache as cache

    analyzer = _SplitAnalyzer()
    # Seed pre-split bars.
    cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-10")
    analyzer.calls.clear()

    # Extend across the split; detection must force a full-window refetch.
    cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-14")
    full_refetch = (("AAA",), "2026-06-01", "2026-06-14")
    assert full_refetch in analyzer.calls

    meta = cache._load_meta("AAA")
    assert meta.get("split_rebuilt_at") == "2026-06-14"


def test_transient_fetch_exception_is_retryable_not_finalized(monkeypatch, tmp_path):
    """M-B: a provider exception must not mark the range permanently attempted."""
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    from ic_engine.commands import performance_window_cache as cache

    monkeypatch.setattr(cache, "_fetch_dividend_events", lambda *a, **k: ([], True))

    class _Flaky(_FakeAnalyzer):
        def __init__(self):
            super().__init__()
            self.fail_next = True

        def fetch_equity_data(self, symbols, start_date, end_date, *, exact_range=False):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("transient provider error")
            return super().fetch_equity_data(
                symbols, start_date, end_date, exact_range=exact_range
            )

    analyzer = _Flaky()
    # First call: the only range raises -> nothing cached, nothing finalized.
    _pl, _div, fetched1 = cache.update_and_slice_panel(
        analyzer, ["AAA"], "2026-06-02", "2026-06-05"
    )
    assert fetched1 == []
    # Second call: the same range must be retried (not a permanent hole).
    _pl, _div, fetched2 = cache.update_and_slice_panel(
        analyzer, ["AAA"], "2026-06-02", "2026-06-05"
    )
    assert "AAA" in fetched2


def test_dividend_correction_triggers_rebuild_no_double_count(monkeypatch, tmp_path):
    """M-A: a corrected amount on an existing ex-date inside the cached range must
    rebuild and replace (not accumulate) the dividend."""
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    from ic_engine.commands import performance_window_cache as cache

    state = {"amount": 1.00}

    def fake_events(sym, s, e, agg):
        ev = {"date": "2026-06-03", "amount": state["amount"], "source": "test"}
        return ([ev] if s <= ev["date"] <= e else []), True

    monkeypatch.setattr(cache, "_fetch_dividend_events", fake_events)
    analyzer = _FakeAnalyzer()

    # Seed: caches bars through 06-05 + dividend 1.00 on 06-03.
    _pl, div1, _f = cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-05")
    assert abs(div1["AAA"] - 1.00) < 1e-9

    # Correction on a LATER day: same ex-date, amount now 1.50 (inside cached range).
    state["amount"] = 1.50
    _pl, div2, _f = cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-06")
    # Replaced, not accumulated (would be 2.50 if double-counted).
    assert abs(div2["AAA"] - 1.50) < 1e-9
    assert cache._load_meta("AAA").get("split_rebuilt_at") == "2026-06-06"


def test_typed_no_data_is_finalized_not_retried_forever(monkeypatch, tmp_path):
    """M-B2: a typed 'No data returned' outcome is a successful-empty response and
    must finalize the hole (delisted/closed span), not retry every call."""
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    from ic_engine.commands import performance_window_cache as cache

    monkeypatch.setattr(cache, "_fetch_dividend_events", lambda *a, **k: ([], True))

    class _NoData(_FakeAnalyzer):
        def fetch_equity_data(self, symbols, start_date, end_date, *, exact_range=False):
            self.calls.append(
                (tuple(str(s).upper() for s in symbols), start_date, end_date)
            )
            raise ValueError("No data returned from Yahoo Finance. Check symbols and date range.")

    analyzer = _NoData()
    cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-02", "2026-06-05")
    n_after_first = len(analyzer.calls)
    cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-02", "2026-06-05")
    assert len(analyzer.calls) == n_after_first  # finalized, no re-fetch


def test_removed_dividend_clears_store_and_rebuilds(monkeypatch, tmp_path):
    """M-A2: an authoritative empty snapshot (dividend removed) must clear the
    stored event and rebuild, not retain stale income."""
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    from ic_engine.commands import performance_window_cache as cache

    state = {"present": True}

    def fake_events(sym, s, e, agg):
        ev = {"date": "2026-06-03", "amount": 1.00, "source": "test"}
        present = state["present"] and s <= ev["date"] <= e
        return ([ev] if present else []), True

    monkeypatch.setattr(cache, "_fetch_dividend_events", fake_events)
    analyzer = _FakeAnalyzer()

    _pl, d1, _f = cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-05")
    assert abs(d1["AAA"] - 1.00) < 1e-9

    state["present"] = False  # provider authoritatively reports the dividend gone
    _pl, d2, _f = cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-06")
    assert abs(d2["AAA"] - 0.0) < 1e-9  # store cleared, no stale income
    assert cache._load_meta("AAA").get("split_rebuilt_at") == "2026-06-06"


def test_massive_authoritative_empty_short_circuits_yfinance(monkeypatch):
    """R6 blocker: a Massive authoritative-empty must WIN (clear), not be
    overridden by a stale yfinance dividend."""
    import sys
    import types

    from ic_engine.commands import performance_window_cache as cache

    class _FakeMassive:
        def __init__(self, *a, **k):
            pass

        def get_dividends_authoritative(self, s, limit=1000):
            return [], True  # authoritative: no dividends

    monkeypatch.setattr(
        "ic_engine.providers.price_provider.MassiveProvider", _FakeMassive
    )

    fake_yf = types.ModuleType("yfinance")

    class _Tk:
        @property
        def dividends(self):
            return pd.Series([0.5], index=[pd.Timestamp("2026-06-03")])  # stale

    fake_yf.Ticker = lambda s: _Tk()
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    events, ok = cache._fetch_dividend_events("AAA", "2026-06-01", "2026-06-10", 0.0)
    assert ok is True and events == []  # Massive empty won; yfinance not consulted


def test_massive_transient_with_empty_yfinance_is_not_authoritative(monkeypatch):
    """R6 blocker: a Massive transient failure must not become authoritative via
    an empty yfinance response (which would wipe the store)."""
    import sys
    import types

    from ic_engine.commands import performance_window_cache as cache

    class _FakeMassive:
        def __init__(self, *a, **k):
            pass

        def get_dividends_authoritative(self, s, limit=1000):
            return [], False  # transient failure

    monkeypatch.setattr(
        "ic_engine.providers.price_provider.MassiveProvider", _FakeMassive
    )

    fake_yf = types.ModuleType("yfinance")

    class _Tk:
        @property
        def dividends(self):
            return pd.Series(dtype="float64")  # empty -> cannot confirm

    fake_yf.Ticker = lambda s: _Tk()
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    _events, ok = cache._fetch_dividend_events("AAA", "2026-06-01", "2026-06-10", 0.0)
    assert ok is False  # not authoritative -> caller keeps prior store


def test_dividend_after_cached_tail_triggers_rebuild(monkeypatch, tmp_path):
    """R5-#3: a new dividend whose ex-date lands AFTER the cached tail still
    retro-adjusts older cached bars, so it must trigger a rebuild."""
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    from ic_engine.commands import performance_window_cache as cache

    state = {"events": []}
    monkeypatch.setattr(
        cache,
        "_fetch_dividend_events",
        lambda sym, s, e, agg: ([ev for ev in state["events"] if s <= ev["date"] <= e], True),
    )
    analyzer = _FakeAnalyzer()

    # Seed bars 06-01..06-05 (panel_min=06-01), no dividends yet.
    cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-05")
    # A dividend appears on 06-08 (after the cached tail, before the new end).
    state["events"] = [{"date": "2026-06-08", "amount": 0.50, "source": "test"}]
    cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-10")
    assert cache._load_meta("AAA").get("split_rebuilt_at") == "2026-06-10"


def test_transient_dividend_failure_keeps_prior_store(monkeypatch, tmp_path):
    """M-A2(rev): a transient dividend failure (ok False) must NOT wipe the store
    and must not advance dividend_synced_through (so it retries)."""
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    from ic_engine.commands import performance_window_cache as cache

    state = {"ok": True}

    def fake_events(sym, s, e, agg):
        if not state["ok"]:
            return [], False  # transient failure
        ev = {"date": "2026-06-03", "amount": 1.00, "source": "test"}
        return ([ev] if s <= ev["date"] <= e else []), True

    monkeypatch.setattr(cache, "_fetch_dividend_events", fake_events)
    analyzer = _FakeAnalyzer()

    cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-05")
    state["ok"] = False
    _pl, d2, _f = cache.update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-06")
    assert abs(d2["AAA"] - 1.00) < 1e-9  # prior store kept
    assert "dividend_synced_through" in cache._load_meta("AAA")
    assert cache._load_meta("AAA")["dividend_synced_through"] == "2026-06-05"  # not advanced


def test_narrow_then_wide_same_end_keeps_older_dividends(monkeypatch, tmp_path):
    """M1(rev): a narrow window must not poison a later wider same-end window's
    dividends (full history is synced through end, not just the narrow tail)."""
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    from ic_engine.commands import performance_window_cache as cache

    # Full dated history; the older event is outside the narrow window but inside
    # the wide one.
    all_events = [
        {"date": "2026-01-15", "amount": 1.00, "source": "test"},
        {"date": "2026-06-13", "amount": 0.25, "source": "test"},
    ]
    monkeypatch.setattr(
        cache,
        "_fetch_dividend_events",
        lambda sym, s, e, agg: ([ev for ev in all_events if s <= ev["date"] <= e], True),
    )
    analyzer = _FakeAnalyzer()

    # Narrow window first (only the 06-13 event falls inside).
    _pl, narrow_div, _f = cache.update_and_slice_panel(
        analyzer, ["AAA"], "2026-06-10", "2026-06-14"
    )
    assert abs(narrow_div["AAA"] - 0.25) < 1e-9

    # Wider window, SAME end: must still see the older 2026-01-15 dividend.
    _pl, wide_div, _f = cache.update_and_slice_panel(
        analyzer, ["AAA"], "2026-01-01", "2026-06-14"
    )
    assert abs(wide_div["AAA"] - 1.25) < 1e-9


def test_large_portfolio_processes_symbols_concurrently(monkeypatch, tmp_path):
    """Scale guard: a large portfolio must NOT serialize per-symbol provider
    round-trips (the 442-symbol timeout regression). Verify the per-symbol work
    runs concurrently and every symbol is processed."""
    import threading
    import time

    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    from ic_engine.commands import performance_window_cache as cache

    monkeypatch.setattr(cache, "_fetch_dividend_events", lambda *a, **k: ([], True))

    lock = threading.Lock()
    state = {"cur": 0, "max": 0}

    class _Concurrent(_FakeAnalyzer):
        def fetch_equity_data(self, symbols, start_date, end_date, *, exact_range=False):
            with lock:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.02)  # hold the slot so overlap is observable
            try:
                return super().fetch_equity_data(
                    symbols, start_date, end_date, exact_range=exact_range
                )
            finally:
                with lock:
                    state["cur"] -= 1

    syms = [f"SY{i:03d}" for i in range(40)]
    analyzer = _Concurrent()
    _pl, _div, fetched = cache.update_and_slice_panel(
        analyzer, syms, "2026-06-01", "2026-06-05"
    )
    assert len(fetched) == 40  # all processed
    assert state["max"] > 1  # ran concurrently, not serialized


def test_new_symbol_triggers_its_fetch_only(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    analyzer = _FakeAnalyzer()

    update_and_slice_panel(analyzer, ["AAA"], "2026-06-01", "2026-06-14")
    assert analyzer.calls == [(("AAA",), "2026-06-01", "2026-06-14")]

    update_and_slice_panel(analyzer, ["AAA", "CCC"], "2026-06-01", "2026-06-14")
    assert analyzer.calls[-1] == (("CCC",), "2026-06-01", "2026-06-14")
    assert len(analyzer.calls) == 2
