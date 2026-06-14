#!/usr/bin/env python3
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


"""
Peer / Benchmark Relative Analytics — InvestorClaw

Compares portfolio factor exposures against benchmarks (SPY, QQQ, IWM, AGG)
and detects style drift.

Outputs:
  - beta_matrix:      portfolio beta vs each benchmark
  - active_share:     0.5 * sum(|w_portfolio - w_benchmark|) (vs SPY)
  - sector_deviation: portfolio sector weights vs SPY sector weights
  - style_scores:     value-vs-growth, large-vs-small, quality
  - factor_tilts:     P/E, P/B, EPS growth tilts
  - drift_alerts:     rolling 90-day sector drift flags (if prior snapshots exist)

Algorithm reuses `PerformanceAnalyzer.calculate_beta()` from
`analyze_performance_polars.py`.

Argv:
  <holdings.json> [performance.json] [--benchmark SPY]
                  [--compare QQQ,IWM,AGG] [output.json]
"""

from __future__ import annotations

import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# ─── Path bootstrap ─────────────────────────────────────────────────────────
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import yfinance as yf  # noqa: E402

from ic_engine.commands.analyze_performance_polars import PerformanceAnalyzer  # noqa: E402
from ic_engine.config.schema import normalize_portfolio, validate_portfolio  # noqa: E402
from ic_engine.rendering.disclaimer_wrapper import DisclaimerWrapper  # noqa: E402
from ic_engine.services.portfolio_utils import (  # noqa: E402
    fetch_benchmark_returns,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ─── Constants ──────────────────────────────────────────────────────────────

DEFAULT_BENCHMARK = "SPY"
DEFAULT_COMPARES = ("QQQ", "IWM", "AGG")

OVERWEIGHT_THRESHOLD = 0.05  # +5% sector delta → OVERWEIGHT
UNDERWEIGHT_THRESHOLD = -0.05  # -5% sector delta → UNDERWEIGHT
DRIFT_THRESHOLD = 0.03  # 3% sector weight change → drift alert

# SPY sector weights (S&P 500, reflects market benchmark).
# Updated April 2026. Source: SPDR S&P 500 ETF Trust factsheet.
# Used as static baseline when sector weights for SPY constituents are not
# directly queried (yfinance sector fields aren't reliably available for the
# ETF holdings list).
SPY_SECTOR_WEIGHTS: Dict[str, float] = {
    "Technology": 0.300,
    "Financial Services": 0.133,
    "Healthcare": 0.110,
    "Consumer Cyclical": 0.105,
    "Communication Services": 0.090,
    "Industrials": 0.080,
    "Consumer Defensive": 0.060,
    "Energy": 0.040,
    "Utilities": 0.025,
    "Real Estate": 0.023,
    "Basic Materials": 0.024,
}

# SPY aggregate fundamentals (April 2026 approximation; used as reference
# when benchmark-level yfinance `info` fields are unreliable).
SPY_FUNDAMENTALS: Dict[str, float] = {
    "pe_ratio": 22.0,
    "pb_ratio": 3.8,
    "eps_growth": 0.10,
}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _get_portfolio(raw: Dict) -> Dict:
    """Return normalized portfolio dict (with equity/bond/... keys)."""
    norm = normalize_portfolio(raw)
    validate_portfolio(norm)
    return norm["portfolio"]


def _equity_positions(portfolio: Dict) -> List[Tuple[str, Dict]]:
    """Return (symbol, entry) list of equity positions with market_value > 0."""
    equity = portfolio.get("equity", {}) or {}
    rows: List[Tuple[str, Dict]] = []
    for sym, entry in equity.items():
        if not isinstance(entry, dict):
            continue
        mv = entry.get("market_value") or 0.0
        try:
            mv = float(mv)
        except (TypeError, ValueError):
            mv = 0.0
        if mv > 0:
            rows.append((sym, entry))
    return rows


def _weights_from_positions(rows: List[Tuple[str, Dict]]) -> Dict[str, float]:
    """Compute normalized equity weights by market value."""
    total = sum(float(e.get("market_value") or 0.0) for _, e in rows)
    if total <= 0:
        return {}
    return {sym: float(e.get("market_value") or 0.0) / total for sym, e in rows}


def _sector_weights(rows: List[Tuple[str, Dict]], weights: Dict[str, float]) -> Dict[str, float]:
    """Aggregate per-symbol weights into sector weights."""
    sectors: Dict[str, float] = {}
    for sym, entry in rows:
        sector = entry.get("sector") or "Unknown"
        if not sector:
            sector = "Unknown"
        sectors[sector] = sectors.get(sector, 0.0) + weights.get(sym, 0.0)
    return sectors


def _fetch_symbol_info(symbol: str) -> Dict:
    """Pull the yfinance `info` dict defensively. Returns {} on failure."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        return info if isinstance(info, dict) else {}
    except Exception as e:
        logger.debug(f"{symbol}: info fetch failed: {e}")
        return {}


def _normalise_fundamental_info(raw: Dict) -> Dict:
    """Normalize provider fundamentals to yfinance-style info keys."""
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    aliases = {
        "price_to_earnings": "trailingPE",
        "pe_ratio": "trailingPE",
        "price_to_book": "priceToBook",
        "price_to_sales": "priceToSalesTrailing12Months",
        "dividend_yield": "dividendYield",
        "market_cap": "marketCap",
        "return_on_equity": "returnOnEquity",
        "return_on_assets": "returnOnAssets",
        "debt_to_equity": "debtToEquity",
        "net_margin": "profitMargins",
        "gross_margin": "grossMargins",
        "operating_margin": "operatingMargins",
    }
    for src, dst in aliases.items():
        if out.get(dst) is None and out.get(src) is not None:
            out[dst] = out[src]
    return out


def _fetch_symbol_info_provider(symbol: str, massive=None, finnhub=None) -> Dict:
    if massive is not None:
        try:
            merged = {}
            overview = massive.get_ticker_overview(symbol) or {}
            ratios = massive.get_financial_ratios(symbol) or {}
            merged.update(overview)
            merged.update(ratios)
            norm = _normalise_fundamental_info(merged)
            if norm:
                return norm
        except Exception as e:
            logger.debug(f"{symbol}: Massive fundamentals failed: {e}")
    if finnhub is not None:
        try:
            metric = finnhub._client.company_basic_financials(symbol, "all") or {}
            norm = _normalise_fundamental_info(metric.get("metric") or metric)
            if norm:
                return norm
        except Exception as e:
            logger.debug(f"{symbol}: Finnhub fundamentals failed: {e}")
    return {}

def _bulk_fetch_info(symbols: List[str]) -> Dict[str, Dict]:
    """Fetch fundamentals with Massive/Finnhub primary and yfinance last fallback."""
    out: Dict[str, Dict] = {}
    if not symbols:
        return out

    massive = None
    finnhub = None
    try:
        from ic_engine.providers.price_provider import FinnhubProvider, MassiveProvider

        try:
            massive = MassiveProvider()
        except Exception as e:
            logger.debug(f"Massive fundamentals unavailable: {e}")
        try:
            finnhub = FinnhubProvider()
        except Exception as e:
            logger.debug(f"Finnhub fundamentals unavailable: {e}")
    except Exception as e:
        logger.debug(f"Provider fundamentals unavailable: {e}")

    def _one(sym: str) -> Dict:
        info = _fetch_symbol_info_provider(sym, massive=massive, finnhub=finnhub)
        if info:
            return info
        return _fetch_symbol_info(sym)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_one, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                out[sym] = _normalise_fundamental_info(fut.result())
            except Exception as e:
                logger.debug(f"{sym}: info future failed: {e}")
                out[sym] = {}
    return out


def _safe_float(v, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None:
            return default
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _weighted_avg(values: Dict[str, Optional[float]], weights: Dict[str, float]) -> Optional[float]:
    """Weighted mean over symbols for which a value is available."""
    num = 0.0
    den = 0.0
    for sym, v in values.items():
        if v is None:
            continue
        w = weights.get(sym, 0.0)
        if w <= 0:
            continue
        num += w * float(v)
        den += w
    if den <= 0:
        return None
    return num / den


# ─── Core computations ─────────────────────────────────────────────────────


def compute_beta_matrix(
    rows: List[Tuple[str, Dict]],
    weights: Dict[str, float],
    benchmarks: List[str],
    period: str = "1y",
) -> Dict[str, Optional[float]]:
    """Portfolio beta vs each benchmark = weighted sum of per-symbol betas.

    Reuses PerformanceAnalyzer.calculate_beta() per symbol against each
    benchmark's returns series. Symbols with insufficient data are dropped
    from the weighted average (weights renormalized over valid subset).
    """
    analyzer = PerformanceAnalyzer()
    symbols = [s for s, _ in rows]

    # Fetch price data once for all equity symbols
    end_date = datetime.now().strftime("%Y-%m-%d")
    # Approx 1y back for 1y period
    start_dt = datetime.now()
    if period == "1y":
        start_date = (start_dt.replace(year=start_dt.year - 1)).strftime("%Y-%m-%d")
    else:
        start_date = (start_dt.replace(year=start_dt.year - 1)).strftime("%Y-%m-%d")

    try:
        price_data, dividends, ok_symbols = analyzer.fetch_equity_data(
            symbols, start_date, end_date
        )
    except Exception as e:
        logger.warning(f"Could not fetch equity data for beta matrix: {e}")
        return {f"vs_{b.lower()}": None for b in benchmarks}

    # Compute per-symbol returns once (cache)
    per_symbol_returns: Dict[str, np.ndarray] = {}
    for sym in ok_symbols:
        try:
            r = analyzer.calculate_returns(price_data, sym, annual_dividend=dividends.get(sym, 0.0))
            per_symbol_returns[sym] = r
        except Exception as e:
            logger.debug(f"{sym}: returns calc failed: {e}")

    beta_matrix: Dict[str, Optional[float]] = {}
    for bench in benchmarks:
        bench_returns = fetch_benchmark_returns(bench, period=period)
        if bench_returns.size == 0:
            logger.warning(f"No benchmark data for {bench} — skipping")
            beta_matrix[f"vs_{bench.lower()}"] = None
            continue

        # Weighted average of per-symbol betas
        num = 0.0
        den = 0.0
        valid_count = 0
        for sym, rets in per_symbol_returns.items():
            w = weights.get(sym, 0.0)
            if w <= 0:
                continue
            try:
                res = analyzer.calculate_beta(
                    rets,
                    symbol=sym,
                    benchmark=bench,
                    benchmark_returns=bench_returns,
                )
                if res.get("_valid") and res.get("beta") is not None:
                    num += w * float(res["beta"])
                    den += w
                    valid_count += 1
            except Exception as e:
                logger.debug(f"{sym} vs {bench} beta calc failed: {e}")

        if valid_count == 0:
            logger.warning(
                f"No valid betas computed for {bench} (tried {len(per_symbol_returns)} symbols)"
            )
            beta_matrix[f"vs_{bench.lower()}"] = None
        else:
            beta_matrix[f"vs_{bench.lower()}"] = (num / den) if den > 0 else None

    return beta_matrix


def compute_sector_deviation(
    portfolio_sectors: Dict[str, float],
    benchmark_sectors: Dict[str, float] = None,
) -> Dict[str, Dict]:
    """Compare portfolio sector weights vs benchmark sector weights."""
    bench = benchmark_sectors or SPY_SECTOR_WEIGHTS

    out: Dict[str, Dict] = {}
    all_sectors = set(portfolio_sectors.keys()) | set(bench.keys())

    for sec in sorted(all_sectors):
        p_w = round(portfolio_sectors.get(sec, 0.0), 4)
        b_w = round(bench.get(sec, 0.0), 4)
        delta = round(p_w - b_w, 4)

        entry: Dict = {
            "portfolio": p_w,
            "spy": b_w,
            "delta": delta,
        }
        if delta >= OVERWEIGHT_THRESHOLD:
            entry["flag"] = "OVERWEIGHT"
        elif delta <= UNDERWEIGHT_THRESHOLD:
            entry["flag"] = "UNDERWEIGHT"
        out[sec] = entry

    return out


def compute_active_share(
    portfolio_weights_by_symbol: Dict[str, float],
    benchmark: str = "SPY",
) -> float:
    """Active Share = 0.5 * sum(|w_portfolio - w_benchmark|)

    Since constituent-level SPY weights aren't cheaply available at the
    symbol level via yfinance, we approximate via sector-level active
    share (a well-known proxy): if every overlap were perfect at the
    sector level, active share would be ~0; if the portfolio held no
    SPY constituents at all, active share would be ~1.

    Callers pass symbol-level weights; we approximate using the
    complement of the overlap with SPY's ~top symbols when available
    (falls back to 1.0 - sum of overlaps with SPY top names).
    """
    # Top SPY constituents (April 2026 approximation). Only used as an
    # overlap anchor; non-matching portfolio symbols are treated as
    # non-overlap, which tends to overstate active share slightly for
    # broad portfolios — acceptable for this metric.
    SPY_TOP: Dict[str, float] = {
        "AAPL": 0.072,
        "MSFT": 0.068,
        "NVDA": 0.065,
        "AMZN": 0.037,
        "META": 0.025,
        "GOOGL": 0.022,
        "GOOG": 0.019,
        "TSLA": 0.018,
        "BRK.B": 0.016,
        "AVGO": 0.015,
        "JPM": 0.013,
        "UNH": 0.011,
        "XOM": 0.011,
        "V": 0.010,
        "JNJ": 0.009,
        "LLY": 0.015,
    }

    all_symbols = set(portfolio_weights_by_symbol.keys()) | set(SPY_TOP.keys())
    total = 0.0
    for sym in all_symbols:
        p = portfolio_weights_by_symbol.get(sym, 0.0)
        b = SPY_TOP.get(sym, 0.0)
        total += abs(p - b)
    # For non-top names in portfolio, they contribute full weight to total.
    # For SPY holdings not in portfolio, we only have the top ~16; remaining
    # ~0.56 of SPY isn't enumerated. We clip to [0, 1].
    active_share = min(1.0, max(0.0, 0.5 * total))
    return round(active_share, 4)


def compute_factor_tilts(
    info_map: Dict[str, Dict],
    weights: Dict[str, float],
) -> Dict[str, Dict]:
    """Value vs growth factor tilts via P/E, P/B, EPS growth averages."""

    pe_vals: Dict[str, Optional[float]] = {}
    pb_vals: Dict[str, Optional[float]] = {}
    eps_g_vals: Dict[str, Optional[float]] = {}

    for sym, info in info_map.items():
        pe_vals[sym] = _safe_float(info.get("trailingPE") or info.get("forwardPE"))
        pb_vals[sym] = _safe_float(info.get("priceToBook"))
        eps_g_vals[sym] = _safe_float(
            info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
        )

    port_pe = _weighted_avg(pe_vals, weights)
    port_pb = _weighted_avg(pb_vals, weights)
    port_eps_g = _weighted_avg(eps_g_vals, weights)

    def _tilt(port: Optional[float], bench: float, higher_is_growth: bool) -> str:
        if port is None:
            return "UNKNOWN"
        if higher_is_growth:
            return "GROWTH" if port > bench else "VALUE"
        else:
            # For P/E, P/B: higher = growth-leaning, lower = value-leaning
            return "GROWTH" if port > bench else "VALUE"

    return {
        "pe_ratio": {
            "portfolio": round(port_pe, 3) if port_pe is not None else None,
            "spy": SPY_FUNDAMENTALS["pe_ratio"],
            "tilt": _tilt(port_pe, SPY_FUNDAMENTALS["pe_ratio"], False),
        },
        "pb_ratio": {
            "portfolio": round(port_pb, 3) if port_pb is not None else None,
            "spy": SPY_FUNDAMENTALS["pb_ratio"],
            "tilt": _tilt(port_pb, SPY_FUNDAMENTALS["pb_ratio"], False),
        },
        "eps_growth": {
            "portfolio": round(port_eps_g, 4) if port_eps_g is not None else None,
            "spy": SPY_FUNDAMENTALS["eps_growth"],
            "tilt": _tilt(port_eps_g, SPY_FUNDAMENTALS["eps_growth"], True),
        },
    }


def compute_style_scores(
    info_map: Dict[str, Dict],
    weights: Dict[str, float],
) -> Dict[str, Optional[float]]:
    """Compute high-level style scores.

    value_vs_growth: Normalize average EPS growth vs SPY into [0, 1].
                     0 = pure value, 1 = pure growth.
    large_vs_small:  Weighted market cap relative to large-cap threshold.
                     1.0 = all mega-cap, 0.0 = all small-cap.
    quality:         Weighted average of ROE (as a quality proxy),
                     clipped into [0, 1].
    """
    # Value vs growth score from EPS growth and P/E
    eps_g_vals: Dict[str, Optional[float]] = {}
    mkt_caps: Dict[str, Optional[float]] = {}
    roes: Dict[str, Optional[float]] = {}

    for sym, info in info_map.items():
        eps_g_vals[sym] = _safe_float(
            info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
        )
        mkt_caps[sym] = _safe_float(info.get("marketCap"))
        roe = _safe_float(info.get("returnOnEquity"))
        roes[sym] = roe

    avg_eps_g = _weighted_avg(eps_g_vals, weights)
    # Map avg_eps_g: <=0 → 0.0 (pure value), >=0.25 → 1.0 (pure growth),
    # linear in between.
    if avg_eps_g is None:
        vg_score: Optional[float] = None
    else:
        vg_score = max(0.0, min(1.0, avg_eps_g / 0.25))
        vg_score = round(vg_score, 3)

    avg_mcap = _weighted_avg(mkt_caps, weights)
    # 2B = small-cap cutoff, 200B = mega-cap cutoff. Log-scale map.
    if avg_mcap is None or avg_mcap <= 0:
        ls_score: Optional[float] = None
    else:
        lo = np.log10(2e9)
        hi = np.log10(2e11)
        x = (np.log10(avg_mcap) - lo) / (hi - lo)
        ls_score = round(float(max(0.0, min(1.0, x))), 3)

    avg_roe = _weighted_avg(roes, weights)
    if avg_roe is None:
        quality_score: Optional[float] = None
    else:
        # ROE 0% → 0.0, 30%+ → 1.0, linear
        quality_score = round(max(0.0, min(1.0, avg_roe / 0.30)), 3)

    return {
        "value_vs_growth": vg_score,
        "large_vs_small": ls_score,
        "quality": quality_score,
    }


def detect_drift_alerts(
    current_sectors: Dict[str, float],
    prior_sectors: Optional[Dict[str, float]],
) -> Optional[List[Dict]]:
    """Emit per-sector drift alerts if a prior snapshot is provided.

    Returns None when prior snapshots are unavailable (caller can omit the
    `drift_alerts` key from output).
    """
    if not prior_sectors:
        return None

    alerts: List[Dict] = []
    all_sectors = set(current_sectors.keys()) | set(prior_sectors.keys())

    for sec in sorted(all_sectors):
        cur = current_sectors.get(sec, 0.0)
        prev = prior_sectors.get(sec, 0.0)
        drift = cur - prev
        if abs(drift) < DRIFT_THRESHOLD:
            continue

        # Flag semantics
        if drift > 0 and cur > SPY_SECTOR_WEIGHTS.get(sec, 0.0) + OVERWEIGHT_THRESHOLD:
            flag = "INCREASING_OVERWEIGHT"
        elif drift > 0:
            flag = "INCREASING"
        elif drift < 0 and cur < SPY_SECTOR_WEIGHTS.get(sec, 0.0) + UNDERWEIGHT_THRESHOLD:
            flag = "DECREASING_UNDERWEIGHT"
        else:
            flag = "DECREASING"

        alerts.append(
            {
                "period": "90d",
                "sector": sec,
                "prev_weight": round(prev, 4),
                "current_weight": round(cur, 4),
                "drift": round(drift, 4),
                "flag": flag,
            }
        )

    return alerts


def _load_prior_sector_snapshot(
    candidates: List[Path],
) -> Optional[Dict[str, float]]:
    """Look for a prior peer_analysis output to seed the 90-day baseline.

    `candidates` is a list of candidate paths to try, in priority order.
    Typical inputs include the currently-targeted output_file (so repeat
    runs compare against the previous content) and the reports_dir's
    conventional `peer_analysis.json`.

    Each candidate is probed for either an explicit `_sector_snapshot`
    key or a derivable map from `sector_deviation[sector].portfolio`.
    Returns None if no usable prior snapshot is found.
    """
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            path = Path(candidate)
            if not path.exists():
                continue
            with open(path) as f:
                prior = json.load(f)
            data = prior.get("data", prior)

            # Prefer explicit snapshot if present
            snap = data.get("_sector_snapshot")
            if isinstance(snap, dict) and snap:
                return {k: float(v) for k, v in snap.items()}

            # Otherwise derive from sector_deviation.portfolio
            sd = data.get("sector_deviation") or {}
            if isinstance(sd, dict) and sd:
                return {
                    sec: float(entry.get("portfolio", 0.0))
                    for sec, entry in sd.items()
                    if isinstance(entry, dict)
                }
        except Exception as e:
            logger.debug(f"No usable prior snapshot at {candidate}: {e}")
    return None


# ─── Main entry ─────────────────────────────────────────────────────────────


def run_peer_analysis(
    holdings_file: str,
    performance_file: Optional[str] = None,
    benchmark: str = DEFAULT_BENCHMARK,
    compare: Optional[List[str]] = None,
    output_file: Optional[str] = None,
) -> Dict:
    """Run the full peer analysis and return the result dict."""

    compare = compare or list(DEFAULT_COMPARES)
    benchmarks = [benchmark] + [b for b in compare if b != benchmark]

    # Load holdings
    with open(holdings_file) as f:
        raw = json.load(f)
    portfolio = _get_portfolio(raw)

    # Equity positions + weights
    rows = _equity_positions(portfolio)
    if not rows:
        logger.warning("No equity positions found; peer analysis skipped.")
        result = {
            "as_of": datetime.now().isoformat(timespec="seconds"),
            "benchmark": benchmark,
            "compare": compare,
            "error": "no_equity_positions",
        }
        return result

    weights = _weights_from_positions(rows)
    portfolio_sectors = _sector_weights(rows, weights)

    # 1. Beta matrix — reuse calculate_beta() per symbol, weighted average
    logger.info(f"Computing beta matrix vs {benchmarks}")
    beta_matrix = compute_beta_matrix(rows, weights, benchmarks)

    # 2. Sector deviation vs SPY
    sector_deviation = compute_sector_deviation(portfolio_sectors)

    # 3. Active share (sector-proxied with top SPY constituents)
    active_share = compute_active_share(weights, benchmark=benchmark)

    # 4. Fetch provider fundamentals for factor tilts + style scores (parallel)
    logger.info(f"Fetching provider fundamentals for {len(rows)} symbols")
    info_map = _bulk_fetch_info([s for s, _ in rows])

    factor_tilts = compute_factor_tilts(info_map, weights)
    style_scores = compute_style_scores(info_map, weights)

    # 5. Drift alerts (requires prior snapshot)
    # Probe, in priority order: the actual output file (so repeat runs
    # into the same target compare against last run), the reports_dir's
    # conventional peer_analysis.json, and the performance_file's sibling.
    candidates: List[Path] = []
    if output_file:
        candidates.append(Path(output_file))
        candidates.append(Path(output_file).parent / "peer_analysis.json")
    if performance_file:
        candidates.append(Path(performance_file).parent / "peer_analysis.json")
    candidates.append(Path(holdings_file).parent / "peer_analysis.json")

    prior_sectors = _load_prior_sector_snapshot(candidates)
    drift_alerts = detect_drift_alerts(portfolio_sectors, prior_sectors)

    # Round sector_deviation portfolio + spy floats were already rounded
    result: Dict = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "benchmark": benchmark,
        "compare": compare,
        "holdings_analyzed": len(rows),
        "beta_matrix": {
            k: (round(v, 4) if isinstance(v, (int, float)) else v) for k, v in beta_matrix.items()
        },
        "active_share": active_share,
        "sector_deviation": sector_deviation,
        "style_scores": style_scores,
        "factor_tilts": factor_tilts,
        # Preserve a compact sector snapshot so the next run can compute drift.
        "_sector_snapshot": {k: round(v, 4) for k, v in portfolio_sectors.items()},
    }

    if drift_alerts is not None:
        result["drift_alerts"] = drift_alerts
    else:
        result["drift_alerts_note"] = (
            "Prior sector snapshot not found; drift_alerts unavailable "
            "on first run. Re-run after the snapshot is persisted."
        )

    # Optional: acknowledge that performance.json was supplied (reserved for
    # future use — e.g. pulling portfolio-level returns directly rather than
    # recomputing via yfinance).
    if performance_file and os.path.exists(performance_file):
        result["performance_input"] = str(performance_file)

    # Write output
    if output_file:
        DisclaimerWrapper.wrap_and_save(result, output_file, "Peer / Benchmark Analysis")
        logger.info(f"Peer analysis saved to {output_file}")

    return result


def _build_compact_summary(result: Dict) -> Dict:
    """Build a compact stdout summary (2–3 KB)."""
    sec_dev = result.get("sector_deviation", {}) or {}
    over = [
        {"sector": s, "delta": v.get("delta")}
        for s, v in sec_dev.items()
        if isinstance(v, dict) and v.get("flag") == "OVERWEIGHT"
    ][:10]
    under = [
        {"sector": s, "delta": v.get("delta")}
        for s, v in sec_dev.items()
        if isinstance(v, dict) and v.get("flag") == "UNDERWEIGHT"
    ][:10]

    compact = {
        "as_of": result.get("as_of"),
        "benchmark": result.get("benchmark"),
        "holdings_analyzed": result.get("holdings_analyzed"),
        "beta_matrix": result.get("beta_matrix"),
        "active_share": result.get("active_share"),
        "style_scores": result.get("style_scores"),
        "factor_tilts": {
            k: {
                "portfolio": v.get("portfolio"),
                "spy": v.get("spy"),
                "tilt": v.get("tilt"),
            }
            for k, v in (result.get("factor_tilts") or {}).items()
        },
        "overweight_sectors": over,
        "underweight_sectors": under,
    }
    if "drift_alerts" in result:
        compact["drift_alerts"] = result["drift_alerts"][:10]
    elif "drift_alerts_note" in result:
        compact["drift_alerts_note"] = result["drift_alerts_note"]
    return compact


# ─── Argv parsing ───────────────────────────────────────────────────────────


def _parse_argv(argv: List[str]):
    """Parse:
    <holdings.json> [performance.json] [--benchmark SPY]
                    [--compare QQQ,IWM,AGG] [output.json]
    """
    benchmark = DEFAULT_BENCHMARK
    compare: Optional[List[str]] = None

    positional: List[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--benchmark" and i + 1 < len(argv):
            benchmark = argv[i + 1].upper()
            i += 2
            continue
        if tok == "--compare" and i + 1 < len(argv):
            compare = [s.strip().upper() for s in argv[i + 1].split(",") if s.strip()]
            i += 2
            continue
        # Anything else is a positional path
        positional.append(tok)
        i += 1

    if not positional:
        raise SystemExit(
            "Usage: peer_analysis.py <holdings.json> [performance.json] "
            "[--benchmark SPY] [--compare QQQ,IWM,AGG] [output.json]"
        )

    holdings_file = positional[0]
    performance_file: Optional[str] = None
    output_file: Optional[str] = None

    # Remaining positionals: distinguish performance.json vs output.json by
    # filename heuristic. Only explicit 'performance' names are treated as
    # input; anything else (including peer_analysis.json or unknown names)
    # is treated as the output target.
    rest = positional[1:]
    for p in rest:
        name = os.path.basename(p).lower()
        if "performance" in name:
            performance_file = p
        else:
            output_file = p

    return holdings_file, performance_file, benchmark, compare, output_file


if __name__ == "__main__":
    # Artifact flags (consistent with other commands)
    from ic_engine.commands._artifact_helpers import pop_artifact_flags  # noqa: E402

    _argv = list(sys.argv)
    _artifact_path, _stonkmode = pop_artifact_flags(_argv)
    sys.argv = _argv

    holdings_file, performance_file, benchmark, compare, output_file = _parse_argv(sys.argv)

    # Default output to sibling of holdings_file if none provided
    if output_file is None:
        output_file = str(Path(holdings_file).expanduser().resolve().parent / "peer_analysis.json")

    result = run_peer_analysis(
        holdings_file=holdings_file,
        performance_file=performance_file,
        benchmark=benchmark,
        compare=compare,
        output_file=output_file,
    )

    compact = _build_compact_summary(result)

    # Print human-readable summary to stderr
    print(f"\n{'=' * 70}", file=sys.stderr)
    print("💡 Analysis complete. Review the detailed JSON output above.", file=sys.stderr)
    print("   → Bring these findings to your financial advisor.", file=sys.stderr)
    print(f"{'=' * 70}\n", file=sys.stderr)

    print(json.dumps(compact, separators=(",", ":")))

    if output_file:
        logger.info(f"Full peer analysis data saved to: {output_file}")
