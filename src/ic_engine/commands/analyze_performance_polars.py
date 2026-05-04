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
Analyze portfolio performance: returns, risk metrics, diversification, alerts, projections.
Supports YTD, 12-month rolling, and custom date ranges.
Uses Polars for data handling and NumPy/SciPy for statistical calculations.
"""
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 InvestorClaw Contributors

import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
import polars as pl
import yfinance as yf

from ic_engine.internal.metrics_wrapper import (
    calculate_beta as wrapper_beta,
)
from ic_engine.internal.metrics_wrapper import (
    calculate_drawdown as wrapper_drawdown,
)
from ic_engine.internal.metrics_wrapper import (
    calculate_sharpe_ratio as wrapper_sharpe,
)
from ic_engine.internal.metrics_wrapper import (
    calculate_sortino_ratio as wrapper_sortino,
)
from ic_engine.internal.metrics_wrapper import (
    calculate_var as wrapper_var,
)
from ic_engine.internal.metrics_wrapper import (
    calculate_volatility as wrapper_volatility,
)
from ic_engine.internal.performance_timer import get_timer
from ic_engine.rendering.disclaimer_wrapper import DisclaimerWrapper
from ic_engine.services.portfolio_utils import (
    fetch_benchmark_returns,
    fetch_esg_total_score,
    fetch_governance_risk,
)

# Phase 9: Mode and feature enforcement
try:
    from ic_engine.config.config_loader import get_deployment_mode
    from ic_engine.config.deployment_modes import DeploymentMode, Feature
    from ic_engine.config.feature_manager import FeatureManager, FeatureNotAvailableError
    from ic_engine.config.guardrail_enforcer import GuardrailEnforcer

    _features_available = True
except ImportError:
    _features_available = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _strip_interpretations(obj):
    """Remove verbose interpretation strings to reduce stdout token count.

    Strips keys whose value is a human-readable prose string (len >= 20)
    describing a numeric result. Short codes/tags (len < 20) are preserved.
    Applied to stdout JSON only — file writes keep the full data.
    """
    if isinstance(obj, dict):
        return {
            k: _strip_interpretations(v)
            for k, v in obj.items()
            if k not in ("interpretation", "label", "description")
            or not isinstance(v, str)
            or len(v) < 20
        }
    if isinstance(obj, list):
        return [_strip_interpretations(i) for i in obj]
    return obj


def _build_compact_summary(analysis_data: dict) -> dict:
    """Build a compact summary dict for stdout (2-3KB vs 352KB full data).

    Extracts only the most operationally relevant fields from analysis_data:
    portfolio-level metrics, top/bottom performers, high-risk and high-beta flags.
    """
    ps = analysis_data.get("portfolio_summary", {})
    performance = analysis_data.get("performance", {})

    # Build per-symbol rows with key metrics
    rows = []
    for sym, data in performance.items():
        sharpe_data = data.get("sharpe_ratio", {})
        vol_data = data.get("volatility", {})
        beta_data = data.get("beta", {})
        var_data = data.get("var", {})

        annual_return = sharpe_data.get("annual_return")
        sharpe = sharpe_data.get("sharpe_ratio")
        volatility = vol_data.get("annualized_volatility")
        beta = beta_data.get("beta")
        var_95 = var_data.get("var_95_annualized")  # already multiplied by 100

        rows.append(
            {
                "symbol": sym,
                "return_pct": round(annual_return * 100, 2) if annual_return is not None else None,
                "sharpe": round(sharpe, 3) if sharpe is not None else None,
                "volatility": round(volatility, 4) if volatility is not None else None,
                "beta": round(beta, 3) if beta is not None else None,
                "var_95": round(var_95, 2) if var_95 is not None else None,
            }
        )

    # Sort by return for top/bottom
    valid_rows = [r for r in rows if r["return_pct"] is not None]
    sorted_by_return = sorted(valid_rows, key=lambda x: x["return_pct"], reverse=True)

    top_5 = [
        {"symbol": r["symbol"], "return_pct": r["return_pct"], "sharpe": r["sharpe"]}
        for r in sorted_by_return[:5]
    ]
    bottom_5 = [
        {"symbol": r["symbol"], "return_pct": r["return_pct"]} for r in sorted_by_return[-5:]
    ]

    # High-risk: volatility > 0.40 or VaR_95 > 5%
    high_risk = [
        {"symbol": r["symbol"], "volatility": r["volatility"], "var_95": r["var_95"]}
        for r in rows
        if (r["volatility"] is not None and r["volatility"] > 0.40)
        or (r["var_95"] is not None and abs(r["var_95"]) > 5.0)
    ][:10]

    # High-beta: beta > 1.5
    high_beta = [
        {"symbol": r["symbol"], "beta": r["beta"]}
        for r in rows
        if r["beta"] is not None and r["beta"] > 1.5
    ][:10]

    compact = {
        "portfolio_summary": {
            "period": analysis_data.get("period"),
            "holdings_analyzed": analysis_data.get("holdings_analyzed"),
            "success_rate": analysis_data.get("success_rate"),
            "weighted_volatility": round(ps.get("weighted_volatility", 0), 4),
            "weighted_sharpe": round(ps.get("weighted_sharpe", 0), 4),
        },
        "top_performers": top_5,
        "bottom_performers": bottom_5,
        "high_risk": high_risk,
        "high_beta": high_beta,
    }
    return compact


def _consult_performance_summary(compact: dict, client) -> str:
    """Call consultation model to synthesize compact portfolio summary.

    Args:
        compact: Compact summary dict from _build_compact_summary()
        client: ConsultationClient instance

    Returns:
        Synthesis text string (empty string on failure)
    """
    try:
        prompt = (
            "Synthesize this portfolio performance summary in 2-3 sentences "
            "highlighting key risks and opportunities: "
            + json.dumps(compact, separators=(",", ":"))
        )
        result = client.consult(prompt)
        return result.response if hasattr(result, "response") else str(result)
    except Exception as e:
        logger.warning(f"Consultation synthesis failed: {e}")
        return ""


class PerformanceAnalyzer:
    def __init__(self):
        self.performance = {}
        self.risk_metrics = {}
        self.alerts = []

    @staticmethod
    def parse_date(date_str: str) -> str:
        """Convert date string to valid format, handle 'today', 'ytd', etc."""
        if not date_str or date_str.lower() == "today":
            return datetime.now().strftime("%Y-%m-%d")
        elif date_str.lower() == "ytd":
            year_start = datetime(datetime.now().year, 1, 1)
            return year_start.strftime("%Y-%m-%d")
        elif date_str.lower() == "12m":
            return (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        elif date_str.lower() == "3m":
            return (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        elif date_str.lower() == "1m":
            return (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        else:
            # Assume YYYY-MM-DD format
            return date_str

    def fetch_equity_data(
        self, symbols: list, start_date: str, end_date: str
    ) -> Tuple[pl.DataFrame, Dict, list]:
        """Fetch OHLC and dividend data for equities using Polars.

        NOTE: yfinance returns different column structures depending on symbol count:
        - Single symbol: columns are ['Open', 'High', 'Low', 'Close', 'Volume']
        - Multiple symbols: columns are MultiIndex like ('Open', 'AAPL'), ('Open', 'GOOGL'), etc.

        This method normalizes multi-symbol data into flat columns like 'Close_AAPL', 'Close_GOOGL'.
        """
        try:
            logger.info(f"Fetching data for {len(symbols)} symbols from {start_date} to {end_date}")
            timer = get_timer()

            # Fetch OHLCV via PriceProvider (massive → alpha_vantage → finnhub → yfinance).
            # Returns a DataFrame shaped exactly like yf.download(auto_adjust=True):
            # single-symbol → flat columns; multi-symbol → MultiIndex (metric, symbol).
            with timer.measure("price_panel_fetch"):
                from ic_engine.providers.price_panel import get_ohlcv_panel

                # Compute lookback in days from the requested date range so PriceProvider
                # returns at least the window the analyzer asked for. Pad by 7 days to
                # cover weekends/holidays at the leading edge.
                start_dt = pd.to_datetime(start_date)
                end_dt = pd.to_datetime(end_date)
                today = pd.Timestamp.now().normalize()
                lookback_days = max(int((today - start_dt).days) + 7, 30)

                data_pd = get_ohlcv_panel(symbols, days=lookback_days)

                # Filter to requested date window (PriceProvider returns rolling window
                # ending today; analyzer wants [start_date, end_date]).
                if not data_pd.empty:
                    data_pd = data_pd.loc[
                        (data_pd.index >= start_dt) & (data_pd.index <= end_dt)
                    ]

            # Normalize column structure for both single and multiple symbols
            if len(symbols) == 1:
                # Single symbol - yfinance returns columns like ['Open', 'High', 'Low', 'Close', 'Volume']
                data_pd.columns = [
                    col if isinstance(col, str) else col[0] for col in data_pd.columns.values
                ]
                data_pl = pl.from_pandas(data_pd.reset_index())
                logger.debug(f"Single symbol mode: columns={list(data_pl.columns)}")
            else:
                # Multiple symbols - yfinance returns MultiIndex columns like ('Open', 'AAPL'), ('Close', 'AAPL')
                # Flatten to 'Close_AAPL', 'Close_GOOGL' format
                if isinstance(data_pd.columns, pd.MultiIndex):
                    # MultiIndex: flatten to 'metric_symbol' format
                    new_columns = []
                    for col in data_pd.columns:
                        metric, symbol = col  # ('Close', 'AAPL') -> 'Close_AAPL'
                        new_columns.append(f"{metric}_{symbol}")
                    data_pd.columns = new_columns
                    logger.debug(
                        f"Multi-symbol mode (MultiIndex): converted {len(data_pd.columns)} columns"
                    )
                else:
                    # Single-level columns (shouldn't happen with multiple symbols, but handle it)
                    logger.warning("Multi-symbol download returned single-level columns (unusual)")

                data_pl = pl.from_pandas(data_pd.reset_index())
                logger.debug(
                    f"Multi-symbol final columns: {list(data_pl.columns)[:10]}..."
                )  # Log first 10

            if data_pl.is_empty():
                raise ValueError(
                    "No data returned from Yahoo Finance. Check symbols and date range."
                )

            # Fetch dividends for all symbols (parallelized)
            def _fetch_one_dividend(sym):
                try:
                    ticker = yf.Ticker(sym)
                    div_data = ticker.dividends
                    if not div_data.empty:
                        div_data = div_data[
                            (div_data.index >= start_date) & (div_data.index <= end_date)
                        ]
                        return sym, float(div_data.sum()) if len(div_data) > 0 else 0.0
                    else:
                        return sym, 0.0
                except Exception as e:
                    logger.warning(f"Could not fetch dividends for {sym}: {e}")
                    return sym, 0.0

            dividends = {}
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(_fetch_one_dividend, sym): sym for sym in symbols}
                for future in as_completed(futures):
                    sym, div_value = future.result()
                    dividends[sym] = div_value

            return data_pl, dividends, symbols

        except Exception as e:
            logger.error(f"Error fetching equity data: {e}")
            raise

    @staticmethod
    def _validate_array(arr: np.ndarray, symbol: str, operation: str) -> Tuple[bool, str]:
        """Validate that array is suitable for financial calculations.

        Returns: (is_valid, debug_message)
        """
        if arr is None:
            return False, f"{symbol} {operation}: array is None"
        if len(arr) == 0:
            return False, f"{symbol} {operation}: array is empty (len=0)"
        if len(arr) < 2:
            return False, f"{symbol} {operation}: array too short (len={len(arr)}, need >= 2)"
        nan_count = np.isnan(arr).sum()
        if nan_count == len(arr):
            return False, f"{symbol} {operation}: all values NaN ({nan_count}/{len(arr)})"
        if nan_count > 0:
            return False, f"{symbol} {operation}: partial NaN values ({nan_count}/{len(arr)})"
        return (
            True,
            f"{symbol} {operation}: valid (len={len(arr)}, min={np.min(arr):.6f}, max={np.max(arr):.6f}, mean={np.mean(arr):.6f})",
        )

    def calculate_returns(
        self, price_data: pl.DataFrame, symbol: str, annual_dividend: float = 0.0
    ) -> np.ndarray:
        """Calculate total daily returns from price data including dividend yield.

        FORMULA:
        --------
        Daily Return = (Price_t - Price_t-1) / Price_t-1 + Daily Dividend Yield
        where Daily Dividend Yield = (Annual Dividend / Average Price) / 252

        REFERENCES:
        -----------
        - Price returns: Standard daily return calculation (widely used in finance)
        - Dividend adjustment: Standard practice per MSCI, Bloomberg, FactSet methodologies
        - 252: Standard number of trading days per year (NYSE/NASDAQ)

        Args:
            price_data: DataFrame with price data (Date, Close prices)
            symbol: Stock symbol to extract price column
            annual_dividend: Annual dividend amount. If provided, adds daily dividend yield to returns.

        Returns:
            Array of daily returns including dividend contribution

        Example:
            returns = analyzer.calculate_returns(df, 'AAPL', annual_dividend=0.94)
            # Returns include both price appreciation and dividend income
        """
        try:
            # Extract close prices for the symbol
            # For multi-symbol downloads, columns are named like 'Close_AAPL', 'Close_GOOGL'
            # For single-symbol downloads, column is just 'Close'
            close_col = f"Close_{symbol}"
            if close_col not in price_data.columns:
                # Fallback: look for just 'Close' (single symbol mode)
                if "Close" in price_data.columns:
                    close_col = "Close"
                else:
                    # Last resort: find any Close column
                    close_cols = [col for col in price_data.columns if "Close" in col]
                    if not close_cols:
                        raise ValueError(
                            f"No Close price found for {symbol}. Available columns: {list(price_data.columns)}"
                        )
                    close_col = close_cols[0]

            prices = price_data.select(close_col).to_numpy().flatten()

            # Validate price data
            is_valid, msg = self._validate_array(prices, symbol, "prices")
            if not is_valid:
                logger.error(msg)
                raise ValueError(msg)

            # Calculate daily price returns as percentage change
            price_returns = np.diff(prices) / prices[:-1]

            # Validate returns
            is_valid, msg = self._validate_array(price_returns, symbol, "price_returns")
            if not is_valid:
                logger.error(msg)
                raise ValueError(msg)

            # Add dividend contribution if provided
            if annual_dividend > 0 and len(prices) > 0:
                # Daily dividend yield = annual_dividend / average_price / 252
                avg_price = np.mean(prices)
                daily_dividend_yield = (annual_dividend / avg_price) / 252
                total_returns = price_returns + daily_dividend_yield
                logger.debug(
                    f"{symbol}: Added daily dividend yield of {daily_dividend_yield * 100:.3f}% ({annual_dividend:.2f}/year at {avg_price:.2f} avg price)"
                )
            else:
                total_returns = price_returns

            is_valid, msg = self._validate_array(total_returns, symbol, "final_returns")
            logger.debug(msg)
            return total_returns

        except Exception as e:
            logger.error(f"Error calculating returns for {symbol}: {e}")
            raise

    def calculate_volatility(
        self, returns: np.ndarray, symbol: str = "UNKNOWN", window: int = 30
    ) -> Dict:
        """Calculate volatility metrics using empyrical wrapper.

        FORMULA:
        --------
        Daily Volatility = sqrt( sum((R_i - mean(R))^2) / (n-1) )
        Annualized Volatility = Daily Volatility × sqrt(252)

        Uses sample standard deviation (ddof=1, Bessel's correction) for statistical consistency.
        """
        try:
            is_valid, msg = self._validate_array(returns, symbol, "volatility_input")
            if not is_valid:
                logger.warning(msg)
                return {"_valid": False, "_error": msg, "annualized_volatility": None}

            if len(returns) < window:
                window = max(1, len(returns) - 1)

            wrapper_result = wrapper_volatility(returns, window=window)
            annual_vol = wrapper_result.get("annualized_volatility", 0.0)
            wrapper_result.get("rolling_volatility_30d", 0.0)
            daily_vol = np.std(returns, ddof=1)

            rolling_vols = []
            for i in range(len(returns) - window + 1):
                vol = np.std(returns[i : i + window], ddof=1) * np.sqrt(252)
                rolling_vols.append(vol)

            result = {
                "_valid": True,
                "daily_volatility": float(daily_vol),
                "annualized_volatility": float(annual_vol),
                "rolling_volatility_30d": float(np.mean(rolling_vols)) if rolling_vols else 0.0,
                "high_volatility": float(np.max(rolling_vols)) if rolling_vols else 0.0,
                "low_volatility": float(np.min(rolling_vols)) if rolling_vols else 0.0,
                "note": "Volatility calculated using empyrical (sample std deviation, ddof=1)",
            }
            logger.debug(f"{symbol}: volatility={annual_vol:.4f}")
            return result

        except Exception as e:
            logger.error(f"Error calculating volatility for {symbol}: {e}")
            return {"_valid": False, "_error": str(e), "annualized_volatility": None}

    def calculate_beta(
        self,
        returns: np.ndarray,
        symbol: str = "UNKNOWN",
        benchmark: str = "SPY",
        benchmark_returns: Optional[np.ndarray] = None,
    ) -> Dict:
        """Calculate beta (systematic risk) relative to benchmark using NumPy.

        FORMULA:
        --------
        Beta = Cov(Asset_Returns, Benchmark_Returns) / Var(Benchmark_Returns)

        where:
        - Cov = covariance between asset and benchmark (measures co-movement)
        - Var = variance of benchmark returns
        - Both use sample variance (ddof=1, Bessel's correction)

        INTERPRETATION:
        ----------------
        - Beta > 1.0: Asset is more volatile than market (amplifies market moves)
        - Beta = 1.0: Asset moves in line with market (neutral risk)
        - Beta < 1.0: Asset is less volatile than market (dampens market moves)
        - Beta ≤ 0: Asset moves opposite to market (rare, inverse correlation)

        REFERENCES:
        -----------
        - Source: Capital Asset Pricing Model (CAPM) - Sharpe (1964)
        - Standard practice: Bloomberg, FactSet, MSCI
        - Sample variance (ddof=1): Preferred for historical estimation per NIST guidelines
        - Benchmark: SPY = S&P 500 broad market index (default market proxy)

        Note: Beta is backward-looking (historical). Stability depends on time period selected.
        """
        try:
            is_valid, msg = self._validate_array(returns, symbol, "beta_input")
            if not is_valid:
                logger.warning(msg)
                return {"_valid": False, "_error": msg, "beta": None}

            # Fetch benchmark returns (cached to avoid redundant API calls)
            if benchmark_returns is None:
                bench_returns = fetch_benchmark_returns(benchmark, period="1y")
            else:
                bench_returns = benchmark_returns

            is_valid_bench, msg_bench = self._validate_array(
                bench_returns, benchmark, "benchmark_returns"
            )
            if not is_valid_bench:
                logger.warning(f"{symbol}: {msg_bench}")
                return {"_valid": False, "_error": msg_bench, "beta": None}

            min_len = min(len(returns), len(bench_returns))

            wrapper_result = wrapper_beta(returns[-min_len:], bench_returns[-min_len:])
            beta = wrapper_result.get("beta", 0.0)

            interpretation = (
                "Higher volatility than market"
                if beta > 1
                else "Lower volatility than market"
                if beta < 1
                else "Moves with market"
            )

            result = {
                "_valid": True,
                "beta": float(beta),
                "interpretation": interpretation,
                "note": f"Beta (empyrical) calculated vs {benchmark} using {min_len} periods",
            }
            logger.debug(f"{symbol}: beta={beta:.4f}")
            return result

        except Exception as e:
            logger.warning(f"Could not calculate beta for {symbol}: {e}")
            return {"_valid": False, "_error": str(e), "beta": None}

    def calculate_var(
        self, returns: np.ndarray, symbol: str = "UNKNOWN", confidence: float = 0.95
    ) -> Dict:
        """Calculate Value at Risk (VaR) and Conditional VaR (daily and annualized).

        FORMULA - VALUE AT RISK (VaR):
        --------------------------------
        VaR_95% = Percentile(returns, 5th)  [for 95% confidence level]

        In plain English: "There is a 95% probability that losses will NOT exceed X%"
        Or equivalently: "There is a 5% probability of losing more than X% in one day"

        FORMULA - CONDITIONAL VALUE AT RISK (CVaR, aka Expected Shortfall):
        -----------------------------------------------------------------
        CVaR_95% = Mean(returns where returns ≤ VaR_95%)

        In plain English: "If the worst 5% of days occur, the average loss would be X%"

        ANNUALIZATION:
        ---------------
        Annualized VaR = Daily VaR × √252
        where 252 = trading days per year

        REFERENCES:
        -----------
        - VaR methodology: Bank for International Settlements (BIS), Basel Accords
        - Historical simulation: Standard practice (Dowd, 2007)
        - CVaR (Expected Shortfall): Superior to VaR (tail-risk aware) - Rockafellar & Uryasev (2002)
        - 95% confidence: Standard for portfolio risk reporting (JP Morgan, BlackRock, etc.)

        Note: VaR assumes returns are i.i.d. and may underestimate tail risk in stressed markets.
        """
        try:
            is_valid, msg = self._validate_array(returns, symbol, "var_input")
            if not is_valid:
                logger.warning(msg)
                return {
                    "_valid": False,
                    "_error": msg,
                    "var_95_daily": None,
                    "var_95_annualized": None,
                }

            wrapper_result = wrapper_var(returns, confidence=confidence)
            daily_var = wrapper_result.get("var_95", 0.0) * 100
            annualized_var = wrapper_result.get("var_95_annualized", 0.0)

            daily_cvar = (
                np.mean(returns[returns <= daily_var / 100])
                if len(returns[returns <= daily_var / 100]) > 0
                else 0.0
            )
            annualized_cvar = daily_cvar * np.sqrt(252)

            result = {
                "_valid": True,
                "var_95_daily": float(daily_var),
                "var_95_annualized": float(annualized_var),
                "cvar_95_daily": float(daily_cvar) * 100,
                "cvar_95_annualized": float(annualized_cvar) * 100,
                "interpretation_daily": f"Daily (95% confidence): worst expected daily loss of {daily_var:.2f}%",
                "interpretation_annualized": f"Annual (95% confidence): worst expected annual loss of {annualized_var:.2f}%",
                "note": "VaR (empyrical): annualized = daily × √252. Use for risk planning.",
            }
            logger.debug(f"{symbol}: VaR_95_annualized={annualized_var:.2f}%")
            return result

        except Exception as e:
            logger.error(f"Error calculating VaR for {symbol}: {e}")
            return {
                "_valid": False,
                "_error": str(e),
                "var_95_daily": None,
                "var_95_annualized": None,
            }

    def _get_current_risk_free_rate(self) -> float:
        """Fetch current risk-free rate (3-month T-bill yield) from yfinance.

        Returns:
            Annual risk-free rate as decimal (0.045 = 4.5%)
        """
        try:
            # Fetch 3-month T-bill yield
            tbill = yf.Ticker("^IRX")
            # IRX is quoted as annual percentage (e.g., 4.50 for 4.5%)
            current_yield = tbill.info.get("regularMarketPrice", 2.0)
            # Convert from percentage to decimal
            risk_free_rate = max(0.0, current_yield / 100)
            logger.info(f"Fetched current T-bill yield: {current_yield:.2f}%")
            return risk_free_rate
        except Exception as e:
            logger.warning(f"Could not fetch current T-bill yield: {e}. Using 2.0% default.")
            return 0.02

    def calculate_sharpe_ratio(
        self, returns: np.ndarray, symbol: str = "UNKNOWN", risk_free_rate: float = None
    ) -> Dict:
        """Calculate Sharpe Ratio: risk-adjusted return relative to risk-free rate (uses empyrical wrapper).

        FORMULA:
        --------
        Sharpe Ratio = (Annual Return - Risk-Free Rate) / Annual Volatility

        In plain English: "How much excess return are you getting per unit of risk taken?"

        Where:
        - Annual Return = Daily Return Average × 252
        - Annual Volatility = Daily Volatility × √252
        - Risk-Free Rate = Current U.S. Treasury yield (3-month T-Bill by default)

        INTERPRETATION:
        ----------------
        - Sharpe > 1.0: Good risk-adjusted return
        - Sharpe > 2.0: Excellent risk-adjusted return (professional quality)
        - Sharpe > 3.0: Outstanding risk-adjusted return
        - Negative Sharpe: Portfolio underperformed risk-free rate

        Args:
            returns: Array of daily returns (decimal, e.g., 0.01 = 1%)
            symbol: Stock symbol for logging context
            risk_free_rate: Annual risk-free rate (e.g., 0.045 = 4.5%).
        """
        try:
            is_valid, msg = self._validate_array(returns, symbol, "sharpe_input")
            if not is_valid:
                logger.warning(msg)
                return {"_valid": False, "_error": msg, "sharpe_ratio": None}

            if risk_free_rate is None:
                risk_free_rate = self._get_current_risk_free_rate()

            wrapper_result = wrapper_sharpe(returns, risk_free_rate=risk_free_rate)
            sharpe = wrapper_result.get("sharpe_ratio", 0.0)
            annual_return = wrapper_result.get("annual_return", 0.0)
            annual_vol = wrapper_result.get("annual_volatility", 0.0)

            # Sortino: like Sharpe but penalizes only downside volatility.
            # Aggregator at portfolio_summary picks this up via _wsum.
            try:
                sortino_res = wrapper_sortino(returns, risk_free_rate=risk_free_rate)
                sortino = float(sortino_res.get("sortino_ratio", 0.0))
            except Exception as e:
                logger.debug(f"{symbol}: sortino fallback to 0 ({e})")
                sortino = 0.0

            if sharpe > 2.0:
                sharpe_quality = "Excellent (professional-quality risk-adjusted returns)"
            elif sharpe > 1.0:
                sharpe_quality = "Good (solid risk-adjusted returns)"
            elif sharpe > 0:
                sharpe_quality = "Modest (returns exceed risk-free rate but modest per risk)"
            else:
                sharpe_quality = "Poor (underperformed risk-free rate)"

            result = {
                "_valid": True,
                "sharpe_ratio": float(sharpe),
                "sortino_ratio": sortino,
                "annual_return": float(annual_return),
                "annual_volatility": float(annual_vol),
                "risk_free_rate_used": float(risk_free_rate),
                "sharpe_quality": sharpe_quality,
                "note": "Sharpe ratio (empyrical) = (annual_return - risk_free_rate) / annual_volatility. Measures return per unit of risk.",
                "_explanation": "Sharpe > 1.5 is excellent; >1.0 is good; >0 means you beat the risk-free rate. Higher is better.",
            }
            logger.debug(f"{symbol}: sharpe={sharpe:.4f}")
            return result

        except Exception as e:
            logger.error(f"Error calculating Sharpe Ratio for {symbol}: {e}")
            return {"_valid": False, "_error": str(e), "sharpe_ratio": None}

    def calculate_drawdown(self, prices: np.ndarray, symbol: str = "UNKNOWN") -> Dict:
        """Calculate maximum drawdown (peak-to-bottom loss) using NumPy.

        FORMULA:
        --------
        For each point in time:
            Drawdown_t = (Price_t - Running_Max) / Running_Max

        where:
        - Running_Max = highest price seen from beginning to time t
        - Price_t = current price at time t
        - Drawdown is negative (represents a loss from peak)

        Maximum Drawdown = minimum drawdown value across all time periods

        In plain English: "What is the worst peak-to-trough loss the portfolio experienced?"

        EXAMPLE:
        --------
        Portfolio value: $100 → $150 (peak) → $120 → $110
        From peak ($150) to trough ($110):
        Drawdown = ($110 - $150) / $150 = -26.7%

        REFERENCES:
        -----------
        - Standard practice: Morningstar, Zephyr StyleADVISOR, eSpeed
        - Risk metric: Widely used to assess downside experience
        - Related: Recovery time after drawdown is also important
        """
        try:
            is_valid, msg = self._validate_array(prices, symbol, "drawdown_prices")
            if not is_valid:
                logger.warning(msg)
                return {"_valid": False, "_error": msg, "max_drawdown": None}

            wrapper_result = wrapper_drawdown(prices)
            max_drawdown = wrapper_result.get("max_drawdown", 0.0) / 100

            result = {
                "_valid": True,
                "max_drawdown": float(max_drawdown) * 100,
                "interpretation": f"Worst peak-to-bottom loss: {max_drawdown * 100:.2f}%",
            }
            logger.debug(f"{symbol}: max_drawdown={max_drawdown * 100:.2f}%")
            return result

        except Exception as e:
            logger.error(f"Error calculating drawdown for {symbol}: {e}")
            return {"_valid": False, "_error": str(e), "max_drawdown": None}

    def analyze_portfolio(
        self, holdings_file: str, output_file: str = None, start_date: str = "12m"
    ) -> Dict:
        """Complete portfolio performance analysis."""
        try:
            # Phase 9: Check feature availability
            if _features_available:
                try:
                    mode_str = get_deployment_mode()
                    mode = DeploymentMode(mode_str)
                    fm = FeatureManager(mode)
                    fm.require_feature(Feature.PERFORMANCE_ANALYSIS)  # Core feature, all modes
                    logger.info(f"Performance analysis enabled for {mode_str} mode")
                except FeatureNotAvailableError as e:
                    logger.error(f"Performance analysis not available: {e}")
                    raise

            from ic_engine.config.schema import normalize_portfolio, validate_portfolio
            from ic_engine.services.portfolio_utils import load_holdings_list

            raw = json.load(open(holdings_file))
            raw = normalize_portfolio(raw)
            validate_portfolio(raw)
            holdings = load_holdings_list(raw)

            start_date = self.parse_date(start_date)
            end_date = (datetime.now() - timedelta(days=1)).strftime(
                "%Y-%m-%d"
            )  # Use yesterday to ensure complete yfinance data

            # Fetch data for equities
            equity_symbols = [h["symbol"] for h in holdings if h.get("asset_type") == "equity"]

            if not equity_symbols:
                logger.warning("No equity holdings found")
                return {"holdings": 0, "performance": {}}

            price_data, dividends, symbols = self.fetch_equity_data(
                equity_symbols, start_date, end_date
            )

            performance_summary = {}

            # Pre-fetch risk-free rate once before loop (avoid redundant network calls per symbol)
            risk_free_rate = self._get_current_risk_free_rate()

            # Pre-fetch benchmark returns once before loop (avoid redundant network calls per symbol)
            timer = get_timer()
            with timer.measure("fetch_benchmark_returns"):
                benchmark_returns = fetch_benchmark_returns("SPY", period="1y")

            valid_symbols = []
            symbol_returns: Dict[str, np.ndarray] = {}
            for symbol in symbols:
                logger.info(f"Analyzing {symbol}")

                try:
                    # Get annual dividend for this symbol
                    dividend = dividends.get(symbol, 0.0)
                    with timer.measure(f"analyze_{symbol}"):
                        returns = self.calculate_returns(
                            price_data, symbol, annual_dividend=dividend
                        )
                        symbol_returns[symbol] = returns

                        vol = self.calculate_volatility(returns, symbol=symbol)
                        beta = self.calculate_beta(
                            returns, symbol=symbol, benchmark_returns=benchmark_returns
                        )
                        sharpe = self.calculate_sharpe_ratio(
                            returns, symbol=symbol, risk_free_rate=risk_free_rate
                        )
                        var = self.calculate_var(returns, symbol=symbol)

                        # Per-symbol max_drawdown: needed by portfolio_summary
                        # aggregator (_wsum(['volatility','max_drawdown'])).
                        try:
                            close_col = f"Close_{symbol}"
                            if close_col not in price_data.columns and "Close" in price_data.columns:
                                close_col = "Close"
                            sym_prices = (
                                price_data.select(close_col).to_numpy().flatten()
                                if close_col in price_data.columns
                                else np.array([])
                            )
                            dd = self.calculate_drawdown(sym_prices, symbol=symbol)
                            if dd.get("_valid") and dd.get("max_drawdown") is not None:
                                vol["max_drawdown"] = float(dd["max_drawdown"])
                        except Exception as e:
                            logger.debug(f"{symbol}: drawdown skipped ({e})")

                    # Check if all calculations succeeded
                    if all(metric.get("_valid", True) for metric in [vol, beta, sharpe, var]):
                        performance_summary[symbol] = {
                            "volatility": vol,
                            "beta": beta,
                            "sharpe_ratio": sharpe,
                            "var": var,
                            "dividends": dividends.get(symbol, 0.0),
                        }
                        valid_symbols.append(symbol)
                        logger.info(f"✓ {symbol}: analysis complete")
                    else:
                        failed_metrics = [
                            m for m in [vol, beta, sharpe, var] if not m.get("_valid", True)
                        ]
                        logger.warning(
                            f"✗ {symbol}: {len(failed_metrics)}/4 metrics failed: {[m.get('_error', 'unknown') for m in failed_metrics]}"
                        )
                except Exception as e:
                    logger.error(f"✗ {symbol}: Fatal error - {e}")

            # Calculate value-weighted portfolio metrics (using only valid symbols)
            # Get closing prices for weighting
            position_weights = {}
            total_value = 0.0

            for symbol in valid_symbols:
                try:
                    close_col = f"Close_{symbol}"
                    if close_col not in price_data.columns:
                        # Fallback: look for just 'Close' (single symbol mode)
                        if "Close" in price_data.columns:
                            close_col = "Close"
                        else:
                            close_cols = [col for col in price_data.columns if "Close" in col]
                            if not close_cols:
                                raise ValueError(f"No Close column for {symbol}")
                            close_col = close_cols[0]

                    # Get latest price
                    latest_price_data = price_data.select(close_col).tail(1).to_numpy().flatten()
                    latest_price = (
                        float(latest_price_data[0]) if len(latest_price_data) > 0 else 1.0
                    )

                    # Ensure we have a valid price
                    if latest_price is None or latest_price != latest_price:  # NaN check
                        latest_price = 1.0

                    # Assume 1 share for weight calculation (we don't have actual share counts)
                    position_weights[symbol] = latest_price
                    total_value += latest_price
                except (ValueError, TypeError, KeyError, IndexError) as e:
                    logger.warning(f"Could not get price for {symbol}: {e}")
                    position_weights[symbol] = 1.0
                    total_value += 1.0

            # Normalize weights
            for symbol in position_weights:
                position_weights[symbol] = (
                    position_weights[symbol] / total_value
                    if total_value > 0
                    else 1.0 / len(valid_symbols)
                )

            # Calculate value-weighted metrics (using valid symbols only)
            def _wsum(metric_path: list[str], default: float = 0.0) -> float:
                """Value-weighted aggregate of a per-symbol metric.
                metric_path is the nested key list, e.g. ['sharpe_ratio', 'sharpe_ratio'].
                Skips symbols whose section is invalid or missing the value."""
                total = 0.0
                for sym in valid_symbols:
                    if sym not in performance_summary:
                        continue
                    node = performance_summary[sym]
                    section = node.get(metric_path[0], {})
                    if not section.get("_valid", True):
                        continue
                    val = section.get(metric_path[-1], default)
                    if val is None:
                        continue
                    total += position_weights.get(sym, 0) * float(val)
                return total

            weighted_volatility = _wsum(["volatility", "annualized_volatility"])
            weighted_sharpe     = _wsum(["sharpe_ratio", "sharpe_ratio"])

            # NEW portfolio-level metrics added 2026-05-03 to close v13-250
            # cobol gaps (n008/n020/n022/n029-n035 — Sortino, drawdown,
            # beta, dividend yield, leverage, value/growth, ESG, risk score).
            weighted_sortino = _wsum(["sharpe_ratio", "sortino_ratio"])
            weighted_max_drawdown = _wsum(["volatility", "max_drawdown"])
            weighted_beta = _wsum(["beta", "beta"])
            weighted_var_95 = _wsum(["var", "var_95_annualized"])
            # n006 "What's my total return?" — surface portfolio-level
            # annual return so the narrator doesn't deflect.
            weighted_annual_return = _wsum(["sharpe_ratio", "annual_return"])

            # Risk score — composite (0-100, lower = safer). Built from
            # vol + var + drawdown + beta. Heuristic, not a CAPM model.
            risk_components = []
            if weighted_volatility > 0:
                risk_components.append(min(100, weighted_volatility * 200))  # 50% vol = 100
            if weighted_var_95:
                risk_components.append(min(100, abs(weighted_var_95) * 10))  # 10% VaR = 100
            if weighted_max_drawdown:
                risk_components.append(min(100, abs(weighted_max_drawdown) * 200))  # 50% DD = 100
            if weighted_beta:
                risk_components.append(min(100, max(0, (weighted_beta - 0.3) * 50)))  # 1.0 beta = 35
            risk_score = sum(risk_components) / len(risk_components) if risk_components else None

            # Dividend yield — sum of (per-symbol dividend_yield × weight).
            # If yfinance.info or Finnhub provided dividendYield it's in
            # performance[sym]['dividend_yield'].
            weighted_dividend_yield = _wsum(["dividend_yield", "dividend_yield"])

            # Value vs growth ratio — heuristic from PE: PE<15=value, PE>25=growth.
            # If per-symbol pe_ratio is in performance section, derive a
            # weighted classifier; otherwise leave None for the narrator.
            value_weight = growth_weight = blend_weight = 0.0
            for sym in valid_symbols:
                if sym not in performance_summary:
                    continue
                pe_node = performance_summary[sym].get("fundamentals", {}) or {}
                pe = pe_node.get("pe_ratio")
                w = position_weights.get(sym, 0)
                if pe is None:
                    blend_weight += w
                elif pe < 15:
                    value_weight += w
                elif pe > 25:
                    growth_weight += w
                else:
                    blend_weight += w
            value_growth_ratio = (
                {"value_weight": round(value_weight, 4),
                 "growth_weight": round(growth_weight, 4),
                 "blend_weight": round(blend_weight, 4)}
                if (value_weight + growth_weight + blend_weight) > 0
                else None
            )

            # n029 correlation matrix — pairwise Pearson correlations
            # across the per-symbol return series. We cap to top-N by
            # weight so the JSON envelope stays small (215×215 = 46k
            # cells would blow past payload limits).
            correlation_matrix: Dict[str, Dict[str, float]] = {}
            avg_pairwise_correlation: Optional[float] = None
            try:
                top_n = min(15, len(valid_symbols))
                top_syms = sorted(valid_symbols, key=lambda s: -position_weights.get(s, 0))[:top_n]
                # Align all return series to the shortest length so
                # corrcoef has a well-formed matrix.
                series = []
                for s in top_syms:
                    r = symbol_returns.get(s)
                    if r is None or len(r) < 2:
                        continue
                    series.append((s, r))
                if len(series) >= 2:
                    min_len = min(len(r) for _, r in series)
                    R = np.vstack([r[-min_len:] for _, r in series])
                    C = np.corrcoef(R)
                    syms_used = [s for s, _ in series]
                    for i, s1 in enumerate(syms_used):
                        correlation_matrix[s1] = {}
                        for j, s2 in enumerate(syms_used):
                            v = C[i, j]
                            correlation_matrix[s1][s2] = (
                                round(float(v), 4) if not np.isnan(v) else None
                            )
                    # Off-diagonal mean (exclude self-correlations of 1.0).
                    n = C.shape[0]
                    if n >= 2:
                        mask = ~np.eye(n, dtype=bool)
                        offdiag = C[mask]
                        offdiag = offdiag[~np.isnan(offdiag)]
                        if len(offdiag) > 0:
                            avg_pairwise_correlation = round(float(np.mean(offdiag)), 4)
            except Exception as e:
                logger.warning(f"correlation_matrix failed: {e}")

            # n034 ESG — weighted Sustainalytics score across top-N holdings.
            # We only fetch top-25 by weight to limit yfinance.sustainability
            # calls (rate-limited; circuit breaker in fetch_esg_total_score).
            esg_acc = esg_w = 0.0
            esg_per_symbol: Dict[str, float] = {}
            esg_top_n = sorted(valid_symbols, key=lambda s: -position_weights.get(s, 0))[:25]
            for sym in esg_top_n:
                score = fetch_esg_total_score(sym)
                if score is None:
                    continue
                w = position_weights.get(sym, 0)
                esg_acc += w * score
                esg_w += w
                esg_per_symbol[sym] = round(score, 2)
            weighted_esg_score = (
                round(esg_acc / esg_w, 2) if esg_w > 0 else None
            )
            esg_note = (
                f"Sustainalytics ESG totalEsg score (lower = better, 0–40 typical). "
                f"Coverage: {len(esg_per_symbol)}/{len(esg_top_n)} top-weighted holdings via yfinance."
                if weighted_esg_score is not None
                else "ESG totalEsg unavailable — Yahoo Finance gated free Sustainalytics access in 2024. "
                     "Governance risk subset (auditRisk, boardRisk, etc.) is reported in governance_* fields. "
                     "For full Sustainalytics ESG composite, configure an MSCI / Sustainalytics provider."
            )

            # Governance risk — works even when ESG composite is gated.
            # Same top-25 universe, separate yfinance.Ticker.info endpoint.
            gov_overall_acc = gov_audit_acc = gov_board_acc = gov_comp_acc = gov_share_acc = 0.0
            gov_w = 0.0
            governance_per_symbol: Dict[str, Dict] = {}
            for sym in esg_top_n:
                g = fetch_governance_risk(sym)
                if g is None or g.get("overall_risk") is None:
                    continue
                w = position_weights.get(sym, 0)
                gov_overall_acc += w * g["overall_risk"]
                if g.get("audit_risk") is not None:
                    gov_audit_acc += w * g["audit_risk"]
                if g.get("board_risk") is not None:
                    gov_board_acc += w * g["board_risk"]
                if g.get("compensation_risk") is not None:
                    gov_comp_acc += w * g["compensation_risk"]
                if g.get("shareholder_rights_risk") is not None:
                    gov_share_acc += w * g["shareholder_rights_risk"]
                gov_w += w
                governance_per_symbol[sym] = g
            if gov_w > 0:
                weighted_governance_overall = round(gov_overall_acc / gov_w, 2)
                weighted_governance_audit = round(gov_audit_acc / gov_w, 2)
                weighted_governance_board = round(gov_board_acc / gov_w, 2)
                weighted_governance_compensation = round(gov_comp_acc / gov_w, 2)
                weighted_governance_shareholder_rights = round(gov_share_acc / gov_w, 2)
                governance_note = (
                    f"Yahoo Finance governance risk (1=best, 10=worst), value-weighted across "
                    f"{len(governance_per_symbol)}/{len(esg_top_n)} top holdings. Source: Institutional "
                    "Shareholder Services (ISS) governance pillars."
                )
            else:
                weighted_governance_overall = None
                weighted_governance_audit = None
                weighted_governance_board = None
                weighted_governance_compensation = None
                weighted_governance_shareholder_rights = None
                governance_note = (
                    "Governance risk unavailable — yfinance.info throttled or no coverage."
                )

            # Leverage ratio — weighted average debt/equity from per-symbol
            # fundamentals if available; else None.
            leverage_acc = leverage_w = 0.0
            for sym in valid_symbols:
                if sym not in performance_summary:
                    continue
                fund = performance_summary[sym].get("fundamentals", {}) or {}
                de = fund.get("debt_to_equity")
                if de is None:
                    continue
                w = position_weights.get(sym, 0)
                leverage_acc += w * float(de)
                leverage_w += w
            weighted_leverage = (leverage_acc / leverage_w) if leverage_w > 0 else None

            analysis_data = {
                "period": start_date,
                "holdings_analyzed": len(performance_summary),
                "holdings_valid": len(valid_symbols),
                "holdings_failed": len(symbols) - len(valid_symbols),
                "success_rate": f"{len(valid_symbols) / len(symbols) * 100:.1f}%"
                if len(symbols) > 0
                else "0%",
                "performance": performance_summary,
                "portfolio_summary": {
                    "weighted_volatility": float(weighted_volatility),
                    "weighted_sharpe": float(weighted_sharpe),
                    "weighted_sortino": float(weighted_sortino),
                    "weighted_max_drawdown": float(weighted_max_drawdown),
                    "weighted_beta_to_market": float(weighted_beta),
                    "weighted_var_95": float(weighted_var_95),
                    "weighted_annual_return": float(weighted_annual_return),
                    "weighted_dividend_yield": float(weighted_dividend_yield),
                    "weighted_leverage": weighted_leverage,
                    "weighted_esg_score": weighted_esg_score,
                    "esg_per_symbol": esg_per_symbol or None,
                    "esg_note": esg_note,
                    "weighted_governance_overall": weighted_governance_overall,
                    "weighted_governance_audit": weighted_governance_audit,
                    "weighted_governance_board": weighted_governance_board,
                    "weighted_governance_compensation": weighted_governance_compensation,
                    "weighted_governance_shareholder_rights": weighted_governance_shareholder_rights,
                    "governance_per_symbol": governance_per_symbol or None,
                    "governance_note": governance_note,
                    "correlation_matrix": correlation_matrix or None,
                    "avg_pairwise_correlation": avg_pairwise_correlation,
                    "correlation_matrix_note": (
                        f"Top {len(correlation_matrix)} positions by weight; "
                        "Pearson r in [-1, 1]; ~1.0 = move together, ~0 = independent, ~-1 = opposite."
                        if correlation_matrix else None
                    ),
                    "value_growth_classification": value_growth_ratio,
                    "risk_score": (
                        {"score": round(risk_score, 1),
                         "scale": "0-100, lower = safer",
                         "components": ["volatility", "VaR_95", "max_drawdown", "beta"]}
                        if risk_score is not None else None
                    ),
                    "note": "Metrics are value-weighted based on closing prices.",
                    "warning": f"Based on {len(valid_symbols)}/{len(symbols)} valid symbols. {len(symbols) - len(valid_symbols)} symbols failed analysis.",
                    "valid_symbols_only": True,
                },
            }

            # Phase 9: Apply guardrails based on deployment mode
            if _features_available:
                try:
                    mode_str = get_deployment_mode()
                    mode = DeploymentMode(mode_str)
                    enforcer = GuardrailEnforcer(mode)

                    # Apply appropriate disclaimer based on mode
                    performance_text = json.dumps(analysis_data, indent=2)
                    enforcer.add_professional_disclaimer(performance_text)
                    logger.info(f"Applied {mode_str} guardrails and disclaimers")
                except Exception as e:
                    logger.warning(f"Could not apply mode-specific guardrails: {e}")

            # Wrap with compliance disclaimers (compact=True omits static metadata ~60 tokens)
            _mode_str = (
                mode_str
                if "mode_str" in dir()
                else (get_deployment_mode() if _features_available else None)
            )
            report = DisclaimerWrapper.wrap_output(
                analysis_data,
                "Portfolio Performance Analysis",
                compact=True,
                deployment_mode=_mode_str,
            )

            # Strip verbose interpretation strings for stdout token reduction
            report = _strip_interpretations(report)

            if output_file:
                DisclaimerWrapper.wrap_and_save(
                    analysis_data,
                    output_file,
                    "Portfolio Performance Analysis",
                    deployment_mode=_mode_str,
                )
                logger.info(f"Performance analysis saved to {output_file}")

            return report

        except Exception as e:
            logger.error(f"Error analyzing portfolio: {e}")
            raise


if __name__ == "__main__":
    # Extract --artifact / --stonkmode before positional parsing
    _project_root = str(Path(__file__).resolve().parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
    from ic_engine.commands._artifact_helpers import pop_artifact_flags

    _argv = list(sys.argv)
    _artifact_path, _stonkmode = pop_artifact_flags(_argv)
    sys.argv = _argv

    if len(sys.argv) < 2:
        print(
            "Usage: python3 analyze_performance.py <holdings.json> [output.json] [start_date] [--artifact PATH] [--stonkmode]"
        )
        print("\nExample:")
        print(
            "  python3 analyze_performance.py ~/portfolio_reports/holdings.json ~/portfolio_reports/performance.json 12m"
        )
        sys.exit(1)

    holdings_file = sys.argv[1]
    start_date = sys.argv[2] if len(sys.argv) > 2 else "ytd"
    end_date = sys.argv[3] if len(sys.argv) > 3 else "today"
    output_file = sys.argv[4] if len(sys.argv) > 4 else None

    analyzer = PerformanceAnalyzer()

    # Run analysis — full data saved to output_file, compact summary to stdout
    from ic_engine.config.schema import normalize_portfolio
    from ic_engine.services.portfolio_utils import load_holdings_list

    raw = json.load(open(holdings_file))
    raw = normalize_portfolio(raw)
    holdings = load_holdings_list(raw)
    equity_symbols = [h["symbol"] for h in holdings if h.get("asset_type") == "equity"]

    report = analyzer.analyze_portfolio(holdings_file, output_file, start_date)

    # Extract raw analysis_data for compact summary (unwrap DisclaimerWrapper envelope)
    _data = report.get("data", report)

    # Build compact summary (~2-3KB) instead of printing full per-symbol JSON
    compact = _build_compact_summary(_data)

    # Add performance timing information
    timer = get_timer()
    timing_info = timer.emit()
    if timing_info:
        compact.update(timing_info)
        timer.log()

    # Part B: Consultation synthesis for large equity portfolios
    consultation_enabled = os.environ.get("INVESTORCLAW_CONSULTATION_ENABLED", "").lower() == "true"
    if consultation_enabled and len(equity_symbols) > 50:
        try:
            from tier3_enrichment import ConsultationClient

            client = ConsultationClient()
            if client.is_available():
                synthesis = _consult_performance_summary(compact, client)
                if synthesis:
                    compact["consultation_synthesis"] = synthesis
        except Exception as _e:
            logger.warning(f"Consultation import/call failed: {_e}")

    print(json.dumps(compact, separators=(",", ":")))

    if output_file:
        logger.info(f"Full performance data saved to: {output_file}")

    # Optional HTML artifact — prefer the full report (which has per-symbol detail);
    # fall back to compact if the full file isn't readable.
    if _artifact_path:
        try:
            from ic_engine.commands._artifact_helpers import build_performance_artifact

            _payload: dict = compact
            if output_file:
                try:
                    with open(output_file) as _pf:
                        _full = json.load(_pf)
                    _payload = _full
                except Exception:
                    _payload = compact
            _out = build_performance_artifact(_payload, _artifact_path, stonkmode=_stonkmode)
            print(f"Artifact: {_out}")
        except Exception as _e:
            logger.warning("Artifact generation failed: %s", _e)
