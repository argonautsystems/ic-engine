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

PriceProvider routes history calls to ``massive`` (Polygon) first, which
returns split-adjusted and dividend-adjusted prices. This preserves the
historical yfinance adjusted-close semantics expected by return calculations.
"""

import logging
from typing import Iterable, List, Optional

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

    NOTE: When PriceProvider routes through Polygon (the default for history),
    returned close prices are split-adjusted AND dividend-adjusted, matching the
    semantics of yfinance.download(auto_adjust=True). When the routing falls
    through to AlphaVantage, prices are also dividend-adjusted (Adjusted Close).
    When the routing falls through to yfinance.Ticker.history (raw close, no
    auto_adjust), prices are NOT dividend-adjusted — daily-return calculations
    will exclude the dividend yield component for that fallback path only.
    """
    syms = [s for s in symbols if s and str(s).strip()]
    if not syms:
        return pd.DataFrame()

    pp = provider or PriceProvider()
    n_days = _resolve_days(days=days, period=period)

    if len(syms) == 1:
        sym = syms[0]
        ohlcv = _rows_to_ohlcv(_history_for(pp, sym, n_days))
        return ohlcv if ohlcv is not None else pd.DataFrame()

    frames: List[pd.DataFrame] = []
    for sym in syms:
        ohlcv = _rows_to_ohlcv(_history_for(pp, sym, n_days))
        if ohlcv is None or ohlcv.empty:
            continue
        ohlcv.columns = pd.MultiIndex.from_product([list(ohlcv.columns), [sym]])
        frames.append(ohlcv)

    if not frames:
        return pd.DataFrame()

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

    NOTE: When PriceProvider routes through Polygon (the default for history),
    returned close prices are split-adjusted AND dividend-adjusted, matching the
    semantics of yfinance.download(auto_adjust=True). When the routing falls
    through to AlphaVantage, prices are also dividend-adjusted (Adjusted Close).
    When the routing falls through to yfinance.Ticker.history (raw close, no
    auto_adjust), prices are NOT dividend-adjusted — daily-return calculations
    will exclude the dividend yield component for that fallback path only.
    """
    syms = [s for s in symbols if s and str(s).strip()]
    if not syms:
        return pd.DataFrame()

    pp = provider or PriceProvider()
    n_days = _resolve_days(days=days, period=period)

    series = {}
    for sym in syms:
        ohlcv = _rows_to_ohlcv(_history_for(pp, sym, n_days))
        if ohlcv is None or "Close" not in ohlcv.columns:
            continue
        col = ohlcv["Close"].dropna()
        if not col.empty:
            series[sym] = col

    panel = pd.DataFrame(series).sort_index()
    # Preserve caller's symbol ordering and failed symbols as all-NaN columns.
    return panel.reindex(columns=syms)
