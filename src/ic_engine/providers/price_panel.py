#!/usr/bin/env python3
# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0


"""
Pandas adapter on top of PriceProvider for analyzers that historically
called yfinance.download() directly.

PriceProvider returns plain dicts / lists of dicts to keep its surface
pandas-free (see price_provider.py docstring). The optimizer, performance
analyzer, and tax-aware rebalancer all need pandas DataFrames shaped like
``yfinance.download(...)`` output. This module bridges the two without
pulling pandas into PriceProvider itself.

The DataFrame shapes returned here intentionally match yfinance.download
with ``auto_adjust=True`` so call sites can keep their existing extraction
logic:

    1 symbol  →  flat columns ['Open', 'High', 'Low', 'Close', 'Volume']
    N symbols →  MultiIndex columns [('Open', 'AAPL'), ('Close', 'AAPL'), ...]

PriceProvider routes history calls to ``massive`` first, which
returns split-adjusted and dividend-adjusted prices. This preserves the
historical yfinance adjusted-close semantics expected by return calculations.
"""

import logging
from typing import Dict, Iterable, List, Optional

import pandas as pd

from .price_provider import PriceProvider

logger = logging.getLogger(__name__)


# yfinance period strings → calendar days. Used by callers that pass
# ``period="1y"`` etc.; PriceProvider only takes a day count.
_PERIOD_DAYS = {
    "1d": 1,
    "5d": 5,
    "1mo": 31,
    "3mo": 92,
    "6mo": 183,
    "1y": 365,
    "2y": 730,
    "5y": 1825,
    "10y": 3650,
    "ytd": 365,
    "max": 3650,
}

_OHLCV_FIELDS = ("Open", "High", "Low", "Close", "Volume")


def _resolve_days(*, days: Optional[int], period: Optional[str]) -> int:
    if days is not None:
        return int(days)
    if period is not None:
        if period not in _PERIOD_DAYS:
            raise ValueError(f"Unknown period {period!r}; valid: {sorted(_PERIOD_DAYS)}")
        return _PERIOD_DAYS[period]
    return 365


def _history_for(provider: PriceProvider, symbol: str, days: int) -> List[dict]:
    try:
        return provider.get_history(symbol, days=days) or []
    except Exception as e:
        logger.warning(f"PriceProvider.get_history({symbol}) failed: {e}")
        return []


def _massive_happy_path_provider() -> PriceProvider:
    pp = PriceProvider(primary="massive")
    # price_panel owns the fallback phase so yfinance can be called once in
    # batch, instead of through PriceProvider's per-symbol history chain.
    pp._fallback_names = []
    return pp


def _rows_to_ohlcv(rows: List[dict]) -> Optional[pd.DataFrame]:
    """Convert a PriceProvider history list into a date-indexed OHLCV DataFrame."""
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    out = pd.DataFrame(index=df.index)
    for src, dst in (
        ("open", "Open"),
        ("high", "High"),
        ("low", "Low"),
        ("close", "Close"),
        ("volume", "Volume"),
    ):
        if src in df.columns:
            out[dst] = pd.to_numeric(df[src], errors="coerce")
    return out if not out.empty else None


def _date_str(date_idx) -> str:
    return date_idx.strftime("%Y-%m-%d") if hasattr(date_idx, "strftime") else str(date_idx)[:10]


def _row_float(row, *names: str, default: float = 0.0) -> float:
    for name in names:
        val = row.get(name)
        if val is not None and not pd.isna(val):
            return float(val)
    return default


def _row_int(row, *names: str, default: int = 0) -> int:
    for name in names:
        val = row.get(name)
        if val is not None and not pd.isna(val):
            return int(val)
    return default


def _append_yf_rows(out: Dict[str, List[Dict]], sym: str, data: pd.DataFrame) -> None:
    for date_idx, row in data.iterrows():
        close = row.get("Close", row.get("close"))
        if close is None or pd.isna(close):
            continue
        out[sym].append(
            {
                "date": _date_str(date_idx),
                "open": _row_float(row, "Open", "open"),
                "high": _row_float(row, "High", "high"),
                "low": _row_float(row, "Low", "low"),
                "close": float(close),
                "volume": _row_int(row, "Volume", "volume"),
                "symbol": sym,
                "provider": "yfinance",
            }
        )


def _yf_batch_fallback(symbols: List[str], days: int) -> Dict[str, List[Dict]]:
    """Batch fallback for symbols that PriceProvider could not resolve.

    Single ``yf.download()`` call for all symbols at once, matching the
    pre-refactor behavior. Used when the Massive-first happy path returns
    empty for one or more symbols; avoids the per-symbol rate-limit cascade
    through AlphaVantage/Finnhub/yfinance.Ticker.history.

    Returns dict mapping each requested symbol to a list of OHLCV row dicts
    in the PriceProvider format. Symbols absent from yfinance response get
    empty lists.
    """
    out: Dict[str, List[Dict]] = {sym: [] for sym in symbols}
    if not symbols:
        return out

    try:
        import yfinance as yf
    except ImportError:
        return out

    # Date-bound the fallback fetch to the requested tail instead of flooring
    # every request to a 1-year period: a one-day incremental delta must not
    # refetch a full year of bars at the provider layer. ``end`` is exclusive in
    # yfinance, so add a day; pad the start by 4 days for weekend/holiday holes.
    end_dt = pd.Timestamp.now().normalize() + pd.Timedelta(days=1)
    start_dt = end_dt - pd.Timedelta(days=max(int(days) + 4, 1))

    try:
        yf_syms = [s.replace(".", "-") for s in symbols]
        reverse = {yf_syms[i]: symbols[i] for i in range(len(symbols))}
        data = yf.download(
            yf_syms if len(yf_syms) > 1 else yf_syms[0],
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
            threads=False,
        )
    except Exception as e:
        logger.warning(f"yf.download batch fallback failed: {e}")
        return out

    if data is None or data.empty:
        return out

    if len(yf_syms) == 1:
        sym = symbols[0]
        if isinstance(data.columns, pd.MultiIndex):
            try:
                data = data.xs(yf_syms[0], axis=1, level=1, drop_level=True)
            except Exception as e:
                logger.warning(f"yf batch fallback parse({sym}): {e}")
                return out
        _append_yf_rows(out, sym, data)
        return out

    # Multi-symbol shape: MultiIndex columns like (Open, AAPL), (Close, AAPL).
    for yf_sym in yf_syms:
        sym = reverse[yf_sym]
        try:
            sym_data = data.xs(yf_sym, axis=1, level=1, drop_level=True)
            if sym_data is None or sym_data.empty:
                continue
            _append_yf_rows(out, sym, sym_data)
        except Exception as e:
            logger.warning(f"yf batch fallback parse({sym}): {e}")

    return out


