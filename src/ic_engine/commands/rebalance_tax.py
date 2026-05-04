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
Tax-Aware Rebalancing — Phase 2.2 of CDM 5 Analytics

Generates actionable rebalancing trades with lot selection that minimizes
realized gains.

RBT-01: Max-Sharpe or min-volatility target weights via PyPortfolioOpt
RBT-02: Lot-level sell selection — rank by (gain_pct asc, holding_days desc)
RBT-03: Wash-sale detection (IRS §1091 30-day window, same symbol)
RBT-04: Tax impact projection — short-term vs long-term gains
RBT-05: Before/after metrics (Sharpe delta, tracking error vs target)
RBT-06: Feature gating via is_fa_pro_enabled() — detailed wash-sale only in FA Pro
RBT-07: Trade list output with optional --max-gain-pct cap
RBT-08: Graceful fallback when lot-level data absent (synthesize from aggregate)

CLI:
    python3 rebalance_tax.py <holdings.json>
        [--targets JSON]
        [--method sharpe|min-vol]
        [--max-gain-pct FLOAT]
        [--federal-rate FLOAT]
        [--state-rate FLOAT]
        [output.json]
"""
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 InvestorClaw Contributors

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Ensure project root is on sys.path for config/rendering imports when invoked
# as `python3 commands/rebalance_tax.py` from arbitrary cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ic_engine.internal.holdings_loader import HoldingsLoader  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Optional dependencies (graceful degradation) --------------------------
_PYPORTFOLIOOPT_AVAILABLE = False
try:
    from pypfopt import EfficientFrontier  # type: ignore

    _PYPORTFOLIOOPT_AVAILABLE = True
except ImportError as e:
    logger.warning(
        "pyportfolioopt not installed — RBT-01 unavailable. "
        "Install: pip install pyportfolioopt. Error: %s",
        e,
    )

# --- Feature gating --------------------------------------------------------
_features_available = False
try:
    from ic_engine.config.config_loader import get_deployment_mode
    from ic_engine.config.deployment_modes import DeploymentMode

    _features_available = True
except ImportError as e:
    logger.debug("deployment_modes not importable (%s) — feature gating disabled", e)

# --- Disclaimer wrapper ----------------------------------------------------
try:
    from ic_engine.rendering.disclaimer_wrapper import DisclaimerWrapper

    _DISCLAIMER_AVAILABLE = True
except ImportError as e:
    _DISCLAIMER_AVAILABLE = False
    logger.debug("DisclaimerWrapper not importable (%s) — raw JSON output will be used", e)


# Default tax rates (federal + long-term capital gains). Override via CLI.
DEFAULT_FEDERAL_LT_RATE = 0.15  # 15% long-term federal capital gains
DEFAULT_FEDERAL_ST_RATE = 0.24  # 24% short-term federal (ordinary income estimate)
DEFAULT_STATE_RATE = 0.05  # 5% state income (representative)
WASH_SALE_WINDOW_DAYS = 30  # IRS §1091
LONG_TERM_THRESHOLD_DAYS = 365  # > 1 year = long-term


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------
def is_fa_pro_enabled() -> bool:
    """RBT-06: Feature gate check for FA_PRO mode."""
    if not _features_available:
        return False
    try:
        mode = get_deployment_mode()
        return mode == DeploymentMode.FA_PROFESSIONAL
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CDM holdings loader — delegates to internal/holdings_loader.py
# ---------------------------------------------------------------------------
def load_holdings_with_lots(holdings_file: str) -> Tuple[pd.DataFrame, List[Dict[str, Any]], float]:
    """
    Load CDM holdings into:
      - holdings DataFrame (aggregate per symbol, for optimizer)
      - lot_records (flat list across all positions)
      - total_portfolio_value

    Filters cash-only positions from the optimizer input. Lot-level detail
    is synthesized from position-level aggregates when the envelope does not
    provide explicit tax lots (preserving RBT-08 behaviour).
    """
    portfolio = HoldingsLoader().load(holdings_file)

    holdings_rows: List[Dict[str, Any]] = []
    lot_records: List[Dict[str, Any]] = []

    for pos in portfolio.positions:
        # Exclude cash from optimizer universe
        if pos.asset_class == "cash" or pos.symbol.upper() == "CASH":
            continue
        if pos.shares is None or pos.current_price is None:
            continue

        account_id = pos.account or "default"
        cost_basis_price = (
            pos.cost_basis_price if pos.cost_basis_price is not None else pos.current_price
        )

        holdings_rows.append(
            {
                "symbol": pos.symbol,
                "shares": float(pos.shares),
                "current_price": float(pos.current_price),
                "cost_basis_price": float(cost_basis_price),
                "account": account_id,
                "tradable": bool(pos.tradable),
            }
        )

        for lot in pos.lots:
            lot_shares = lot.get("shares")
            if lot_shares is None:
                continue
            lot_cb = lot.get("cost_basis_price")
            if lot_cb is None:
                lot_cb = pos.current_price
            lot_records.append(
                {
                    "symbol": pos.symbol,
                    "account": account_id,
                    "shares": float(lot_shares),
                    "cost_basis_price": float(lot_cb),
                    "current_price": float(pos.current_price),
                    "acquisition_date": lot.get("acquisition_date"),
                }
            )

    if not holdings_rows:
        return (
            pd.DataFrame(columns=["symbol", "shares", "current_price", "value", "weight"]),
            [],
            0.0,
        )

    df = pd.DataFrame(holdings_rows)
    df["value"] = df["shares"] * df["current_price"]
    total_value = float(df["value"].sum())
    df["weight"] = df["value"] / total_value if total_value > 0 else 0.0

    return df, lot_records, total_value


# ---------------------------------------------------------------------------
# Target-weight solver
# ---------------------------------------------------------------------------
def _fetch_returns(symbols: List[str], period: str = "1y") -> pd.DataFrame:
    from ic_engine.providers.price_panel import get_close_panel

    if not symbols:
        return pd.DataFrame()
    logger.info("Fetching %s historical data for %d symbols", period, len(symbols))
    try:
        data = get_close_panel(symbols, period=period)
    except Exception as e:
        logger.warning("PriceProvider history fetch failed: %s", e)
        return pd.DataFrame()
    if data is None or data.empty:
        return pd.DataFrame()
    # Drop columns that are entirely NaN (symbols no provider could resolve)
    data = data.dropna(axis=1, how="all")
    if data.empty:
        return pd.DataFrame()
    return data.pct_change(fill_method=None).dropna(how="all")


def _portfolio_perf(weights: Dict[str, float], returns: pd.DataFrame) -> Dict[str, float]:
    """Compute annualized return, volatility, Sharpe for a weight vector."""
    symbols = [s for s in weights.keys() if s in returns.columns]
    if not symbols:
        return {"annual_return": 0.0, "annual_volatility": 0.0, "sharpe_ratio": 0.0}
    w = np.array([weights.get(s, 0.0) for s in symbols], dtype=float)
    total = w.sum()
    if total > 0:
        w = w / total
    mu = returns[symbols].mean().values * 252
    cov = returns[symbols].cov().values * 252
    ret = float(np.dot(w, mu))
    vol = float(np.sqrt(max(w @ cov @ w, 1e-12)))
    sharpe = ret / vol if vol > 0 else 0.0
    return {
        "annual_return": ret,
        "annual_volatility": vol,
        "sharpe_ratio": float(sharpe),
    }


def compute_target_weights(
    holdings: pd.DataFrame,
    method: str = "sharpe",
    overrides: Optional[Dict[str, float]] = None,
) -> Tuple[Dict[str, float], pd.DataFrame]:
    """
    RBT-01: Compute target weights via PyPortfolioOpt or fall back to equal-weight.
    Returns (weights, returns DataFrame).
    """
    all_symbols = holdings["symbol"].tolist()
    # Only optimize over tradable (public-ticker) symbols. Non-tradable
    # positions (bonds, CUSIPs) keep their current weight in the target.
    tradable_mask = holdings.get("tradable")
    if tradable_mask is None:
        tradable_symbols = all_symbols
    else:
        tradable_symbols = holdings.loc[holdings["tradable"].astype(bool), "symbol"].tolist()

    # Reserve fraction of portfolio held in non-tradable assets
    total_value = float((holdings["shares"] * holdings["current_price"]).sum())
    nontradable = [s for s in all_symbols if s not in tradable_symbols]
    nontradable_weights: Dict[str, float] = {}
    if total_value > 0 and nontradable:
        for _, row in holdings.iterrows():
            if row["symbol"] in nontradable:
                nontradable_weights[row["symbol"]] = float(
                    (row["shares"] * row["current_price"]) / total_value
                )
    reserved = sum(nontradable_weights.values())
    budget = max(1.0 - reserved, 0.0)

    if overrides:
        # Direct override path: renormalize to 1.0, fill missing with 0
        w = {s: float(overrides.get(s, 0.0)) for s in all_symbols}
        total = sum(w.values())
        if total > 0:
            w = {k: v / total for k, v in w.items()}
        try:
            returns = _fetch_returns(tradable_symbols)
        except Exception as e:
            logger.warning("Return fetch failed (%s); skipping performance metrics", e)
            returns = pd.DataFrame()
        return w, returns

    def _equal_weight_fallback() -> Dict[str, float]:
        out = dict(nontradable_weights)
        n = len(tradable_symbols)
        if n > 0 and budget > 0:
            share = budget / n
            for s in tradable_symbols:
                out[s] = share
        return out

    if not _PYPORTFOLIOOPT_AVAILABLE or not tradable_symbols:
        if not _PYPORTFOLIOOPT_AVAILABLE:
            logger.warning("pyportfolioopt unavailable — falling back to equal-weight targets")
        return _equal_weight_fallback(), pd.DataFrame()

    try:
        returns = _fetch_returns(tradable_symbols)
    except Exception as e:
        logger.warning("Return fetch failed (%s); using equal-weight fallback", e)
        return _equal_weight_fallback(), pd.DataFrame()

    # Restrict symbols to those with usable return data
    usable = [s for s in tradable_symbols if s in returns.columns]
    if len(usable) < 2 or returns.empty:
        logger.warning("Insufficient return data for optimization; using equal-weight fallback")
        return _equal_weight_fallback(), returns

    mu = returns[usable].mean() * 252
    S = returns[usable].cov() * 252

    try:
        ef = EfficientFrontier(mu, S, weight_bounds=(0, 1))
        if method == "min-vol" or method == "min_volatility":
            weights = ef.min_volatility()
        else:
            # PyPortfolioOpt exposes max_sharpe() in 1.x and max_sharpe_ratio()
            # in some older forks; try both.
            if hasattr(ef, "max_sharpe"):
                weights = ef.max_sharpe()
            else:
                weights = ef.max_sharpe_ratio()
    except Exception as e:
        logger.warning("EfficientFrontier failed (%s); using equal-weight fallback", e)
        return _equal_weight_fallback(), returns

    # Scale tradable weights by budget (so non-tradable weights sum to reserved)
    final: Dict[str, float] = dict(nontradable_weights)
    for sym in usable:
        final[sym] = float(weights.get(sym, 0.0)) * budget
    # Symbols with no return data get 0 allocation
    for sym in tradable_symbols:
        final.setdefault(sym, 0.0)
    return final, returns


# ---------------------------------------------------------------------------
# Lot-level sell selection
# ---------------------------------------------------------------------------
def _parse_date(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00").split("+")[0])
    except Exception:
        # Try plain date format
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d")
        except Exception:
            return None


def _rank_lots_for_sale(lots: List[Dict[str, Any]], as_of: datetime) -> List[Dict[str, Any]]:
    """
    RBT-02: Rank lots to minimize realized gains.

    Primary sort: gain_pct ascending (prefer losses first).
    Secondary sort: holding_days descending (prefer longest-held ties, which
    tend to qualify for long-term rates on the positive-gain side).
    """
    enriched = []
    for lot in lots:
        cb = lot["cost_basis_price"]
        cp = lot["current_price"]
        gain_pct = (cp - cb) / cb if cb > 0 else 0.0
        acq = _parse_date(lot.get("acquisition_date"))
        holding_days = (as_of - acq).days if acq else None
        enriched.append(
            {
                **lot,
                "gain_pct": gain_pct,
                "holding_days": holding_days,
            }
        )

    def _key(lot: Dict[str, Any]) -> Tuple[float, int]:
        # Sort by gain_pct ascending, then holding_days descending (longest-held first)
        hd = lot["holding_days"]
        return (lot["gain_pct"], -(hd if hd is not None else 0))

    enriched.sort(key=_key)
    return enriched


def _select_lots_to_sell(
    symbol_lots: List[Dict[str, Any]],
    shares_to_sell: float,
    as_of: datetime,
    max_gain_pct: Optional[float],
) -> Tuple[List[Dict[str, Any]], float]:
    """Select lots (in rank order) to cover shares_to_sell. Returns (selected, remaining_to_sell)."""
    ranked = _rank_lots_for_sale(symbol_lots, as_of)
    selected: List[Dict[str, Any]] = []
    remaining = shares_to_sell

    for lot in ranked:
        if remaining <= 0:
            break
        if max_gain_pct is not None and lot["gain_pct"] > max_gain_pct:
            # Cap: skip lots whose realization exceeds the user's gain tolerance
            continue
        take = min(lot["shares"], remaining)
        if take <= 0:
            continue
        selected.append({**lot, "shares_sold": take})
        remaining -= take

    return selected, max(remaining, 0.0)


# ---------------------------------------------------------------------------
# Wash-sale detection
# ---------------------------------------------------------------------------
def detect_wash_sales(
    sell_orders: List[Dict[str, Any]],
    all_lots: List[Dict[str, Any]],
    sold_date: datetime,
    window_days: int = WASH_SALE_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    """
    RBT-03: Detect potential wash sales.

    A wash sale occurs when a security is sold at a loss and a substantially
    identical security is purchased within 30 days BEFORE or AFTER the sale.
    We flag any same-symbol purchase lot across all accounts in that window.
    """
    flags: List[Dict[str, Any]] = []
    symbols_sold_at_loss = {o["symbol"] for o in sell_orders if o.get("gain_pct", 0.0) < 0.0}

    for lot in all_lots:
        if lot["symbol"] not in symbols_sold_at_loss:
            continue
        acq = _parse_date(lot.get("acquisition_date"))
        if acq is None:
            continue
        delta = (sold_date - acq).days
        if abs(delta) <= window_days:
            flags.append(
                {
                    "symbol": lot["symbol"],
                    "sold_date": sold_date.date().isoformat(),
                    "prior_purchase": acq.date().isoformat(),
                    "days": int(delta),
                    "account": lot.get("account", "default"),
                }
            )

    return flags


# ---------------------------------------------------------------------------
# Trade list builder
# ---------------------------------------------------------------------------
def build_trade_list(
    holdings: pd.DataFrame,
    lot_records: List[Dict[str, Any]],
    target_weights: Dict[str, float],
    total_value: float,
    max_gain_pct: Optional[float],
    federal_rate_lt: float,
    federal_rate_st: float,
    state_rate: float,
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    RBT-02/RBT-04/RBT-07: Build the tax-aware trade list.

    Generates SELL orders for overweight symbols using lot ranking, and BUY
    orders for underweight symbols. Computes per-trade tax impact and
    aggregate tax summary.
    """
    as_of = as_of or datetime.now()
    trade_list: List[Dict[str, Any]] = []

    # Index lots by symbol (aggregate across accounts for selection pool)
    lots_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for lot in lot_records:
        lots_by_symbol.setdefault(lot["symbol"], []).append(lot)

    # Current weights
    current_weights = {
        row["symbol"]: (row["shares"] * row["current_price"]) / total_value
        if total_value > 0
        else 0.0
        for _, row in holdings.iterrows()
    }
    {row["symbol"]: float(row["shares"]) for _, row in holdings.iterrows()}
    current_price_map = {
        row["symbol"]: float(row["current_price"]) for _, row in holdings.iterrows()
    }

    total_realized_gain = 0.0
    long_term_gain = 0.0
    short_term_gain = 0.0

    all_symbols = set(current_weights) | set(target_weights)
    for symbol in sorted(all_symbols):
        tgt_w = target_weights.get(symbol, 0.0)
        cur_w = current_weights.get(symbol, 0.0)
        tgt_w - cur_w
        target_value = tgt_w * total_value
        current_value = cur_w * total_value
        price = current_price_map.get(symbol, 0.0)
        if price <= 0:
            continue

        delta_value = target_value - current_value
        delta_shares = delta_value / price

        if abs(delta_value) < 1.0:
            continue  # ignore sub-$1 drift

        if delta_shares < 0:
            # SELL path — lot-level selection
            shares_to_sell = abs(delta_shares)
            available = lots_by_symbol.get(symbol, [])
            selected, unfilled = _select_lots_to_sell(
                available, shares_to_sell, as_of, max_gain_pct
            )

            for lot in selected:
                shares_sold = lot["shares_sold"]
                cb = lot["cost_basis_price"]
                realized_gain = (price - cb) * shares_sold
                gain_pct = (price - cb) / cb if cb > 0 else 0.0
                hd = lot.get("holding_days")
                is_long = hd is not None and hd >= LONG_TERM_THRESHOLD_DAYS
                tax_rate = (
                    (federal_rate_lt + state_rate) if is_long else (federal_rate_st + state_rate)
                )
                tax_impact = max(realized_gain, 0.0) * tax_rate  # losses offset but don't "cost"

                total_realized_gain += realized_gain
                if is_long:
                    long_term_gain += realized_gain
                else:
                    short_term_gain += realized_gain

                trade_list.append(
                    {
                        "action": "SELL",
                        "symbol": symbol,
                        "shares": round(shares_sold, 4),
                        "current_price": round(price, 4),
                        "cost_basis": round(cb, 4),
                        "gain_pct": round(gain_pct, 6),
                        "realized_gain": round(realized_gain, 2),
                        "tax_impact": round(tax_impact, 2),
                        "wash_sale_flag": False,  # filled in second pass
                        "holding_period": "long" if is_long else "short",
                        "holding_days": hd,
                        "account": lot.get("account", "default"),
                        "acquisition_date": lot.get("acquisition_date"),
                    }
                )

            if unfilled > 0:
                logger.warning(
                    "Could not fully rebalance %s: %.4f shares blocked by --max-gain-pct cap",
                    symbol,
                    unfilled,
                )

        else:
            # BUY path — no tax consequence
            trade_list.append(
                {
                    "action": "BUY",
                    "symbol": symbol,
                    "shares": round(delta_shares, 4),
                    "current_price": round(price, 4),
                    "cost_basis": None,
                    "gain_pct": None,
                    "tax_impact": 0.0,
                    "wash_sale_flag": False,
                    "holding_period": "n/a",
                }
            )

    # Second pass: wash-sale flags on SELL orders
    sell_orders = [t for t in trade_list if t["action"] == "SELL"]
    wash_flags = (
        detect_wash_sales(sell_orders, lot_records, as_of) if is_fa_pro_enabled() or True else []
    )
    flagged_symbols = {f["symbol"] for f in wash_flags}
    for t in trade_list:
        if t["action"] == "SELL" and t["symbol"] in flagged_symbols and t.get("gain_pct", 0) < 0:
            t["wash_sale_flag"] = True

    tax_summary = {
        "total_realized_gain": round(total_realized_gain, 2),
        "long_term_gain": round(long_term_gain, 2),
        "short_term_gain": round(short_term_gain, 2),
        "estimated_tax_cost": round(
            max(long_term_gain, 0.0) * (federal_rate_lt + state_rate)
            + max(short_term_gain, 0.0) * (federal_rate_st + state_rate),
            2,
        ),
        "federal_rate_lt": federal_rate_lt,
        "federal_rate_st": federal_rate_st,
        "state_rate": state_rate,
    }

    return {
        "trade_list": trade_list,
        "tax_summary": tax_summary,
        "wash_sale_flags": wash_flags,
    }


