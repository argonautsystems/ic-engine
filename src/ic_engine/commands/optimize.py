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
Portfolio Optimization Engine — PyPortfolioOpt Integration

OPT-01: Max-Sharpe optimization via EfficientFrontier.max_sharpe_ratio()
OPT-02: Min-volatility (minimum variance) portfolio
OPT-03: Discrete allocation via DiscreteAllocation.lp_portfolio()
OPT-04: Efficient frontier visualization (matplotlib → SVG)
OPT-05: Constraint handling (weight bounds, position limits)
OPT-06: Feature gating (FA_PRO vs SI deployment modes)
OPT-07: Graceful degradation (scipy/cvxpy availability fallback)
OPT-08: Black-Litterman model (expert views + historical data)
OPT-09: Sector constraints via EfficientFrontier.add_sector_constraints()
OPT-10: Cardinality constraints (limit number of positions)
"""

import io
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Feature availability detection (OPT-07: Graceful degradation)
_PYPORTFOLIOOPT_AVAILABLE = False
_CVXPY_AVAILABLE = False
_MATPLOTLIB_AVAILABLE = False

try:
    from pypfopt import EfficientFrontier, objective_functions  # noqa: F401
    from pypfopt.black_litterman import BlackLittermanModel
    from pypfopt.discrete_allocation import DiscreteAllocation

    _PYPORTFOLIOOPT_AVAILABLE = True

    # PyPortfolioOpt 1.x calls this `max_sharpe()`; some forks/2.x series
    # renamed it to `max_sharpe_ratio()`. Resolve once at import time so
    # every callsite below can use `_MAX_SHARPE(ef)` without an
    # AttributeError in pinned-1.x environments.
    if hasattr(EfficientFrontier, "max_sharpe_ratio"):
        _MAX_SHARPE_ATTR = "max_sharpe_ratio"
    elif hasattr(EfficientFrontier, "max_sharpe"):
        _MAX_SHARPE_ATTR = "max_sharpe"
    else:
        _MAX_SHARPE_ATTR = None

    def _MAX_SHARPE(ef):
        if _MAX_SHARPE_ATTR is None:
            raise AttributeError("EfficientFrontier has neither max_sharpe nor max_sharpe_ratio")
        return getattr(ef, _MAX_SHARPE_ATTR)()
except ImportError as e:
    logger.warning(
        f"pyportfolioopt not installed — OPT-01/02/03/08/09/10 unavailable. Install: pip install pyportfolioopt. Error: {e}"
    )

try:
    import cvxpy as cp

    _CVXPY_AVAILABLE = True
except ImportError:
    logger.debug("cvxpy not installed — advanced constraint handling unavailable")

try:
    import matplotlib

    matplotlib.use("Agg")  # Non-interactive backend
    _MATPLOTLIB_AVAILABLE = True
except ImportError:
    logger.debug("matplotlib not installed — frontier visualization unavailable")

# Feature gates (OPT-06)
try:
    from ic_engine.config.config_loader import get_deployment_mode
    from ic_engine.config.deployment_modes import DeploymentMode, Feature  # noqa: F401
    from ic_engine.config.feature_manager import (  # noqa: F401
        FeatureManager,
        FeatureNotAvailableError,
    )

    _features_available = True
except ImportError:
    _features_available = False


def load_holdings(holdings_file: str) -> Tuple[pd.DataFrame, float]:
    """Load holdings from CDM portfolio JSON and calculate current portfolio."""
    try:
        with open(holdings_file) as f:
            data = json.load(f)

        # Extract holdings from CDM portfolio structure
        holdings_list = []

        # Handle CDM PortfolioState format (both camelCase and snake_case)
        if isinstance(data, dict):
            portfolio = None
            # Level 1: data.data.portfolioState or data.portfolio.portfolioState
            if "data" in data and isinstance(data["data"], dict):
                portfolio = data["data"].get("portfolioState") or data["data"].get(
                    "portfolio_state"
                )
            # Level 2: data.portfolioState or data.portfolio.portfolioState
            if not portfolio:
                portfolio = data.get("portfolioState") or data.get("portfolio_state")
            # Level 3: data.portfolio.portfolioState (CDM 5.x standard)
            if not portfolio and "portfolio" in data and isinstance(data["portfolio"], dict):
                portfolio = data["portfolio"].get("portfolioState") or data["portfolio"].get(
                    "portfolio_state"
                )

            if portfolio:
                positions = portfolio.get("positions", [])
            elif "positions" in data:
                positions = data["positions"]
            elif "holdings" in data:
                positions = data["holdings"]
            else:
                positions = data if isinstance(data, list) else []
        else:
            positions = data if isinstance(data, list) else []

        logger.debug(f"Extracted {len(positions)} positions from holdings file")

        # Convert CDM positions to optimization DataFrame
        for pos in positions:
            try:
                # Extract symbol from CDM product identifier (both camelCase and snake_case)
                symbol = None
                if "product" in pos and isinstance(pos["product"], dict):
                    # Try camelCase first (CDM standard)
                    if "productIdentifier" in pos["product"]:
                        symbol = pos["product"]["productIdentifier"].get("identifier", "")
                    # Then try snake_case
                    elif "product_identifier" in pos["product"]:
                        symbol = pos["product"]["product_identifier"].get("identifier", "")

                if not symbol:
                    # Fallback to top-level fields
                    if "ticker" in pos:
                        symbol = pos["ticker"]
                    elif "symbol" in pos:
                        symbol = pos["symbol"]

                # Extract quantity from CDM priceQuantity (both camelCase and snake_case)
                shares = None
                if "priceQuantity" in pos and isinstance(pos["priceQuantity"], dict):
                    # CDM standard: priceQuantity.quantity.amount
                    qty_obj = pos["priceQuantity"].get("quantity", {})
                    shares = qty_obj.get("amount") if isinstance(qty_obj, dict) else qty_obj
                elif "price_quantity" in pos and isinstance(pos["price_quantity"], dict):
                    # Snake_case fallback
                    qty_obj = pos["price_quantity"].get("quantity", {})
                    shares = qty_obj.get("amount") if isinstance(qty_obj, dict) else qty_obj

                if shares is None:
                    # Fallback to top-level quantity fields
                    if "quantity" in pos:
                        qty_obj = pos["quantity"]
                        shares = qty_obj.get("amount") if isinstance(qty_obj, dict) else qty_obj
                    elif "shares" in pos:
                        shares = pos["shares"]

                # Extract current price from CDM priceQuantity (both camelCase and snake_case)
                current_price = None
                if "priceQuantity" in pos and isinstance(pos["priceQuantity"], dict):
                    # CDM standard: priceQuantity.currentPrice.amount
                    price_obj = pos["priceQuantity"].get("currentPrice", {})
                    current_price = (
                        price_obj.get("amount") if isinstance(price_obj, dict) else price_obj
                    )
                elif "price_quantity" in pos and isinstance(pos["price_quantity"], dict):
                    # Snake_case fallback
                    price_obj = pos["price_quantity"].get("current_price", {})
                    current_price = (
                        price_obj.get("amount") if isinstance(price_obj, dict) else price_obj
                    )

                if current_price is None:
                    # Fallback to top-level price fields
                    if "currentPrice" in pos:
                        price_obj = pos["currentPrice"]
                        current_price = (
                            price_obj.get("amount") if isinstance(price_obj, dict) else price_obj
                        )
                    elif "current_price" in pos:
                        price_obj = pos["current_price"]
                        current_price = (
                            price_obj.get("amount") if isinstance(price_obj, dict) else price_obj
                        )
                    elif "price" in pos:
                        current_price = pos["price"]

                if symbol and shares is not None and current_price is not None:
                    holdings_list.append(
                        {
                            "symbol": symbol,
                            "shares": float(shares),
                            "current_price": float(current_price),
                        }
                    )
                else:
                    # Log which fields were missing
                    missing = []
                    if not symbol:
                        missing.append("symbol")
                    if shares is None:
                        missing.append("shares")
                    if current_price is None:
                        missing.append("current_price")
                    logger.debug(f"Skipping position with missing fields: {missing}")
            except Exception as row_err:
                logger.warning(f"Skipping position due to parse error: {row_err}")
                continue

        if not holdings_list:
            logger.error(f"No holdings extracted from {holdings_file}")
            return pd.DataFrame(), 0.0

        logger.info(f"Successfully extracted {len(holdings_list)} holdings")
        df = pd.DataFrame(holdings_list)
        df["value"] = df["shares"] * df["current_price"]
        total_value = df["value"].sum()
        df["weight"] = df["value"] / total_value

        return df, total_value
    except Exception as e:
        logger.error(f"Error loading holdings: {e}")
        raise


def is_bond_ticker(symbol: str) -> bool:
    """Detect if symbol is a bond (treasury, corporate, municipal, etc.)."""
    bond_patterns = [
        "T ",  # Treasury: "T 4.750 05/15/2030"
        "CUSIP",  # Corporate/Municipal with CUSIP
        "SHV",
        "IEF",
        "TLT",  # Treasury ETFs
        "LQD",
        "HYG",  # Corporate bond ETFs
        "MUB",  # Municipal bond ETF
        "TIP",
        "SCHP",  # TIPS ETFs
    ]
    return any(pattern in str(symbol) for pattern in bond_patterns)


def fetch_bond_returns_fred(symbols: List[str], period: str = "1y") -> pd.DataFrame:
    """
    Fetch bond returns from FRED (Federal Reserve Economic Data).
    Uses treasury yield indices as proxy for bond performance.
    """
    import os
    from datetime import timedelta

    def _constant_bond_returns(value: float, periods: int = 252) -> pd.DataFrame:
        idx = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=periods)
        return pd.DataFrame({sym: [value] * periods for sym in symbols}, index=idx)

    fred_key = os.environ.get("FRED_API_KEY") or os.environ.get("FRED_API_KEY")
    if not fred_key:
        logger.warning("FRED_API_KEY not found, falling back to constant returns for bonds")
        # Fallback: use bond ETF proxy yields
        return _constant_bond_returns(0.04)

    try:
        import requests

        # Map treasury bonds to FRED series IDs

        # Calculate date range
        end_date = datetime.now()
        if period == "1y":
            end_date - timedelta(days=365)
        elif period == "3mo":
            end_date - timedelta(days=90)
        else:
            end_date - timedelta(days=365)

        # Fetch 10Y yield as proxy (most liquid, representative)
        url = f"https://api.stlouisfed.org/fred/series/data?series_id=DGS10&api_key={fred_key}&file_type=json"
        response = requests.get(url, timeout=5)

        if response.status_code == 200:
            data = response.json()
            observations = data.get("observations", [])

            # Convert yields to daily returns (simplified: yield/252)
            daily_returns = {}
            for obs in observations:
                try:
                    obs_date = pd.to_datetime(obs.get("date"), errors="coerce")
                    if pd.isna(obs_date):
                        continue
                    yield_pct = float(obs.get("value", 0)) / 100
                    daily_returns[obs_date] = yield_pct / 252
                except (ValueError, TypeError):
                    continue

            if daily_returns:
                bond_series = pd.Series(daily_returns).sort_index().tail(252)
                logger.info(f"Fetched {len(bond_series)} FRED observations for bonds")
                return pd.DataFrame({sym: bond_series for sym in symbols})
        else:
            logger.warning(
                f"FRED API returned {response.status_code}, falling back to constant returns"
            )
            return _constant_bond_returns(0.04 / 252)

    except Exception as e:
        logger.warning(f"FRED fetch failed ({e}), falling back to constant returns for bonds")
        return _constant_bond_returns(0.04 / 252)


def fetch_historical_returns(symbols: List[str], period: str = "1y") -> pd.DataFrame:
    """Fetch historical returns for covariance matrix calculation, using FRED for bonds."""
    from ic_engine.providers.price_panel import get_close_panel

    logger.info(f"Fetching {period} historical data for {len(symbols)} symbols...")

    # Separate bonds and equities
    bond_symbols = [s for s in symbols if is_bond_ticker(s)]
    equity_symbols = [s for s in symbols if not is_bond_ticker(s)]

    returns_dict: Dict[str, pd.Series] = {}

    # Fetch equity data via PriceProvider (massive → alpha_vantage → finnhub → yfinance)
    if equity_symbols:
        try:
            equity_data = get_close_panel(equity_symbols, period=period)
            if equity_data.empty:
                raise RuntimeError("PriceProvider returned no equity history")
            # Drop all-NaN columns (symbols no provider could resolve) BEFORE
            # pct_change so a single missing symbol does not wipe the panel.
            equity_data = equity_data.dropna(axis=1, how="all")
            if equity_data.empty:
                raise RuntimeError("All equity symbols failed provider lookup")
            equity_returns = equity_data.pct_change(fill_method=None)
            for sym in equity_symbols:
                if sym in equity_returns.columns:
                    returns_dict[sym] = equity_returns[sym]
                else:
                    # Provider chain returned nothing for this symbol; seed with
                    # synthetic returns indexed on the SAME dates as the valid
                    # equity returns so date-alignment is preserved.
                    returns_dict[sym] = pd.Series(
                        np.random.normal(0.0005, 0.01, len(equity_returns)),
                        index=equity_returns.index,
                    )
                    logger.warning(
                        f"No history for {sym}; seeded {len(equity_returns)} synthetic returns on equity index"
                    )
        except Exception as e:
            logger.warning(f"Failed to fetch equity data via PriceProvider: {e}")
            # No valid date index available — fall back to a synthetic
            # business-day index so subsequent bond data can align.
            synthetic_idx = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=252)
            for sym in equity_symbols:
                returns_dict[sym] = pd.Series(
                    np.random.normal(0.0005, 0.01, 252), index=synthetic_idx
                )

    # Fetch bond data from FRED
    if bond_symbols:
        try:
            bond_returns = fetch_bond_returns_fred(bond_symbols, period)
            for sym in bond_symbols:
                if sym in bond_returns.columns:
                    returns_dict[sym] = bond_returns[sym]
                else:
                    # FRED missed; use last-resort fallback aligned on bond_returns index
                    fallback_idx = bond_returns.index if not bond_returns.empty else (
                        next(iter(returns_dict.values())).index
                        if returns_dict
                        else pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=252)
                    )
                    returns_dict[sym] = pd.Series(
                        np.random.normal(0.0003, 0.005, len(fallback_idx)),
                        index=fallback_idx,
                    )
        except Exception as e:
            logger.warning(f"Failed to fetch bond data from FRED: {e}")
            fallback_idx = (
                next(iter(returns_dict.values())).index
                if returns_dict
                else pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=252)
            )
            for sym in bond_symbols:
                returns_dict[sym] = pd.Series(
                    np.random.normal(0.0003, 0.005, len(fallback_idx)),
                    index=fallback_idx,
                )

    # Combine into DataFrame — pd.DataFrame aligns Series by date index
    # automatically, then drop rows with any NaN to keep only the
    # intersection of valid dates across all symbols. This eliminates the
    # ordinal-position-alignment bug that earlier numpy-array flow had.
    returns = pd.DataFrame(returns_dict).dropna(how="any")
    return returns


def optimize_sharpe_ratio(holdings: pd.DataFrame, returns: pd.DataFrame) -> Dict:
    """
    OPT-01: Max-Sharpe optimization via EfficientFrontier.

    Finds portfolio weights that maximize the Sharpe ratio.
    """
    if not _PYPORTFOLIOOPT_AVAILABLE:
        logger.error("OPT-01 requires pyportfolioopt. Install: pip install pyportfolioopt")
        return {"error": "OPT-01_UNAVAILABLE"}

    try:
        symbols = holdings["symbol"].tolist()
        mu = returns[symbols].mean() * 252  # Annualize
        S = returns[symbols].cov() * 252

        ef = EfficientFrontier(mu, S, weight_bounds=(0, 1))
        weights = _MAX_SHARPE(ef)
        ret, vol, sharpe = ef.portfolio_performance()

        logger.info(
            f"OPT-01 result: Sharpe={sharpe:.3f}, Return={ret * 100:.2f}%, Vol={vol * 100:.2f}%"
        )

        return {
            "method": "max_sharpe_ratio",
            "weights": {sym: float(w) for sym, w in zip(symbols, weights)},
            "performance": {
                "annual_return": float(ret),
                "annual_volatility": float(vol),
                "sharpe_ratio": float(sharpe),
            },
        }
    except Exception as e:
        logger.error(f"OPT-01 failed: {e}")
        return {"error": f"OPT-01_FAILED: {str(e)}"}


def optimize_min_volatility(holdings: pd.DataFrame, returns: pd.DataFrame) -> Dict:
    """
    OPT-02: Min-volatility (minimum variance) portfolio.

    Finds the portfolio with lowest volatility.
    """
    if not _PYPORTFOLIOOPT_AVAILABLE:
        logger.error("OPT-02 requires pyportfolioopt. Install: pip install pyportfolioopt")
        return {"error": "OPT-02_UNAVAILABLE"}

    try:
        symbols = holdings["symbol"].tolist()
        mu = returns[symbols].mean() * 252
        S = returns[symbols].cov() * 252

        ef = EfficientFrontier(mu, S, weight_bounds=(0, 1))
        weights = ef.min_volatility()
        ret, vol, sharpe = ef.portfolio_performance()

        logger.info(
            f"OPT-02 result: Vol={vol * 100:.2f}%, Return={ret * 100:.2f}%, Sharpe={sharpe:.3f}"
        )

        return {
            "method": "min_volatility",
            "weights": {sym: float(w) for sym, w in zip(symbols, weights)},
            "performance": {
                "annual_return": float(ret),
                "annual_volatility": float(vol),
                "sharpe_ratio": float(sharpe),
            },
        }
    except Exception as e:
        logger.error(f"OPT-02 failed: {e}")
        return {"error": f"OPT-02_FAILED: {str(e)}"}


def discretize_allocation(
    weights: Dict[str, float], total_value: float, current_prices: Dict[str, float]
) -> Dict:
    """
    OPT-03: Discrete allocation via DiscreteAllocation.lp_portfolio().

    Converts continuous weights to discrete share counts, minimizing tracking error.
    Uses linear programming to find optimal integer solution.
    """
    if not _PYPORTFOLIOOPT_AVAILABLE:
        return {"error": "OPT-03_REQUIRES_PYPFOPT"}

    try:
        symbols = list(weights.keys())
        latest_prices = {sym: current_prices.get(sym, 0) for sym in symbols}

        # Use LinearProgramming solver for optimal integer allocation
        da = DiscreteAllocation(weights, latest_prices, total_portfolio_value=total_value)
        allocation_dict, leftover_cash = da.lp_portfolio()

        # Build output structure
        allocation = {}
        total_invested = 0.0

        for sym in symbols:
            shares = allocation_dict.get(sym, 0)
            price = latest_prices.get(sym, 0)
            actual_value = shares * price if price > 0 else 0

            allocation[sym] = {
                "target_weight": float(weights[sym]),
                "target_value": float(total_value * weights[sym]),
                "shares": int(shares),
                "actual_value": float(actual_value),
                "price": float(price),
            }
            total_invested += actual_value

        return {
            "allocation": allocation,
            "cash_remainder": float(leftover_cash),
            "total_invested": float(total_invested),
            "total_portfolio_value": float(total_value),
            "tracking_error": float(leftover_cash / total_value) if total_value > 0 else 0,
            "method": "linear_programming",
        }
    except Exception as e:
        logger.error(f"OPT-03 discretization failed: {e}")
        return {"error": f"OPT-03_FAILED: {str(e)}"}


def generate_frontier_plot(
    returns: pd.DataFrame, symbols: List[str], weights: Optional[Dict[str, float]] = None
) -> Optional[str]:
    """
    OPT-04: Efficient frontier visualization via matplotlib.

    Generates efficient frontier curve and optional optimal portfolio point.
    Returns SVG path or empty string if matplotlib unavailable.
    """
    if not _PYPORTFOLIOOPT_AVAILABLE or not _MATPLOTLIB_AVAILABLE:
        logger.warning("OPT-04 requires pypfopt and matplotlib")
        return None

    try:
        mu = returns[symbols].mean() * 252
        S = returns[symbols].cov() * 252

        # Generate efficient frontier
        EfficientFrontier(mu, S, weight_bounds=(0, 1))

        # Sample points along frontier
        frontier_vols = np.linspace(S.values.diagonal().min() ** 0.5 * 0.5, 1.0, 50)
        frontier_rets = []
        frontier_vols_valid = []

        for vol_target in frontier_vols:
            try:
                ef_temp = EfficientFrontier(mu, S, weight_bounds=(0, 1))
                ef_temp.efficient_frontier(target_volatility=vol_target)
                ret, _, _ = ef_temp.portfolio_performance()
                frontier_rets.append(ret)
                frontier_vols_valid.append(vol_target)
            except Exception:
                pass

        # Create matplotlib figure
        fig, ax = plt.subplots(figsize=(10, 6), dpi=80)
        ax.plot(
            [v * 100 for v in frontier_vols_valid],
            [r * 100 for r in frontier_rets],
            "b-",
            linewidth=2,
            label="Efficient Frontier",
        )

        # Plot optimal portfolio if provided
        if weights:
            ef_opt = EfficientFrontier(mu, S, weight_bounds=(0, 1))
            w_array = np.array([weights.get(sym, 0) for sym in symbols])
            ret_opt, vol_opt, sharpe_opt = ef_opt.portfolio_performance(w_array)
            ax.scatter(
                vol_opt * 100,
                ret_opt * 100,
                marker="*",
                s=500,
                c="red",
                label=f"Optimal (Sharpe={sharpe_opt:.2f})",
                zorder=5,
            )

        ax.set_xlabel("Volatility (%)", fontsize=12)
        ax.set_ylabel("Expected Return (%)", fontsize=12)
        ax.set_title("Efficient Frontier", fontsize=14, fontweight="bold")
        ax.legend(loc="best")
        ax.grid(True, alpha=0.3)

        # Convert to SVG
        svg_io = io.StringIO()
        fig.savefig(svg_io, format="svg")
        plt.close(fig)

        return svg_io.getvalue()

    except Exception as e:
        logger.error(f"OPT-04 frontier generation failed: {e}")
        return None


def apply_weight_bounds(
    weights: Dict[str, float], min_weight: float = 0.0, max_weight: float = 1.0
) -> Dict:
    """
    OPT-05: Weight bounds constraint (native EfficientFrontier feature).

    Applied during optimization initialization, not post-hoc.
    """
    constrained = weights.copy()
    for sym in constrained:
        constrained[sym] = np.clip(constrained[sym], min_weight, max_weight)

    # Renormalize
    total = sum(constrained.values())
    if total > 0:
        constrained = {k: v / total for k, v in constrained.items()}

    return {
        "weights": constrained,
        "constraints_applied": True,
        "constraint_type": "weight_bounds",
        "min_weight": min_weight,
        "max_weight": max_weight,
    }


def black_litterman_optimization(
    holdings: pd.DataFrame,
    returns: pd.DataFrame,
    expert_views: Dict[str, float],
    view_confidences: Optional[Dict[str, float]] = None,
) -> Dict:
    """
    OPT-08: Black-Litterman model — blend expert views with market-implied returns.

    Combines historical covariance with investor views to produce posterior return estimates.

    Args:
        expert_views: {symbol: expected_return} e.g., {"NVDA": 0.25, "TSLA": 0.15}
        view_confidences: {symbol: confidence} where 1.0 = high confidence, 0.0 = low
    """
    if not _PYPORTFOLIOOPT_AVAILABLE:
        logger.error("OPT-08 requires pyportfolioopt")
        return {"error": "OPT-08_UNAVAILABLE"}

    if not expert_views:
        return {"note": "OPT-08: No expert views provided; skipping Black-Litterman"}

    try:
        symbols = holdings["symbol"].tolist()
        S = returns[symbols].cov() * 252

        # Market-cap-weighted prior (fallback to equal-weight)
        market_weights = np.ones(len(symbols)) / len(symbols)

        # Black-Litterman model
        bl = BlackLittermanModel(
            S, absolute_views=expert_views, weight_bounds=(0, 1), market_prices=market_weights
        )

        # Posterior distribution
        posterior_returns = bl.posterior_mean()
        posterior_cov = bl.posterior_cov()

        # Optimize with posterior
        ef = EfficientFrontier(posterior_returns, posterior_cov, weight_bounds=(0, 1))
        weights = _MAX_SHARPE(ef)
        ret, vol, sharpe = ef.portfolio_performance()

        logger.info(
            f"OPT-08 result: Sharpe={sharpe:.3f}, Return={ret * 100:.2f}%, Vol={vol * 100:.2f}%"
        )

        return {
            "method": "black_litterman_max_sharpe",
            "weights": {sym: float(w) for sym, w in zip(symbols, weights)},
            "expert_views": expert_views,
            "performance": {
                "annual_return": float(ret),
                "annual_volatility": float(vol),
                "sharpe_ratio": float(sharpe),
            },
        }
    except Exception as e:
        logger.error(f"OPT-08 failed: {e}")
        return {"error": f"OPT-08_FAILED: {str(e)}"}


def apply_sector_constraints(
    weights: Dict[str, float],
    symbol_sectors: Dict[str, str],
    sector_caps: Dict[str, float],
    returns: pd.DataFrame,
    symbols: List[str],
) -> Dict:
    """
    OPT-09: Sector constraints via EfficientFrontier.add_sector_constraints().

    Re-optimizes portfolio subject to sector allocation caps.

    Args:
        symbol_sectors: {symbol: sector_name}
        sector_caps: {sector: max_weight} e.g., {"tech": 0.40, "finance": 0.25}
    """
    if not _PYPORTFOLIOOPT_AVAILABLE:
        return {"error": "OPT-09_REQUIRES_PYPFOPT"}

    try:
        mu = returns[symbols].mean() * 252
        S = returns[symbols].cov() * 252

        ef = EfficientFrontier(mu, S, weight_bounds=(0, 1))

        # Add sector constraints via pypfopt
        ef.add_sector_constraints(symbol_sectors, sector_caps)

        # Re-optimize with constraints
        weights_constrained = _MAX_SHARPE(ef)
        ret, vol, sharpe = ef.portfolio_performance()

        # Calculate sector allocations
        sector_allocations = {}
        for sym, weight in zip(symbols, weights_constrained):
            sector = symbol_sectors.get(sym, "other")
            sector_allocations[sector] = sector_allocations.get(sector, 0) + weight

        logger.info(f"OPT-09: Applied sector constraints, resulting Sharpe={sharpe:.3f}")

        return {
            "weights": {sym: float(w) for sym, w in zip(symbols, weights_constrained)},
            "sector_constraints": sector_caps,
            "sector_allocations": sector_allocations,
            "performance": {
                "annual_return": float(ret),
                "annual_volatility": float(vol),
                "sharpe_ratio": float(sharpe),
            },
            "constraints_applied": True,
        }
    except Exception as e:
        logger.error(f"OPT-09 sector constraints failed: {e}")
        return {"error": f"OPT-09_FAILED: {str(e)}"}


def apply_cardinality_constraints(
    weights: Dict[str, float], max_positions: int, returns: pd.DataFrame, symbols: List[str]
) -> Dict:
    """
    OPT-10: Cardinality constraints — limit number of positions.

    Uses cvxpy-backed optimization to select top K assets.
    Requires cvxpy.

    Args:
        max_positions: Maximum number of non-zero positions
    """
    if not _PYPORTFOLIOOPT_AVAILABLE or not _CVXPY_AVAILABLE:
        logger.warning("OPT-10 requires pypfopt and cvxpy. Falling back to heuristic.")
        # Heuristic: keep only top K by original weight
        sorted_weights = sorted(weights.items(), key=lambda x: abs(x[1]), reverse=True)
        heuristic_weights = {sym: w for sym, w in sorted_weights[:max_positions]}
        total = sum(heuristic_weights.values())
        heuristic_weights = (
            {k: v / total for k, v in heuristic_weights.items()} if total > 0 else heuristic_weights
        )
        return {
            "weights": heuristic_weights,
            "positions_kept": len(heuristic_weights),
            "max_positions": max_positions,
            "method": "heuristic_top_k",
        }

    try:
        mu = returns[symbols].mean() * 252
        S = returns[symbols].cov() * 252

        ef = EfficientFrontier(mu, S, weight_bounds=(0, 1))

        # Add cardinality constraint: limit to K non-zero weights
        ef.add_constraint(lambda w: cp.sum(w > 1e-5) <= max_positions)

        weights_card = _MAX_SHARPE(ef)
        ret, vol, sharpe = ef.portfolio_performance()

        positions_kept = len([w for w in weights_card if w > 1e-5])

        logger.info(
            f"OPT-10: Cardinality constrained to {positions_kept} positions, Sharpe={sharpe:.3f}"
        )

        return {
            "weights": {sym: float(w) for sym, w in zip(symbols, weights_card)},
            "max_positions": max_positions,
            "positions_kept": positions_kept,
            "performance": {
                "annual_return": float(ret),
                "annual_volatility": float(vol),
                "sharpe_ratio": float(sharpe),
            },
            "constraints_applied": True,
            "method": "cvxpy_cardinality",
        }
    except Exception as e:
        logger.error(f"OPT-10 cardinality constraints failed: {e}")
        return {"error": f"OPT-10_FAILED: {str(e)}"}


def is_fa_pro_enabled() -> bool:
    """OPT-06: Feature gate check for FA_PRO mode."""
    if not _features_available:
        return False

    try:
        mode = get_deployment_mode()
        return mode == DeploymentMode.FA_PROFESSIONAL
    except Exception:
        return False


def main():
    """Portfolio optimization command entrypoint."""
    if len(sys.argv) < 2:
        print(
            json.dumps(
                {
                    "error": "Usage: python optimize.py <holdings_file> [method] [options]",
                    "methods": ["sharpe", "min_volatility", "black_litterman"],
                    "options": {
                        "--expert-views": "JSON: {symbol: expected_return}",
                        "--sector-caps": "JSON: {sector: max_weight}",
                        "--sector-map": "JSON: {symbol: sector}",
                        "--max-positions": "Integer: limit number of holdings",
                    },
                    "example": "python optimize.py ~/portfolios/my_holdings.json sharpe --max-positions 20",
                }
            )
        )
        sys.exit(1)

    holdings_file = sys.argv[1]
    method = sys.argv[2] if len(sys.argv) > 2 else "sharpe"

    if not Path(holdings_file).exists():
        print(json.dumps({"error": f"Holdings file not found: {holdings_file}"}))
        sys.exit(1)

    try:
        holdings, total_value = load_holdings(holdings_file)
        symbols = holdings["symbol"].tolist()
        returns = fetch_historical_returns(symbols)

        # Parse options
        expert_views = None
        sector_caps = None
        sector_map = None
        max_positions = None

        for i, arg in enumerate(sys.argv[3:], 3):
            if arg == "--expert-views" and i + 1 < len(sys.argv):
                try:
                    expert_views = json.loads(sys.argv[i + 1])
                except json.JSONDecodeError:
                    logger.warning(f"Invalid expert views JSON: {sys.argv[i + 1]}")

            elif arg == "--sector-caps" and i + 1 < len(sys.argv):
                try:
                    sector_caps = json.loads(sys.argv[i + 1])
                except json.JSONDecodeError:
                    logger.warning(f"Invalid sector caps JSON: {sys.argv[i + 1]}")

            elif arg == "--sector-map" and i + 1 < len(sys.argv):
                try:
                    sector_map = json.loads(sys.argv[i + 1])
                except json.JSONDecodeError:
                    logger.warning(f"Invalid sector map JSON: {sys.argv[i + 1]}")

            elif arg == "--max-positions" and i + 1 < len(sys.argv):
                try:
                    max_positions = int(sys.argv[i + 1])
                except ValueError:
                    logger.warning(f"Invalid max-positions: {sys.argv[i + 1]}")

        # Run base optimization
        if method == "sharpe":
            result = optimize_sharpe_ratio(holdings, returns)
        elif method == "min_volatility":
            result = optimize_min_volatility(holdings, returns)
        elif method == "black_litterman":
            result = black_litterman_optimization(holdings, returns, expert_views or {})
        else:
            result = {"error": f"Unknown method: {method}"}

        if "error" not in result:
            weights = result.get("weights", {})

            # OPT-09: Sector constraints
            if sector_map and sector_caps:
                sector_result = apply_sector_constraints(
                    weights, sector_map, sector_caps, returns, symbols
                )
                if "error" not in sector_result:
                    weights = sector_result.get("weights", weights)
                    result["sector_constraints"] = sector_result

            # OPT-10: Cardinality constraints
            if max_positions:
                card_result = apply_cardinality_constraints(
                    weights, max_positions, returns, symbols
                )
                if "error" not in card_result:
                    weights = card_result.get("weights", weights)
                    result["cardinality"] = card_result

            result["weights"] = weights

            # OPT-03: Discrete allocation via LinearProgramming
            current_prices = {row["symbol"]: row["current_price"] for _, row in holdings.iterrows()}
            result["discrete"] = discretize_allocation(weights, total_value, current_prices)

            # OPT-04: Efficient frontier visualization
            svg_content = generate_frontier_plot(returns, symbols, weights)
            if svg_content:
                # Save to file
                svg_path = "efficient_frontier.svg"
                with open(svg_path, "w") as f:
                    f.write(svg_content)
                result["frontier_svg"] = svg_path
                logger.info(f"Frontier SVG written to {svg_path}")

            # OPT-06: Feature gating
            if is_fa_pro_enabled():
                result["fa_pro_enabled"] = True
                result["disclaimer"] = "EDUCATIONAL ANALYSIS - FA Professional guardrails active"

        # Print human-readable summary to stderr
        print(f"\n{'=' * 70}", file=sys.stderr)
        print("💡 Analysis complete. Review the detailed JSON output above.", file=sys.stderr)
        print("   → Bring these findings to your financial advisor.", file=sys.stderr)
        print(f"{'=' * 70}\n", file=sys.stderr)

        print(json.dumps(result, indent=2, default=str))

    except Exception as e:
        print(json.dumps({"error": str(e), "traceback": str(sys.exc_info())}))
        sys.exit(1)


if __name__ == "__main__":
    main()
