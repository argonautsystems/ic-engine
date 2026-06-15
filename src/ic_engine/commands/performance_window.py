# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic time-window portfolio performance.

This command is intentionally non-narrative and LLM-free. It reuses the
existing ``PerformanceAnalyzer.calculate_returns`` math while sourcing prices
from a persistent per-symbol OHLCV panel.  Each call fetches only missing/new
bars via ``PerformanceAnalyzer.fetch_equity_data`` (Massive → provider
fallbacks → yfinance), then slices the requested window and emits a signed v2.5
ic_result envelope with per-holding total returns, contribution, and portfolio
totals.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ic_engine.commands.analyze_performance_polars import PerformanceAnalyzer
from ic_engine.commands.performance_window_cache import (
    load_result_cache,
    result_cache_lock,
    save_result_cache,
    update_and_slice_panel,
)
from ic_engine.runtime.envelope import (
    attach_hmac,
    new_ic_result,
    portfolio_id_for_holdings,
    utc_now_iso,
)
from ic_engine.services.portfolio_utils import load_holdings_list

import re

# Canonical terse tokens still accepted verbatim (and surfaced in the error help).
_PERIOD_ALIASES = {
    "1d": 1,
    "1w": 7,
    "7d": 7,
    "2w": 14,
    "14d": 14,
    "1mo": 30,
    "1m": 30,
    "30d": 30,
    "3mo": 90,
    "3m": 90,
    "quarter": 90,
    "last_quarter": 90,
    "6mo": 180,
    "6m": 180,
    "1y": 365,
    "12mo": 365,
    "2y": 730,
}

# Single-word natural periods (after relative-prefix stripping, e.g. "last week"
# → "week"). Calendar approximations: month≈30d, quarter≈90d, year≈365d.
_WORD_PERIODS = {
    "yesterday": 1,
    "today": 1,
    "day": 1,
    "week": 7,
    "fortnight": 14,
    "month": 30,
    "quarter": 90,
    "semester": 180,
    "year": 365,
    "decade": 3650,
}
_UNIT_DAYS = {"d": 1, "w": 7, "mo": 30, "m": 30, "y": 365}
# Anything that means "the full available provider history".
_MAX_TOKENS = {"max", "all", "all_time", "everything", "lifetime", "ever", "alltime"}


def _normalize_period(token: str) -> str:
    """Lowercase, unify separators, and strip relative/filler prefixes so natural
    phrasings collapse to a canonical form: 'last 20 Years' → '20_years',
    'over the past 6 months' → '6_months', 'this week' → 'week'."""
    t = (token or "").strip().lower()
    t = re.sub(r"[\s\-]+", "_", t)
    for pre in (
        "in_the_", "over_the_", "for_the_", "during_the_", "trailing_",
        "previous_", "past_", "last_", "this_", "the_", "over_", "for_", "in_",
    ):
        while t.startswith(pre):
            t = t[len(pre):]
    return t.strip("_")