def get_ohlcv_panel(
    symbols: Iterable[str],
    *,
    days: Optional[int] = None,
    period: Optional[str] = None,
    provider: Optional[PriceProvider] = None,
) -> pd.DataFrame:
    """Return OHLCV panel matching ``yfinance.download(symbols, ..., auto_adjust=True)``.

    Single symbol → flat columns ['Open', 'High', 'Low', 'Close', 'Volume'].
    Multiple symbols → MultiIndex columns ``[(metric, symbol)]``.

    Symbols that fail to fetch are retained as all-NaN columns.

    NOTE: In the default adapter path, PriceProvider is limited to Massive for
    the per-symbol happy path. Missing symbols are then fetched in one
    yfinance.download(auto_adjust=True) batch so fallback remains split- and
    dividend-adjusted without per-symbol provider cascades.
    """
    syms = [s for s in symbols if s and str(s).strip()]
    if not syms:
        return pd.DataFrame()

    pp = provider or _massive_happy_path_provider()
    n_days = _resolve_days(days=days, period=period)

    frames: List[pd.DataFrame] = []
    missing_syms: List[str] = []
    for sym in syms:
        ohlcv = _rows_to_ohlcv(_history_for(pp, sym, n_days))
        if ohlcv is None or ohlcv.empty:
            missing_syms.append(sym)
            continue
        if len(syms) == 1:
            frames.append(ohlcv)
        else:
            ohlcv.columns = pd.MultiIndex.from_product([list(ohlcv.columns), [sym]])
            frames.append(ohlcv)

    if missing_syms:
        logger.info(f"price_panel: batch-fallback {len(missing_syms)} symbols via yfinance")
        fallback_rows = _yf_batch_fallback(missing_syms, n_days)
        for sym, rows in fallback_rows.items():
            ohlcv = _rows_to_ohlcv(rows)
            if ohlcv is None or ohlcv.empty:
                continue
            if len(syms) == 1:
                frames.append(ohlcv)
            else:
                ohlcv.columns = pd.MultiIndex.from_product([list(ohlcv.columns), [sym]])
                frames.append(ohlcv)

    if not frames:
        return pd.DataFrame()

    if len(syms) == 1:
        return frames[0]

    combined = pd.concat(frames, axis=1).sort_index()
    # Reorder columns to mirror yfinance: outer level metrics in OHLCV order,
    # inner level symbols in caller-supplied order.
    metrics = [m for m in _OHLCV_FIELDS if m in combined.columns.get_level_values(0)]
    columns = [(m, s) for m in metrics for s in syms]
    return combined.reindex(columns=pd.MultiIndex.from_tuples(columns))


def get_close_panel(
    symbols: Iterable[str],
    *,
    days: Optional[int] = None,
    period: Optional[str] = None,
    provider: Optional[PriceProvider] = None,
) -> pd.DataFrame:
    """Return wide close-price panel: index=date, columns=symbol.

    Replaces the historical idiom ``yf.download(symbols, period=p)["Adj Close"]``
    plus the single-symbol ``.to_frame()`` dance in callers like ``optimize.py``.

    NOTE: In the default adapter path, PriceProvider is limited to Massive for
    the per-symbol happy path. Missing symbols are then fetched in one
    yfinance.download(auto_adjust=True) batch so fallback remains split- and
    dividend-adjusted without per-symbol provider cascades.
    """
    syms = [s for s in symbols if s and str(s).strip()]
    if not syms:
        return pd.DataFrame()

    pp = provider or _massive_happy_path_provider()
    n_days = _resolve_days(days=days, period=period)

    series = {}
    missing_syms: List[str] = []
    for sym in syms:
        ohlcv = _rows_to_ohlcv(_history_for(pp, sym, n_days))
        if ohlcv is None or "Close" not in ohlcv.columns:
            missing_syms.append(sym)
            continue
        col = ohlcv["Close"].dropna()
        if not col.empty:
            series[sym] = col
        else:
            missing_syms.append(sym)

    if missing_syms:
        logger.info(f"price_panel: batch-fallback {len(missing_syms)} symbols via yfinance")
        fallback_rows = _yf_batch_fallback(missing_syms, n_days)
        for sym, rows in fallback_rows.items():
            ohlcv = _rows_to_ohlcv(rows)
            if ohlcv is None or "Close" not in ohlcv.columns:
                continue
            col = ohlcv["Close"].dropna()
            if not col.empty:
                series[sym] = col

    panel = pd.DataFrame(series).sort_index()
    # Preserve caller's symbol ordering and failed symbols as all-NaN columns.
    return panel.reindex(columns=syms)
