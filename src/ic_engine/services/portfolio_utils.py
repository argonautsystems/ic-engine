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
Shared utility functions for portfolio analyzer scripts.
Centralizes common operations: data loading, financial metrics, formatting.

Eliminates duplication across analyze_performance_polars.py,
fetch_historical_data.py, fetch_holdings.py, fetch_analyst_data.py, and others.
"""

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# ─── Runtime Import Path Setup (OpenClaw-safe, no PYTHONPATH env var) ────────
_skill_dir = Path(__file__).parent.parent  # Go from services/ → skill/
if str(_skill_dir) not in sys.path:
    sys.path.insert(0, str(_skill_dir))

# Import Holding for CDM-compatible interface
from ic_engine.internal.holdings_loader import HoldingsLoader
from ic_engine.models.holdings import Holding
from ic_engine.services.summary_utils import (  # noqa: F401  re-exported for back-compat
    SUMMARY_FIELD_ALIASES,
    extract_summary_block,
    normalize_summary_fields,
)

logger = logging.getLogger(__name__)


# ─── Data Loading ───────────────────────────────────────────────────────────
# These functions are retained as thin compatibility wrappers. The primary
# loader is now :class:`internal.holdings_loader.HoldingsLoader`; everything
# below is a shim so that callers in pipeline.py, peer_analysis.py, and
# analyze_performance_polars.py keep working during the consolidation.


def load_holdings_list(holdings_input) -> List[Dict]:
    """Load a flat list of holding dicts from a JSON file (or already-parsed dict).

    Equivalent to ``HoldingsLoader().load(holdings_input).to_dicts()``.
    """
    if isinstance(holdings_input, dict):
        portfolio = HoldingsLoader().load_from_dict(holdings_input)
    else:
        portfolio = HoldingsLoader().load(holdings_input)
    return portfolio.to_dicts()


def load_portfolio_json(holdings_file: str) -> Dict:
    """Load full portfolio JSON without transformation.

    Retained for callers that need direct access to the untouched envelope
    (e.g., disclaimer extraction). New code should prefer
    ``HoldingsLoader().load(...)`` and access ``PortfolioData.raw``.
    """
    path = Path(holdings_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Portfolio file not found: {path}")
    with open(path, "r") as f:
        return json.load(f)


def portfolio_to_holdings_list(
    portfolio_dict: Dict, asset_class_keys: Optional[List[str]] = None
) -> List[Dict]:
    """Convert a canonical portfolio dict into a flat holdings list.

    This helper is retained for legacy callers (fetch_portfolio_news.py,
    portfolio_analyzer.py used to use it). It now routes through
    HoldingsLoader by wrapping the provided dict in a synthetic envelope.
    """
    if asset_class_keys is None:
        asset_class_keys = {"equity", "bond", "cash", "margin", "crypto", "futures", "metals"}

    # Wrap the legacy-keyed portfolio dict so HoldingsLoader can parse it
    envelope = {"portfolio": portfolio_dict}
    portfolio = HoldingsLoader().load_from_dict(envelope)
    wanted = {str(a).lower() for a in asset_class_keys}
    return [
        h
        for h in portfolio.to_dicts()
        if (h.get("asset_class") or h.get("asset_type") or "").lower() in wanted
    ]


def normalize_to_holdings_list(holdings_json: Dict) -> List[Dict]:
    """Normalize any supported holdings schema (CDM, keyed, flat, wrapped)
    to a flat list of dicts.

    Thin wrapper over ``HoldingsLoader().load_from_dict(...).to_dicts()``.
    """
    portfolio = HoldingsLoader().load_from_dict(holdings_json)
    if not portfolio.positions:
        logger.warning(
            "normalize_to_holdings_list: no positions extracted (envelope keys=%s)",
            list(holdings_json.keys()) if isinstance(holdings_json, dict) else None,
        )
    return portfolio.to_dicts()


def holdings_to_holding_objects(holdings_list: List[Dict]) -> List[Holding]:
    """Convert a flat holdings list of dicts into a list of Holding objects."""
    return [Holding.from_dict(h) for h in holdings_list]


# ─── Date Parsing ───────────────────────────────────────────────────────────


def parse_date_shorthand(date_str: str) -> str:
    """
    Convert date shorthand to YYYY-MM-DD string.

    Handles: 'today', 'ytd', '12m', '3m', '1m', or any YYYY-MM-DD passthrough.

    Args:
        date_str: shorthand or explicit date string

    Returns:
        YYYY-MM-DD string
    """
    if not date_str or date_str.lower() == "today":
        return datetime.now().strftime("%Y-%m-%d")

    lower = date_str.lower()
    if lower == "ytd":
        return datetime(datetime.now().year, 1, 1).strftime("%Y-%m-%d")
    elif lower == "12m":
        return (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    elif lower == "3m":
        return (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    elif lower == "1m":
        return (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    return date_str


# ─── Financial Metrics ──────────────────────────────────────────────────────


def calculate_annualized_volatility(returns: np.ndarray, trading_days: int = 252) -> float:
    """
    Annualized volatility from daily returns array.

    Args:
        returns: 1-D numpy array of daily returns (e.g. [0.01, -0.005, ...])
        trading_days: number of trading days per year (default 252)

    Returns:
        Annualized volatility as a decimal (e.g. 0.18 for 18%)
    """
    if len(returns) < 2:
        return 0.0
    return float(np.std(returns) * np.sqrt(trading_days))


def calculate_sharpe_ratio(
    returns: np.ndarray, risk_free_rate: float = 0.02, trading_days: int = 252
) -> float:
    """
    Annualized Sharpe ratio from daily returns.

    Args:
        returns: 1-D numpy array of daily returns
        risk_free_rate: annual risk-free rate as decimal (default 0.02 for 2%)
        trading_days: number of trading days per year (default 252)

    Returns:
        Sharpe ratio (dimensionless). 0.0 if volatility is zero.
    """
    if len(returns) < 2:
        return 0.0
    annual_return = float(np.mean(returns) * trading_days)
    annual_vol = calculate_annualized_volatility(returns, trading_days)
    if annual_vol == 0:
        return 0.0
    return float((annual_return - risk_free_rate) / annual_vol)


def calculate_max_drawdown(prices: np.ndarray) -> float:
    """
    Maximum peak-to-trough drawdown from a price series.

    Args:
        prices: 1-D numpy array of prices (not returns)

    Returns:
        Maximum drawdown as a decimal (e.g. -0.35 for -35%).
        Returns 0.0 if insufficient data.
    """
    if len(prices) < 2:
        return 0.0
    daily_returns = np.diff(prices) / prices[:-1]
    cumulative = np.cumprod(1 + daily_returns)
    running_max = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - running_max) / running_max
    return float(np.min(drawdown))


def calculate_beta(asset_returns: np.ndarray, benchmark_returns: np.ndarray) -> float:
    """
    Beta of an asset relative to a benchmark.

    Aligns arrays to the same length (takes most recent N observations).

    Args:
        asset_returns: daily return array for the asset
        benchmark_returns: daily return array for the benchmark

    Returns:
        Beta coefficient. Returns 1.0 if data is insufficient or variance is zero.
    """
    if len(asset_returns) < 2 or len(benchmark_returns) < 2:
        return 1.0

    min_len = min(len(asset_returns), len(benchmark_returns))
    a = asset_returns[-min_len:]
    b = benchmark_returns[-min_len:]

    benchmark_variance = float(np.var(b))
    if benchmark_variance == 0:
        return 1.0

    covariance = float(np.cov(a, b)[0][1])
    return float(covariance / benchmark_variance)


def calculate_var(returns: np.ndarray, confidence: float = 0.95) -> Tuple[float, float]:
    """
    Historical Value at Risk (VaR) and Conditional VaR (CVaR/Expected Shortfall).

    Args:
        returns: daily return array
        confidence: confidence level (default 0.95 for 95%)

    Returns:
        (var, cvar) — both as daily return decimals (e.g. -0.025 for -2.5%)
        Returns (0.0, 0.0) if insufficient data (< 10 observations).
    """
    if len(returns) < 10:
        return 0.0, 0.0

    var = float(np.percentile(returns, (1 - confidence) * 100))
    tail = returns[returns <= var]
    cvar = float(np.mean(tail)) if len(tail) > 0 else var
    return var, cvar


def unrealized_gain_loss(
    shares: float, purchase_price: float, current_price: float
) -> Tuple[float, float]:
    """
    Calculate unrealized gain/loss in dollars and percentage.

    Args:
        shares: number of shares (or bond quantity)
        purchase_price: average cost basis per share
        current_price: current market price per share

    Returns:
        (unrealized_dollar, unrealized_pct)
        unrealized_pct is as a percentage value (e.g. 5.0 for +5%).
        Returns (0.0, 0.0) if purchase_price is zero or negative.
    """
    if purchase_price <= 0:
        return 0.0, 0.0
    cost_basis = shares * purchase_price
    unrealized = shares * (current_price - purchase_price)
    unrealized_pct = (unrealized / cost_basis) * 100
    return float(unrealized), float(unrealized_pct)


def calculate_herfindahl(weights: Dict[str, float]) -> float:
    """
    Herfindahl-Hirschman Index (diversification measure).

    Scaled to 0–10000:
      0     = perfectly diversified (infinite positions, equal weight)
      10000 = single holding (100% concentration)

    Args:
        weights: dict of {symbol: weight} where weights are decimals summing to ~1.0

    Returns:
        HHI score 0–10000
    """
    if not weights:
        return 0.0
    hhi = sum(w**2 for w in weights.values())
    return min(float(hhi * 10000), 10000.0)


def classify_diversification(herfindahl: float) -> str:
    """
    Classify diversification level from Herfindahl index.

    Returns: 'High', 'Medium', or 'Low'
    """
    if herfindahl < 1500:
        return "High"
    elif herfindahl < 5000:
        return "Medium"
    return "Low"


# ─── Benchmark Data Cache ────────────────────────────────────────────────────

_benchmark_cache: Dict[str, np.ndarray] = {}


def fetch_benchmark_returns(benchmark: str = "SPY", period: str = "1y") -> np.ndarray:
    """
    Fetch daily returns for a benchmark ticker, with process-level caching.

    Avoids redundant network calls when called multiple times for the same
    benchmark (e.g., once per symbol in a 260-stock portfolio).

    Args:
        benchmark: ticker symbol (default 'SPY')
        period: yfinance period string (default '1y')

    Returns:
        numpy array of daily returns. Returns empty array on failure.
    """
    cache_key = f"{benchmark}:{period}"
    if cache_key in _benchmark_cache:
        return _benchmark_cache[cache_key]

    try:
        import yfinance as yf

        data = yf.Ticker(benchmark).history(period=period)
        if data.empty:
            logger.warning(f"No data returned for benchmark {benchmark}")
            return np.array([])
        prices = data["Close"].values
        returns = np.diff(prices) / prices[:-1]
        _benchmark_cache[cache_key] = returns
        logger.debug(f"Cached {len(returns)} benchmark returns for {benchmark}")
        return returns
    except Exception as e:
        logger.warning(f"Could not fetch benchmark {benchmark}: {e}")
        return np.array([])


def clear_benchmark_cache() -> None:
    """Clear the benchmark returns cache (useful for testing)."""
    _benchmark_cache.clear()


# ─── Formatting ─────────────────────────────────────────────────────────────


def format_market_cap(market_cap: Optional[int]) -> Optional[str]:
    """
    Convert raw market cap integer to human-readable string.

    Args:
        market_cap: market capitalization in dollars (integer)

    Returns:
        Formatted string like "$2.5T", "$450B", "$1.2M", or None if input is None/0.
    """
    if not market_cap:
        return None
    if market_cap >= 1e12:
        return f"${market_cap / 1e12:.1f}T"
    elif market_cap >= 1e9:
        return f"${market_cap / 1e9:.1f}B"
    elif market_cap >= 1e6:
        return f"${market_cap / 1e6:.1f}M"
    return f"${market_cap:,}"


def safe_float(value, default: float = 0.0) -> float:
    """
    Safely convert a value to float, returning default on failure.

    Handles None, NaN, empty string, non-numeric strings.
    """
    if value is None:
        return default
    try:
        result = float(value)
        if result != result:  # NaN check
            return default
        return result
    except (TypeError, ValueError):
        return default