def _resolve_period_days(token: str) -> tuple[str, int | None]:
    """Return ('ytd', None) | ('max', None) | ('days', N) for a period token,
    accepting terse tokens, natural words, and 'N unit' phrasings (any N)."""
    raw = (token or "").strip().lower()
    if raw in _PERIOD_ALIASES:
        return ("days", _PERIOD_ALIASES[raw])
    if raw in ("ytd", "year_to_date", "yeartodate"):
        return ("ytd", None)

    t = _normalize_period(token)
    if not t:
        return ("days", _PERIOD_ALIASES["1mo"])
    if t in ("ytd", "year_to_date", "yeartodate"):
        return ("ytd", None)
    if (
        t in _MAX_TOKENS
        or t.startswith("entire")
        or t.startswith("since_inception")
        or "inception" in t
        or "history" in t
        or "all_time" in t
    ):
        return ("max", None)
    if t in _PERIOD_ALIASES:
        return ("days", _PERIOD_ALIASES[t])
    if t in _WORD_PERIODS:
        return ("days", _WORD_PERIODS[t])
    # "N unit": 20years, 6_months, 90_days, 3yr, 2wks ...
    m = re.fullmatch(r"(\d+)_?(d|day|days|w|wk|wks|week|weeks|mo|month|months|m|y|yr|yrs|year|years)", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("d"):
            per = _UNIT_DAYS["d"]
        elif unit.startswith("w"):
            per = _UNIT_DAYS["w"]
        elif unit.startswith("mo") or unit in ("month", "months", "m"):
            per = _UNIT_DAYS["mo"]
        else:
            per = _UNIT_DAYS["y"]
        return ("days", max(n * per, 1))
    raise ValueError(
        f"Unsupported period {token!r}. Use a token (1d, 1w, 1mo, 3mo, 6mo, ytd, "
        "1y, 2y, max), a natural phrase ('last week', 'last month', 'last year', "
        "'last 5 years', 'entire history'), or explicit start_date/end_date."
    )

# "max" must mean provider maximum, not an arbitrary 10-year cap. Request a
# deliberately old equity-history start; provider responses are then clamped to
# their actual earliest returned row per holding.
_PROVIDER_MAX_START = date(1900, 1, 1)


@dataclass(frozen=True)
class ResolvedWindow:
    period: str
    start_date: str
    end_date: str
    requested_start_date: str
    requested_end_date: str


def _ic_engine_version() -> str:
    try:
        from ic_engine import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def _today() -> date:
    """Engine end-of-day anchor for deterministic relative windows."""
    override = (
        os.environ.get("INVESTORCLAW_TODAY") or os.environ.get("IC_ENGINE_TODAY") or ""
    ).strip()
    if override:
        return date.fromisoformat(override)
    return datetime.now(timezone.utc).date()


def _parse_iso_date(raw: str, *, field: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except Exception as exc:
        raise ValueError(f"{field} must be ISO YYYY-MM-DD, got {raw!r}") from exc


def resolve_window(
    *,
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    today: date | None = None,
) -> ResolvedWindow:
    """Resolve deterministic period/date inputs to an inclusive date range.

    Relative periods end at engine EOD (today). Explicit ``end_date`` defaults
    to today. ``max`` requests the full provider history and is later clamped
    to the earliest actual provider date returned for the holdings.
    """
    anchor = today or _today()
    token = (period or "").strip().lower()

    explicit_start = _parse_iso_date(start_date, field="start_date") if start_date else None
    explicit_end = _parse_iso_date(end_date, field="end_date") if end_date else None

    if explicit_start and token:
        raise ValueError("Provide either period or start_date, not both")

    if explicit_start:
        end = explicit_end or anchor
        if explicit_start > end:
            raise ValueError("start_date must be on or before end_date")
        return ResolvedWindow(
            period="custom",
            start_date=explicit_start.isoformat(),
            end_date=end.isoformat(),
            requested_start_date=explicit_start.isoformat(),
            requested_end_date=end.isoformat(),
        )

    if explicit_end and not token:
        raise ValueError("end_date requires start_date or period")

    end = explicit_end or anchor
    token = token or "1mo"

    kind, days = _resolve_period_days(token)
    if kind == "ytd":
        start = date(end.year, 1, 1)
    elif kind == "max":
        start = _PROVIDER_MAX_START
    else:
        start = end - timedelta(days=int(days))

    if start > end:
        raise ValueError("resolved start_date must be on or before end_date")
    return ResolvedWindow(
        period=token,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        requested_start_date=start.isoformat(),
        requested_end_date=end.isoformat(),
    )


def _as_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _close_series(price_pd: pd.DataFrame, symbol: str) -> pd.Series:
    if isinstance(price_pd.columns, pd.MultiIndex):
        if ("Close", symbol) in price_pd.columns:
            return price_pd[("Close", symbol)]
        if (symbol, "Close") in price_pd.columns:
            return price_pd[(symbol, "Close")]
    col = f"Close_{symbol}"
    if col in price_pd.columns:
        return price_pd[col]
    if "Close" in price_pd.columns:
        return price_pd["Close"]
    return pd.Series(dtype="float64")


def _first_last_valid(series: pd.Series) -> tuple[float | None, float | None, str | None, str | None]:
    cleaned = pd.to_numeric(series, errors="coerce").dropna()
    if cleaned.empty:
        return None, None, None, None
    start_idx = cleaned.index[0]
    end_idx = cleaned.index[-1]
    start = _as_float(cleaned.iloc[0])
    end = _as_float(cleaned.iloc[-1])
    start_day = pd.Timestamp(start_idx).date().isoformat()
    end_day = pd.Timestamp(end_idx).date().isoformat()
    return start, end, start_day, end_day


def _compound_return_pct(returns: np.ndarray) -> float | None:
    """Compound analyzer-return array into a window total-return percent."""
    if returns is None or len(returns) == 0:
        return None
    arr = np.asarray(returns, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) == 0:
        return None
    return float((np.prod(1.0 + arr) - 1.0) * 100.0)


def _holding_shares(holding: dict[str, Any], end_price: float | None) -> float:
    shares = _as_float(holding.get("shares") or holding.get("quantity"))
    market_value = _as_float(holding.get("market_value") or holding.get("value"))
    if shares is None and market_value is not None and end_price:
        shares = market_value / end_price
    return shares if shares is not None else 0.0


def build_performance_window(
    holdings_file: str | Path,
    *,
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Return a signed deterministic performance-window envelope."""
    resolved = resolve_window(period=period, start_date=start_date, end_date=end_date, today=today)
    holdings_path = Path(holdings_file).expanduser()
    holdings = load_holdings_list(str(holdings_path))
    equity_holdings = [
        h for h in holdings if str(h.get("asset_type") or h.get("asset_class") or "").lower() == "equity"
    ]
    symbols = [str(h.get("symbol", "")).upper() for h in equity_holdings if h.get("symbol")]
    if not symbols:
        raise ValueError("No equity holdings found for performance-window analysis")

    holdings_hash = portfolio_id_for_holdings(holdings_path)
    # Serialize load→compute→save for this exact window so concurrent same-key
    # writers (warmth cron + agent request) don't race the atomic replace.
    with result_cache_lock(
        holdings_hash, resolved.start_date, resolved.end_date, resolved.period
    ):
        cached_envelope = load_result_cache(
            holdings_hash, resolved.start_date, resolved.end_date, resolved.period
        )
        if cached_envelope is not None:
            return cached_envelope
        signed = _compute_window_envelope(resolved, equity_holdings, symbols, holdings_hash)
        save_result_cache(
            holdings_hash, resolved.start_date, resolved.end_date, signed, resolved.period
        )
        return signed


def _compute_window_envelope(
    resolved: ResolvedWindow,
    equity_holdings: list[dict[str, Any]],
    symbols: list[str],
    holdings_hash: str,
) -> dict[str, Any]:
    """Fetch the incremental panel and build a signed performance-window envelope."""
    analyzer = PerformanceAnalyzer()
    price_pl, dividends, fetched_symbols = update_and_slice_panel(
        analyzer, symbols, resolved.start_date, resolved.end_date
    )
    price_pd = price_pl.to_pandas()
    date_col = next((c for c in ("Date", "Datetime", "index") if c in price_pd.columns), None)
    if date_col:
        price_pd = price_pd.set_index(pd.to_datetime(price_pd[date_col]))
    else:
        price_pd.index = pd.to_datetime(price_pd.index)
    price_pd = price_pd.sort_index()

    fetched_set = {str(s).upper() for s in fetched_symbols}
    rows: list[dict[str, Any]] = []
    total_start_value = 0.0
    total_pnl = 0.0

    for h in equity_holdings:
        symbol = str(h.get("symbol", "")).upper()
        if symbol not in fetched_set:
            continue

        series = _close_series(price_pd, symbol)
        start_price, end_price, actual_start, actual_end = _first_last_valid(series)
        if start_price is None or end_price is None:
            continue

        # Reuse the analyzer's total-return calculation path. This is the
        # load-bearing correctness point: dividends are included exactly where
        # analyze_performance_polars includes them, avoiding a divergent
        # price-only implementation.
        returns = analyzer.calculate_returns(
            price_pl,
            symbol,
            annual_dividend=float(dividends.get(symbol, 0.0) or 0.0),
        )
        ret_pct = _compound_return_pct(returns)
        if ret_pct is None:
            continue

        shares = _holding_shares(h, end_price)
        start_value = shares * start_price
        pnl = start_value * (ret_pct / 100.0)
        end_value = start_value + pnl
        total_start_value += start_value
        total_pnl += pnl

        dividend_per_share = _as_float(dividends.get(symbol)) or 0.0
        rows.append(
            {
                "symbol": symbol,
                "shares": round(shares, 8),
                "start_price": round(start_price, 4),
                "end_price": round(end_price, 4),
                "return_pct": round(ret_pct, 4),
                "start_value": round(start_value, 2),
                "end_value": round(end_value, 2),
                "pnl": round(pnl, 2),
                "contribution": round(pnl, 2),
                "dividend_per_share": round(dividend_per_share, 6),
                "dividend_income": round(dividend_per_share * shares, 2),
                "actual_start_date": actual_start,
                "actual_end_date": actual_end,
            }
        )

    if not rows:
        raise ValueError("No price/return data available for requested window")

    actual_starts = [r["actual_start_date"] for r in rows if r.get("actual_start_date")]
    actual_ends = [r["actual_end_date"] for r in rows if r.get("actual_end_date")]
    clamped_start = min(actual_starts) if actual_starts else resolved.start_date
    clamped_end = max(actual_ends) if actual_ends else resolved.end_date
    total_end_value = total_start_value + total_pnl
    total_return_pct = (total_pnl / total_start_value) * 100.0 if total_start_value else None
    movers = sorted(
        [r for r in rows if r.get("contribution") is not None],
        key=lambda r: abs(float(r["contribution"])),
        reverse=True,
    )[:10]

    section = {
        "period": resolved.period,
        "start_date": clamped_start,
        "end_date": clamped_end,
        "requested_start_date": resolved.requested_start_date,
        "requested_end_date": resolved.requested_end_date,
        "holdings": rows,
        "totals": {
            "period": resolved.period,
            "start_date": clamped_start,
            "end_date": clamped_end,
            "total_return_pct": round(total_return_pct, 4) if total_return_pct is not None else None,
            "total_pnl": round(total_pnl, 2),
            "start_value": round(total_start_value, 2),
            "end_value": round(total_end_value, 2),
            "top_movers": [
                {
                    "symbol": r["symbol"],
                    "return_pct": r["return_pct"],
                    "contribution": r["contribution"],
                    "pnl": r["pnl"],
                }
                for r in movers
            ],
        },
        "calculation": "Returns reuse PerformanceAnalyzer.calculate_returns (total-return path including analyzer dividend treatment) on an incremental per-symbol OHLCV panel.",
        "disclaimer": "Deterministic historical window using cached OHLCV provider data; only missing/new bars are fetched via the provider fallback chain.",
    }
    generated_at = utc_now_iso()
    envelope = {
        "schema_version": "v2.5.0",
        "generated_at": generated_at,
        "portfolio_id": holdings_hash,
        "ic_result": new_ic_result(command="performance-window", run_id=str(uuid.uuid4())),
        "sections": {"performance_window": section},
        "section_meta": {
            "performance_window": {
                "computed_at": generated_at,
                "ttl_seconds": 300,
                "source": "performance_window",
                "status": "success",
                # Signed engine-version + request identity so the result cache can
                # validate freshness (version miss / TTL) without breaking HMAC.
                "engine_version": _ic_engine_version(),
                "period": resolved.period,
                "requested_start_date": resolved.requested_start_date,
                "requested_end_date": resolved.requested_end_date,
            }
        },
        "failed_sections": [],
    }
    return attach_hmac(envelope)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic explicit-window portfolio performance")
    parser.add_argument("holdings_file")
    parser.add_argument("output_file", nargs="?", help="Optional JSON output path")
    parser.add_argument("--period", default=None, help="1d, 1w, 2w, 1mo, 3mo, 6mo, ytd, 1y, 2y, max")
    parser.add_argument("--start", dest="start_date", default=None, help="ISO YYYY-MM-DD inclusive start")
    parser.add_argument("--end", dest="end_date", default=None, help="ISO YYYY-MM-DD inclusive end")
    parser.add_argument("--verbose", action="store_true", help="Accepted for router compatibility; output remains deterministic JSON")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    try:
        envelope = build_performance_window(
            args.holdings_file,
            period=args.period,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        if args.output_file:
            out = Path(args.output_file).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        print(json.dumps(envelope, separators=(",", ":")))
        return 0
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        print(
            json.dumps(
                {
                    "ic_result": {
                        "script": "performance_window.py",
                        "exit_code": 1,
                        "duration_ms": duration_ms,
                    },
                    "error": str(exc),
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