# ---------------------------------------------------------------------------
# Before/after metrics
# ---------------------------------------------------------------------------
def _tracking_error(weights: Dict[str, float], target: Dict[str, float]) -> float:
    """Simple L2 deviation from target (not true TE which needs return series)."""
    all_sym = set(weights) | set(target)
    diff = [weights.get(s, 0.0) - target.get(s, 0.0) for s in all_sym]
    return float(np.sqrt(sum(d * d for d in diff)))


def compute_before_after_metrics(
    holdings: pd.DataFrame,
    target_weights: Dict[str, float],
    returns: pd.DataFrame,
    total_value: float,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """RBT-05: before/after Sharpe + tracking error vs target."""
    current_weights = {
        row["symbol"]: (row["shares"] * row["current_price"]) / total_value
        if total_value > 0
        else 0.0
        for _, row in holdings.iterrows()
    }

    if returns is None or returns.empty:
        before = {
            "sharpe": 0.0,
            "tracking_error": round(_tracking_error(current_weights, target_weights), 4),
        }
        after = {"sharpe": 0.0, "tracking_error": 0.0}
        return before, after

    before_perf = _portfolio_perf(current_weights, returns)
    after_perf = _portfolio_perf(target_weights, returns)

    before = {
        "sharpe": round(before_perf["sharpe_ratio"], 4),
        "annual_return": round(before_perf["annual_return"], 4),
        "annual_volatility": round(before_perf["annual_volatility"], 4),
        "tracking_error": round(_tracking_error(current_weights, target_weights), 4),
    }
    after = {
        "sharpe": round(after_perf["sharpe_ratio"], 4),
        "annual_return": round(after_perf["annual_return"], 4),
        "annual_volatility": round(after_perf["annual_volatility"], 4),
        "tracking_error": 0.0,  # by construction, target == target
    }
    return before, after


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: List[str]) -> Dict[str, Any]:
    args = {
        "holdings_file": None,
        "output_file": None,
        "targets": None,
        "method": "sharpe",
        "max_gain_pct": None,
        "federal_rate_lt": DEFAULT_FEDERAL_LT_RATE,
        "federal_rate_st": DEFAULT_FEDERAL_ST_RATE,
        "state_rate": DEFAULT_STATE_RATE,
    }

    positional: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--targets" and i + 1 < len(argv):
            try:
                args["targets"] = json.loads(argv[i + 1])
            except json.JSONDecodeError as e:
                logger.warning("Invalid --targets JSON: %s", e)
            i += 2
        elif a == "--method" and i + 1 < len(argv):
            m = argv[i + 1].lower()
            args["method"] = (
                "min-vol" if m in ("min-vol", "min_volatility", "min_vol") else "sharpe"
            )
            i += 2
        elif a == "--max-gain-pct" and i + 1 < len(argv):
            try:
                args["max_gain_pct"] = float(argv[i + 1])
            except ValueError:
                logger.warning("Invalid --max-gain-pct: %s", argv[i + 1])
            i += 2
        elif a == "--federal-rate" and i + 1 < len(argv):
            try:
                args["federal_rate_lt"] = float(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif a == "--federal-rate-st" and i + 1 < len(argv):
            try:
                args["federal_rate_st"] = float(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif a == "--state-rate" and i + 1 < len(argv):
            try:
                args["state_rate"] = float(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif a.startswith("--"):
            # Unknown flag: skip value if present
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                i += 2
            else:
                i += 1
        else:
            positional.append(a)
            i += 1

    if positional:
        args["holdings_file"] = positional[0]
    if len(positional) > 1:
        args["output_file"] = positional[1]

    return args


def _usage() -> Dict[str, Any]:
    return {
        "error": "Usage: python3 rebalance_tax.py <holdings.json> "
        "[--targets JSON] [--method sharpe|min-vol] "
        "[--max-gain-pct FLOAT] [--federal-rate FLOAT] "
        "[--federal-rate-st FLOAT] [--state-rate FLOAT] "
        "[output.json]",
        "methods": ["sharpe", "min-vol"],
        "example": "python3 rebalance_tax.py holdings.json --method sharpe "
        "--max-gain-pct 0.20 rebalance_tax.json",
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps(_usage(), indent=2))
        return 1

    parsed = _parse_args(sys.argv[1:])
    holdings_file = parsed["holdings_file"]

    if not holdings_file or not Path(holdings_file).exists():
        print(json.dumps({"error": f"Holdings file not found: {holdings_file}"}))
        return 1

    try:
        holdings, lot_records, total_value = load_holdings_with_lots(holdings_file)
    except Exception as e:
        print(json.dumps({"error": f"Failed to load holdings: {e}"}))
        return 1

    if holdings.empty:
        print(json.dumps({"error": "No holdings extracted from input file"}))
        return 1

    # Compute target weights
    target_weights, returns = compute_target_weights(
        holdings, method=parsed["method"], overrides=parsed["targets"]
    )

    # Build trade list
    result = build_trade_list(
        holdings=holdings,
        lot_records=lot_records,
        target_weights=target_weights,
        total_value=total_value,
        max_gain_pct=parsed["max_gain_pct"],
        federal_rate_lt=parsed["federal_rate_lt"],
        federal_rate_st=parsed["federal_rate_st"],
        state_rate=parsed["state_rate"],
    )

    before_metrics, after_metrics = compute_before_after_metrics(
        holdings, target_weights, returns, total_value
    )
    result["before_metrics"] = before_metrics
    result["after_metrics"] = after_metrics
    result["target_weights"] = {k: round(v, 6) for k, v in target_weights.items()}
    result["method"] = parsed["method"]
    result["total_portfolio_value"] = round(total_value, 2)
    result["fa_pro_enabled"] = is_fa_pro_enabled()
    if parsed["max_gain_pct"] is not None:
        result["max_gain_pct_cap"] = parsed["max_gain_pct"]

    # Check for wash-sale flags
    wash_sale_flags = result.get("wash_sale_flags", [])
    wash_sale_count = (
        len(wash_sale_flags)
        if isinstance(wash_sale_flags, list)
        else sum(1 for v in wash_sale_flags.values() if v)
    )
    if wash_sale_count > 0:
        print(
            f"\n⚠️  WASH-SALE WARNING (IRS §1091)\n"
            f"   {wash_sale_count} sells flagged for wash-sale risk.\n"
            f"   Review the wash_sale_flags below before executing any trades.\n"
            f"   Consult your tax advisor before proceeding.\n",
            file=sys.stderr,
        )

    # Output
    output_file = parsed["output_file"]
    if output_file and _DISCLAIMER_AVAILABLE:
        deployment_mode = "fa_professional" if is_fa_pro_enabled() else None
        DisclaimerWrapper.wrap_and_save(
            result,
            output_file,
            analysis_type="Tax-Aware Rebalancing",
            deployment_mode=deployment_mode,
        )
        print(
            json.dumps(
                {"status": "ok", "output_file": output_file, "trades": len(result["trade_list"])},
                indent=2,
            )
        )
    elif output_file:
        with open(output_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(
            json.dumps(
                {"status": "ok", "output_file": output_file, "trades": len(result["trade_list"])},
                indent=2,
            )
        )
    else:
        if _DISCLAIMER_AVAILABLE:
            deployment_mode = "fa_professional" if is_fa_pro_enabled() else None
            wrapped = DisclaimerWrapper.wrap_output(
                result,
                analysis_type="Tax-Aware Rebalancing",
                deployment_mode=deployment_mode,
            )
            print(json.dumps(wrapped, indent=2, default=str))
        else:
            print(json.dumps(result, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())
