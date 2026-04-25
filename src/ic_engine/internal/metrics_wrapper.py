#!/usr/bin/env python3
"""
Wrapper around empyrical and numpy for portfolio metrics.
Replaces custom Sharpe, Volatility, Beta, VaR calculations with standard implementations.
"""

import logging
import os
from typing import Dict, Optional

import numpy as np

try:
    import empyrical

    EMPYRICAL_AVAILABLE = True
except ImportError:
    EMPYRICAL_AVAILABLE = False

logger = logging.getLogger(__name__)


def get_risk_free_rate() -> float:
    """Fetch current 10-year Treasury yield from FRED (fallback to 4.0%)."""
    try:
        import requests

        key = os.getenv("FRED_API_KEY")
        if not key:
            return 0.04
        url = "https://api.stlouisfed.org/fred/series/DFF/observations"
        r = requests.get(url, params={"api_key": key, "limit": 1}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data.get("observations"):
                val = float(data["observations"][0]["value"])
                return val / 100  # Convert from percentage
    except Exception as e:
        logger.warning(f"Could not fetch FRED rate: {e}")
    return 0.04


def calculate_sharpe_ratio(returns: np.ndarray, risk_free_rate: Optional[float] = None) -> Dict:
    """Calculate Sharpe ratio using empyrical (annualized returns and volatility)."""
    if EMPYRICAL_AVAILABLE and len(returns) > 1:
        try:
            if risk_free_rate is None:
                risk_free_rate = get_risk_free_rate()
            # Empyrical expects daily returns; assumes 252 trading days per year
            # Note: empyrical.sharpe_ratio uses 'risk_free' parameter (daily rate)
            daily_rate = risk_free_rate / 252
            sharpe = empyrical.sharpe_ratio(returns, risk_free=daily_rate)
            annual_return = empyrical.annual_return(returns)
            annual_volatility = empyrical.annual_volatility(returns)
            return {
                "sharpe_ratio": float(sharpe) if not np.isnan(sharpe) else 0.0,
                "annual_return": float(annual_return) if not np.isnan(annual_return) else 0.0,
                "annual_volatility": float(annual_volatility)
                if not np.isnan(annual_volatility)
                else 0.0,
            }
        except Exception as e:
            logger.warning(f"Empyrical Sharpe calculation failed: {e}")

    # Fallback: custom implementation
    if len(returns) < 2:
        return {"sharpe_ratio": 0.0, "annual_return": 0.0, "annual_volatility": 0.0}

    if risk_free_rate is None:
        risk_free_rate = get_risk_free_rate()

    mean_return = np.mean(returns)
    std_return = np.std(returns, ddof=1)
    annual_return = (1 + mean_return) ** 252 - 1
    annual_vol = std_return * np.sqrt(252)

    if annual_vol == 0:
        sharpe = 0.0
    else:
        sharpe = (annual_return - risk_free_rate) / annual_vol

    return {
        "sharpe_ratio": float(sharpe),
        "annual_return": float(annual_return),
        "annual_volatility": float(annual_vol),
    }


def calculate_volatility(returns: np.ndarray, window: int = 30) -> Dict:
    """Calculate annualized volatility using empyrical."""
    if EMPYRICAL_AVAILABLE and len(returns) > 1:
        try:
            # Empyrical annualized_volatility
            vol = empyrical.annual_volatility(returns)
            rolling_vol = (
                empyrical.annual_volatility(returns[-window:]) if len(returns) >= window else vol
            )
            return {
                "annualized_volatility": float(vol) if not np.isnan(vol) else 0.0,
                "rolling_volatility_30d": float(rolling_vol) if not np.isnan(rolling_vol) else 0.0,
            }
        except Exception as e:
            logger.warning(f"Empyrical volatility calculation failed: {e}")

    # Fallback: custom implementation
    if len(returns) < 2:
        return {"annualized_volatility": 0.0, "rolling_volatility_30d": 0.0}

    std_return = np.std(returns, ddof=1)
    annual_vol = std_return * np.sqrt(252)
    rolling_std = np.std(returns[-window:], ddof=1) if len(returns) >= window else std_return
    rolling_vol = rolling_std * np.sqrt(252)

    return {
        "annualized_volatility": float(annual_vol),
        "rolling_volatility_30d": float(rolling_vol),
    }


def calculate_beta(returns: np.ndarray, benchmark_returns: np.ndarray) -> Dict:
    """Calculate beta (market sensitivity) using linear regression."""
    if len(returns) < 2 or len(benchmark_returns) < 2:
        return {"beta": 0.0, "alpha": 0.0, "r_squared": 0.0}

    try:
        # Ensure same length (align timeseries)
        min_len = min(len(returns), len(benchmark_returns))
        returns = returns[-min_len:]
        benchmark_returns = benchmark_returns[-min_len:]

        # Linear regression: returns = alpha + beta * benchmark_returns
        p, residuals, rank, sv, rcond = np.polyfit(benchmark_returns, returns, 1, full=True)
        beta = p[0]
        alpha = p[1] * 252  # Annualize intercept

        # Calculate R-squared
        ss_res = np.sum(residuals) if len(residuals) > 0 else 0
        ss_tot = np.sum((returns - np.mean(returns)) ** 2)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0.0

        return {
            "beta": float(beta) if not np.isnan(beta) else 0.0,
            "alpha": float(alpha) if not np.isnan(alpha) else 0.0,
            "r_squared": float(r_squared) if not np.isnan(r_squared) else 0.0,
        }
    except Exception as e:
        logger.warning(f"Beta calculation failed: {e}")
        return {"beta": 0.0, "alpha": 0.0, "r_squared": 0.0}


def calculate_var(returns: np.ndarray, confidence: float = 0.95) -> Dict:
    """Calculate Value-at-Risk (VaR) at specified confidence level."""
    if EMPYRICAL_AVAILABLE and len(returns) > 1:
        try:
            var = empyrical.value_at_risk(returns, cutoff=1 - confidence)
            var_annualized = var * np.sqrt(252) * 100  # Annualize and convert to percentage
            return {
                "var_95": float(var) if not np.isnan(var) else 0.0,
                "var_95_annualized": float(var_annualized) if not np.isnan(var_annualized) else 0.0,
            }
        except Exception as e:
            logger.warning(f"Empyrical VaR calculation failed: {e}")

    # Fallback: quantile-based VaR
    if len(returns) < 2:
        return {"var_95": 0.0, "var_95_annualized": 0.0}

    var = np.percentile(returns, (1 - confidence) * 100)
    var_annualized = var * np.sqrt(252) * 100

    return {
        "var_95": float(var),
        "var_95_annualized": float(var_annualized),
    }


def calculate_drawdown(prices: np.ndarray) -> Dict:
    """Calculate maximum drawdown."""
    if len(prices) < 2:
        return {"max_drawdown": 0.0, "current_drawdown": 0.0}

    try:
        cummax = np.maximum.accumulate(prices)
        drawdown = (prices - cummax) / cummax
        max_drawdown = np.min(drawdown)
        current_drawdown = drawdown[-1]

        return {
            "max_drawdown": float(max_drawdown) * 100,  # Convert to percentage
            "current_drawdown": float(current_drawdown) * 100,
        }
    except Exception as e:
        logger.warning(f"Drawdown calculation failed: {e}")
        return {"max_drawdown": 0.0, "current_drawdown": 0.0}


def calculate_sortino_ratio(returns: np.ndarray, risk_free_rate: Optional[float] = None) -> Dict:
    """Calculate Sortino ratio (downside risk only)."""
    if EMPYRICAL_AVAILABLE and len(returns) > 1:
        try:
            if risk_free_rate is None:
                risk_free_rate = get_risk_free_rate()
            sortino = empyrical.sortino_ratio(returns, risk_free_rate=risk_free_rate)
            return {"sortino_ratio": float(sortino) if not np.isnan(sortino) else 0.0}
        except Exception as e:
            logger.warning(f"Empyrical Sortino calculation failed: {e}")

    # Fallback: simplified Sortino
    if len(returns) < 2:
        return {"sortino_ratio": 0.0}

    if risk_free_rate is None:
        risk_free_rate = get_risk_free_rate()

    excess_returns = returns - risk_free_rate / 252
    downside = excess_returns[excess_returns < 0]
    downside_std = np.std(downside, ddof=1) if len(downside) > 0 else np.std(returns, ddof=1)
    annual_return = (1 + np.mean(returns)) ** 252 - 1
    annual_downside_vol = downside_std * np.sqrt(252)

    if annual_downside_vol == 0:
        sortino = 0.0
    else:
        sortino = (annual_return - risk_free_rate) / annual_downside_vol

    return {"sortino_ratio": float(sortino)}


def calculate_calmar_ratio(returns: np.ndarray) -> Dict:
    """Calculate Calmar ratio (return / max drawdown)."""
    if EMPYRICAL_AVAILABLE and len(returns) > 1:
        try:
            calmar = empyrical.calmar_ratio(returns)
            return {"calmar_ratio": float(calmar) if not np.isnan(calmar) else 0.0}
        except Exception as e:
            logger.warning(f"Empyrical Calmar calculation failed: {e}")

    # Fallback: manual calculation
    if len(returns) < 2:
        return {"calmar_ratio": 0.0}

    annual_return = (1 + np.mean(returns)) ** 252 - 1
    cummax = np.maximum.accumulate(np.cumprod(1 + returns))
    drawdown = (np.cumprod(1 + returns) - cummax) / cummax
    max_drawdown = np.min(drawdown)

    if max_drawdown == 0:
        calmar = 0.0
    else:
        calmar = annual_return / abs(max_drawdown)

    return {"calmar_ratio": float(calmar)}
