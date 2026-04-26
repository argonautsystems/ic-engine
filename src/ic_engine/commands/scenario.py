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
Macro Stress Tests — Phase 2.3 CDM 5 Analytics

Reprices the portfolio under macro shocks and surfaces drawdown / VaR
outcomes. Supports built-in canonical scenarios (rate shifts, credit
widening, 2008/2020 equity crashes) as well as user-specified custom
shocks.

Usage:
    python3 scenario.py <holdings.json>
                        [performance.json]
                        [--scenarios LIST]
                        [--shocks JSON]
                        [--artifact PATH]
                        [output.json]

Algorithm overview:
    Bonds:    ΔP/P = -D·Δy + 0.5·C·Δy²           (duration/convexity)
    Equities: Δr   = β·shock_mkt + φ·shock_sector + ε   (factor model)
    Portfolio: weighted sum → new_value, drawdown_pct, var_95, var_99

VaR methodology:
    * Monte Carlo: 10,000 simulations by default (100,000 if GPU available
      via cupy; transparently falls back to NumPy otherwise).
    * For each simulation, each holding's shock is drawn from
      N(scenario_impact, per_holding_sigma) where sigma is derived from
      per-symbol volatility in performance.json (or a reasonable default).
    * VaR_95 / VaR_99 report loss magnitudes at those confidence levels.

Output: JSON blob with a `scenarios` array; optional HTML artifact with
live-repricing range sliders (Plotly + vanilla JS) when --artifact given.
"""

from __future__ import annotations

import json
import logging
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Bootstrap project root for sibling imports when invoked as `python3 commands/scenario.py`
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402

from ic_engine.internal.holdings_loader import HoldingsLoader  # noqa: E402
from ic_engine.rendering.disclaimer_wrapper import DisclaimerWrapper  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Optional GPU acceleration for the 100K-path Monte Carlo case.
_CUPY_AVAILABLE = False
try:
    import cupy as _cp  # type: ignore

    _CUPY_AVAILABLE = True
except Exception:
    _cp = None  # type: ignore


# ---------------------------------------------------------------------------
# Built-in scenario shock definitions
# ---------------------------------------------------------------------------
# Each scenario carries three levers consumed by the repricing model:
#     rate_shock_bps    — parallel yield curve shift (for bond duration/convexity)
#     credit_shock_bps  — additional spread for corporate bonds (on top of rates)
#     equity_mkt_shock  — factor shock applied via each equity's beta
#     sector_shocks     — sector-specific incremental shocks (additive)
#     description       — human-readable scenario label
#
# 2008 and 2020 sector shocks are drawn from realized S&P 500 peak-to-trough
# sector returns during those drawdowns (educational approximations).

BUILTIN_SCENARIOS: Dict[str, Dict[str, Any]] = {
    "rates_up_150bps": {
        "description": "Parallel yield curve shift +150bps",
        "rate_shock_bps": 150.0,
        "credit_shock_bps": 0.0,
        "equity_mkt_shock": -0.04,  # rising rates typically drag equities
        "sector_shocks": {
            "Technology": -0.02,
            "Real Estate": -0.06,
            "Utilities": -0.04,
            "Financials": 0.015,  # banks benefit from net interest margin
        },
    },
    "rates_down_100bps": {
        "description": "Parallel yield curve shift -100bps",
        "rate_shock_bps": -100.0,
        "credit_shock_bps": 0.0,
        "equity_mkt_shock": 0.03,
        "sector_shocks": {
            "Technology": 0.02,
            "Real Estate": 0.05,
            "Utilities": 0.03,
            "Financials": -0.015,
        },
    },
    "credit_shock": {
        "description": "Corporate credit spreads widen +200bps",
        "rate_shock_bps": 0.0,
        "credit_shock_bps": 200.0,
        "equity_mkt_shock": -0.05,
        "sector_shocks": {
            "Financials": -0.08,
            "Energy": -0.06,
            "Consumer Discretionary": -0.04,
        },
    },
    "equity_crash_2008": {
        "description": "Global Financial Crisis 2008 regime (peak-to-trough)",
        "rate_shock_bps": -100.0,
        "credit_shock_bps": 300.0,
        "equity_mkt_shock": -0.37,  # SPX -37% in 2008
        "sector_shocks": {
            "Financials": -0.22,
            "Real Estate": -0.18,
            "Energy": -0.10,
            "Consumer Discretionary": -0.07,
            "Technology": -0.05,
            "Materials": -0.08,
            "Industrials": -0.08,
            "Consumer Staples": 0.10,  # defensive outperformance
            "Health Care": 0.12,
            "Utilities": 0.08,
            "Communication Services": -0.02,
        },
    },
    "equity_crash_2020": {
        "description": "COVID-19 drawdown regime (Q1 2020)",
        "rate_shock_bps": -150.0,
        "credit_shock_bps": 150.0,
        "equity_mkt_shock": -0.34,  # SPX peak-to-trough Feb-Mar 2020
        "sector_shocks": {
            "Energy": -0.20,
            "Financials": -0.12,
            "Industrials": -0.10,
            "Real Estate": -0.08,
            "Consumer Discretionary": -0.06,
            "Technology": 0.06,  # WFH tailwind
            "Communication Services": 0.04,
            "Health Care": 0.05,
            "Consumer Staples": 0.03,
            "Utilities": 0.00,
        },
    },
}


# ---------------------------------------------------------------------------
# Holdings / performance loading
# ---------------------------------------------------------------------------


def load_holdings(path: str) -> Tuple[List[Dict[str, Any]], float]:
    """Load CDM portfolio JSON and return normalized holdings + total value.

    Delegates to :class:`internal.holdings_loader.HoldingsLoader`; the flat
    dict shape returned here (`asset_type`, `market_value`, `sector`, bond
    analytics fields, etc.) is produced by ``Position.to_dict()``.
    """
    portfolio = HoldingsLoader().load(path)
    holdings: List[Dict[str, Any]] = []
    for pos in portfolio.positions:
        if pos.market_value is None:
            # Preserve legacy behaviour: skip positions without a computable MV
            continue
        # Keep only the fields scenario.py actually reads downstream; this
        # makes the dict shape predictable for the stress-test model.
        holdings.append(
            {
                "symbol": pos.symbol,
                "asset_type": pos.asset_class,
                "sector": pos.sector or "Other",
                "market_value": float(pos.market_value),
                "quantity": float(pos.shares) if pos.shares is not None else None,
                "price": float(pos.current_price) if pos.current_price is not None else None,
                "modified_duration": pos.modified_duration,
                "macaulay_duration": pos.macaulay_duration,
                "convexity": pos.convexity,
                "coupon_rate": pos.coupon_rate,
                "years_to_maturity": pos.years_to_maturity,
                "is_corporate": pos.is_corporate if pos.asset_class == "bond" else False,
            }
        )

    total_value = float(portfolio.total_value)
    if total_value <= 0:
        logger.warning("Zero or negative portfolio value after load; repricing will be trivial")
    logger.info(f"Loaded {len(holdings)} holdings totaling ${total_value:,.2f}")
    return holdings, total_value


def load_performance(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Load per-symbol beta + volatility from analyze_performance output."""
    if not path:
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        logger.warning(f"performance.json not found: {path}")
        return {}
    except json.JSONDecodeError as e:
        logger.warning(f"performance.json parse error: {e}")
        return {}

    # DisclaimerWrapper.wrap_and_save puts payload under .data
    payload = raw.get("data", raw) if isinstance(raw, dict) else {}
    per_sym = payload.get("performance") or {}

    normalized: Dict[str, Dict[str, Any]] = {}
    if isinstance(per_sym, dict):
        for sym, metrics in per_sym.items():
            if not isinstance(metrics, dict):
                continue
            beta_block = metrics.get("beta") or {}
            vol_block = metrics.get("volatility") or {}
            normalized[sym.upper()] = {
                "beta": _safe_float(beta_block.get("beta")),
                "volatility": _safe_float(
                    vol_block.get("annualized_volatility") or vol_block.get("volatility")
                ),
            }
    return normalized


def _safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Repricing primitives
# ---------------------------------------------------------------------------
# Bloomberg-style defaults used when CDM fields are missing. These are
# deliberately conservative averages for an aggregate intermediate-duration
# bond portfolio (~5y duration, modest convexity).
DEFAULT_MODIFIED_DURATION = 5.0
DEFAULT_CONVEXITY = 40.0

# Default equity beta when performance.json lacks a reading for the symbol.
DEFAULT_BETA = 1.0
# Default per-symbol annualized volatility for Monte Carlo (reasonable mid-cap).
DEFAULT_ANNUALIZED_VOL = 0.25
# Default bond volatility — duration × yield-vol approximation.
DEFAULT_BOND_VOL = 0.06


def reprice_bond(holding: Dict[str, Any], rate_shock_bps: float, credit_shock_bps: float) -> float:
    """Return ΔP/P for a single bond under the given shocks.

    ΔP/P = -D·Δy + 0.5·C·Δy²

    Δy aggregates rate_shock and (for corporate bonds) credit_shock. Both
    inputs are in basis points and converted to decimal yield before the
    duration/convexity expansion is applied.
    """
    d = _safe_float(
        holding.get("modified_duration"),
        _safe_float(holding.get("macaulay_duration"), DEFAULT_MODIFIED_DURATION),
    )
    c = _safe_float(holding.get("convexity"), DEFAULT_CONVEXITY) or DEFAULT_CONVEXITY

    total_bps = float(rate_shock_bps)
    if holding.get("is_corporate"):
        total_bps += float(credit_shock_bps)
    dy = total_bps / 10_000.0  # bps -> decimal yield

    return -d * dy + 0.5 * c * dy * dy


def reprice_equity(
    holding: Dict[str, Any],
    perf_map: Dict[str, Dict[str, Any]],
    equity_mkt_shock: float,
    sector_shocks: Mapping[str, float],
) -> float:
    """Return Δr for a single equity using a 2-factor model.

    Δr = β · equity_mkt_shock + sector_shock     (idiosyncratic ε is
    sampled only inside the Monte Carlo loop; the point estimate here is
    the expected return under the scenario).
    """
    sym = holding.get("symbol", "").upper()
    beta = _safe_float((perf_map.get(sym) or {}).get("beta"), DEFAULT_BETA) or DEFAULT_BETA
    sector = holding.get("sector") or "Other"
    sector_shock = float(sector_shocks.get(sector, 0.0))
    return beta * float(equity_mkt_shock) + sector_shock


# ---------------------------------------------------------------------------
# Monte Carlo VaR
# ---------------------------------------------------------------------------


def monte_carlo_var(
    holdings: List[Dict[str, Any]],
    perf_map: Dict[str, Dict[str, Any]],
    scenario: Mapping[str, Any],
    total_value: float,
    n_sims: int,
    use_gpu: bool = False,
) -> Tuple[float, float]:
    """Return (var_95_loss_dollar, var_99_loss_dollar).

    Each holding's stressed return is drawn from
        N(expected_scenario_return, per_holding_sigma)
    and portfolio P&L is aggregated across `n_sims` paths.
    """
    if not holdings or total_value <= 0 or n_sims <= 0:
        return 0.0, 0.0

    xp = _cp if (use_gpu and _CUPY_AVAILABLE) else np

    n = len(holdings)
    weights = xp.array([h["market_value"] / total_value for h in holdings])
    mu = xp.zeros(n)
    sigma = xp.zeros(n)

    rate_bps = float(scenario.get("rate_shock_bps", 0.0))
    credit_bps = float(scenario.get("credit_shock_bps", 0.0))
    mkt = float(scenario.get("equity_mkt_shock", 0.0))
    sector_shocks = scenario.get("sector_shocks", {}) or {}

    for i, h in enumerate(holdings):
        asset_type = h["asset_type"]
        if asset_type == "bond":
            mu[i] = reprice_bond(h, rate_bps, credit_bps)
            sigma[i] = DEFAULT_BOND_VOL
        elif asset_type == "equity":
            mu[i] = reprice_equity(h, perf_map, mkt, sector_shocks)
            sym_vol = (perf_map.get(h["symbol"].upper()) or {}).get("volatility")
            sigma[i] = float(sym_vol) if sym_vol else DEFAULT_ANNUALIZED_VOL
        elif asset_type == "cash":
            mu[i] = 0.0
            sigma[i] = 0.0
        else:
            # Unknown instrument — shock with market factor conservatively.
            mu[i] = 0.5 * mkt
            sigma[i] = DEFAULT_ANNUALIZED_VOL

    # Monte Carlo: per-holding independent draws. This understates correlation
    # but is appropriate for a VaR envelope around the point estimate.
    # Shape: (n_sims, n_holdings)
    rng = xp.random.default_rng(seed=42)
    shocks = rng.normal(loc=mu, scale=sigma, size=(n_sims, n))
    portfolio_returns = shocks @ weights  # (n_sims,)
    portfolio_pnl = portfolio_returns * total_value

    # VaR_x is the loss magnitude at the (1-x)-quantile of P&L.
    if use_gpu and _CUPY_AVAILABLE:
        pnl_np = xp.asnumpy(portfolio_pnl)  # bring back to host
    else:
        pnl_np = np.asarray(portfolio_pnl)

    var_95 = float(-np.percentile(pnl_np, 5))
    var_99 = float(-np.percentile(pnl_np, 1))
    # Clamp negative VaR (scenario is net-positive) to 0.
    return max(var_95, 0.0), max(var_99, 0.0)


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------


def run_scenario(
    name: str,
    scenario: Mapping[str, Any],
    holdings: List[Dict[str, Any]],
    perf_map: Dict[str, Dict[str, Any]],
    total_value: float,
    n_sims: int,
    use_gpu: bool,
) -> Dict[str, Any]:
    """Apply a single scenario and return its impact dict."""
    rate_bps = float(scenario.get("rate_shock_bps", 0.0))
    credit_bps = float(scenario.get("credit_shock_bps", 0.0))
    mkt = float(scenario.get("equity_mkt_shock", 0.0))
    sector_shocks = scenario.get("sector_shocks", {}) or {}

    equity_pnl = 0.0
    bond_pnl = 0.0
    cash_value = 0.0
    equity_value = 0.0
    bond_value = 0.0

    for h in holdings:
        mv = float(h["market_value"])
        if h["asset_type"] == "bond":
            delta = reprice_bond(h, rate_bps, credit_bps)
            bond_pnl += mv * delta
            bond_value += mv
        elif h["asset_type"] == "equity":
            delta = reprice_equity(h, perf_map, mkt, sector_shocks)
            equity_pnl += mv * delta
            equity_value += mv
        elif h["asset_type"] == "cash":
            cash_value += mv
        else:
            # Treat "other" instruments with a muted market shock.
            equity_pnl += mv * (0.5 * mkt)
            equity_value += mv

    total_pnl = equity_pnl + bond_pnl
    new_value = total_value + total_pnl
    equity_impact = (equity_pnl / equity_value) if equity_value > 0 else 0.0
    bond_impact = (bond_pnl / bond_value) if bond_value > 0 else 0.0
    total_impact = (total_pnl / total_value) if total_value > 0 else 0.0
    drawdown_pct = abs(min(total_impact, 0.0))

    var_95, var_99 = monte_carlo_var(
        holdings,
        perf_map,
        scenario,
        total_value,
        n_sims=n_sims,
        use_gpu=use_gpu,
    )

    return {
        "name": name,
        "description": scenario.get("description", name),
        "rate_shock_bps": rate_bps,
        "credit_shock_bps": credit_bps,
        "equity_mkt_shock": mkt,
        "sector_shocks": dict(sector_shocks),
        "equity_impact": round(equity_impact, 6),
        "bond_impact": round(bond_impact, 6),
        "total_impact": round(total_impact, 6),
        "new_value": round(new_value, 2),
        "drawdown_pct": round(drawdown_pct, 6),
        "var_95": round(var_95, 2),
        "var_99": round(var_99, 2),
    }


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_flag(argv: List[str], flag: str) -> Optional[str]:
    """Destructively pop `--flag VALUE` from argv and return VALUE, or None."""
    if flag not in argv:
        return None
    idx = argv.index(flag)
    if idx + 1 >= len(argv):
        del argv[idx : idx + 1]
        return None
    value = argv[idx + 1]
    del argv[idx : idx + 2]
    return value


def parse_args(argv: List[str]) -> Dict[str, Any]:
    """Parse argv for scenario.py. Modifies argv in place."""
    # Extract flags first so they don't disturb positional ordering.
    scenarios_flag = _parse_flag(argv, "--scenarios")
    shocks_flag = _parse_flag(argv, "--shocks")
    sims_flag = _parse_flag(argv, "--sims")

    positional = [a for a in argv if not a.startswith("--")]
    if not positional:
        raise SystemExit(
            "Usage: scenario.py <holdings.json> [performance.json] "
            "[--scenarios LIST] [--shocks JSON] [--sims N] [--artifact PATH] "
            "[output.json]"
        )

    holdings_path = positional[0]

    # Distinguish performance.json vs output.json by suffix heuristic + arity.
    performance_path: Optional[str] = None
    output_path: Optional[str] = None

    if len(positional) == 2:
        second = positional[1]
        # Treat as performance if file exists; otherwise it's the output path.
        if Path(second).exists():
            performance_path = second
        else:
            output_path = second
    elif len(positional) >= 3:
        performance_path = positional[1]
        output_path = positional[2]

    scenarios: List[str] = []
    if scenarios_flag:
        scenarios = [s.strip() for s in scenarios_flag.split(",") if s.strip()]
    else:
        scenarios = [
            "rates_up_150bps",
            "rates_down_100bps",
            "credit_shock",
            "equity_crash_2008",
            "equity_crash_2020",
        ]

    custom_shocks: Optional[Dict[str, Any]] = None
    if shocks_flag:
        try:
            custom_shocks = json.loads(shocks_flag)
            if not isinstance(custom_shocks, dict):
                raise ValueError("--shocks JSON must be an object")
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Invalid --shocks JSON: {e}")
            raise SystemExit(1)

    try:
        n_sims = int(sims_flag) if sims_flag else (100_000 if _CUPY_AVAILABLE else 10_000)
    except ValueError:
        logger.warning(f"Invalid --sims value, falling back to default: {sims_flag}")
        n_sims = 10_000

    return {
        "holdings_path": holdings_path,
        "performance_path": performance_path,
        "output_path": output_path,
        "scenarios": scenarios,
        "custom_shocks": custom_shocks,
        "n_sims": n_sims,
    }


# ---------------------------------------------------------------------------
# HTML artifact (interactive sliders)
# ---------------------------------------------------------------------------


def build_scenario_artifact(
    result: Dict[str, Any],
    holdings_snapshot: Dict[str, Any],
    output_path: str,
    stonkmode: bool = False,
) -> str:
    """Render an interactive scenario-stress artifact.

    Uses ArtifactGenerator for layout + Plotly chart of scenario impacts,
    plus a raw HTML block with three range sliders that reprice live via
    vanilla JS using aggregated portfolio sensitivities.
    """
    from ic_engine.commands._artifact_helpers import _attach_narrative_and_terms
    from ic_engine.rendering.artifact_generator import PALETTE, ArtifactGenerator

    scenarios = result.get("scenarios", []) or []
    total_value = float(holdings_snapshot.get("total_value", 0.0))

    metadata = {
        "Total Value": f"${total_value:,.0f}",
        "Equity Value": f"${holdings_snapshot.get('equity_value', 0):,.0f}",
        "Bond Value": f"${holdings_snapshot.get('bond_value', 0):,.0f}",
        "Cash Value": f"${holdings_snapshot.get('cash_value', 0):,.0f}",
        "Scenarios Run": len(scenarios),
        "MC Sims": result.get("n_sims", 0),
    }
    artifact = ArtifactGenerator(
        title="Macro Stress Test — Scenario Analysis",
        disclaimer="EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        metadata=metadata,
    )

    # Impact bar chart — total impact per scenario
    if scenarios:
        names = [s.get("name", "?") for s in scenarios]
        totals = [float(s.get("total_impact", 0.0)) * 100 for s in scenarios]
        artifact.add_bar_chart(
            names,
            totals,
            "Total Portfolio Impact by Scenario (%)",
            x_label="Scenario",
            y_label="Impact (%)",
            col_class="col-6",
            color=PALETTE.get("neg", "#ef4444"),
        )

        # VaR 95/99 comparison bars
        [float(s.get("var_95", 0.0)) for s in scenarios]
        var99 = [float(s.get("var_99", 0.0)) for s in scenarios]
        artifact.add_bar_chart(
            names,
            var99,
            "VaR 99% by Scenario ($)",
            x_label="Scenario",
            y_label="VaR 99 ($)",
            col_class="col-6",
            color=PALETTE.get("bond", "#fb923c"),
        )
        # Scenario summary table
        rows = []
        for s in scenarios:
            rows.append(
                {
                    "Scenario": s.get("name", ""),
                    "Description": s.get("description", ""),
                    "Equity Δ": f"{float(s.get('equity_impact', 0)) * 100:+.2f}%",
                    "Bond Δ": f"{float(s.get('bond_impact', 0)) * 100:+.2f}%",
                    "Total Δ": f"{float(s.get('total_impact', 0)) * 100:+.2f}%",
                    "New Value": f"${float(s.get('new_value', 0)):,.0f}",
                    "Drawdown": f"{float(s.get('drawdown_pct', 0)) * 100:.2f}%",
                    "VaR 95": f"${float(s.get('var_95', 0)):,.0f}",
                    "VaR 99": f"${float(s.get('var_99', 0)):,.0f}",
                }
            )
        artifact.add_table(
            rows,
            "Scenario Impacts",
            columns=[
                "Scenario",
                "Description",
                "Equity Δ",
                "Bond Δ",
                "Total Δ",
                "New Value",
                "Drawdown",
                "VaR 95",
                "VaR 99",
            ],
        )

    # Interactive slider block — live JS repricing
    sliders_html = _build_slider_block(holdings_snapshot)
    artifact.add_raw_block(sliders_html, title="Live Scenario Playground", col_class="col-12")

    # Stonkmode narrative hook (optional)
    summary_lines = [f"Portfolio: ${total_value:,.0f}"]
    for s in scenarios[:5]:
        summary_lines.append(
            f"  {s.get('name')}: total {float(s.get('total_impact', 0)) * 100:+.2f}%, "
            f"VaR99 ${float(s.get('var_99', 0)):,.0f}"
        )
    data_summary = "\n".join(summary_lines)
    text_for_terms = (
        data_summary + " Duration Convexity Beta Value at Risk Drawdown Spread Yield Curve"
    )
    _attach_narrative_and_terms(
        artifact,
        "scenario",
        data_summary,
        text_for_terms,
        stonkmode,
    )

    return str(artifact.save(output_path))


def _build_slider_block(snapshot: Dict[str, Any]) -> str:
    """Return a self-contained HTML fragment implementing 3 range sliders.

    The JS uses aggregate portfolio sensitivities (weighted beta, weighted
    duration, weighted convexity, weighted corporate exposure) to reprice
    the entire book instantly as sliders are dragged. No network calls.
    """
    js_payload = {
        "totalValue": float(snapshot.get("total_value", 0.0)),
        "equityValue": float(snapshot.get("equity_value", 0.0)),
        "bondValue": float(snapshot.get("bond_value", 0.0)),
        "cashValue": float(snapshot.get("cash_value", 0.0)),
        "corpBondValue": float(snapshot.get("corp_bond_value", 0.0)),
        "weightedBeta": float(snapshot.get("weighted_beta", DEFAULT_BETA)),
        "weightedDuration": float(snapshot.get("weighted_duration", DEFAULT_MODIFIED_DURATION)),
        "weightedConvexity": float(snapshot.get("weighted_convexity", DEFAULT_CONVEXITY)),
    }
    payload_json = json.dumps(js_payload)

    # NOTE: all curly braces in the CSS/JS below are doubled so .format()
    # leaves them alone; we splice the JSON via str.replace to avoid any
    # brace/format-spec interactions.
    html = """
<style>
  .scenario-sliders {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 24px;
    padding: 16px 8px;
  }}
  .scenario-sliders .slider-card {{
    background: rgba(255,255,255,0.02);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px;
    padding: 16px;
  }}
  .scenario-sliders label {{
    display: block;
    font-weight: 600;
    margin-bottom: 6px;
  }}
  .scenario-sliders input[type=range] {{
    width: 100%;
  }}
  .scenario-sliders .readout {{
    margin-top: 6px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 14px;
    color: #9ca3af;
  }}
  .scenario-summary {{
    margin-top: 18px;
    padding: 14px;
    border-radius: 8px;
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(37,99,235,0.08);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 14px;
    line-height: 1.7;
  }}
  .scenario-summary strong {{ color: #f3f4f6; }}
  .scenario-summary .pnl-pos {{ color: #22c55e; }}
  .scenario-summary .pnl-neg {{ color: #ef4444; }}
</style>
<div class="scenario-sliders">
  <div class="slider-card">
    <label for="rateSlider">Δ Rates (bps)</label>
    <input type="range" id="rateSlider" min="-200" max="200" step="5" value="0"/>
    <div class="readout">Shift: <span id="rateVal">0</span> bps</div>
  </div>
  <div class="slider-card">
    <label for="spreadSlider">Δ Credit Spreads (bps)</label>
    <input type="range" id="spreadSlider" min="-100" max="200" step="5" value="0"/>
    <div class="readout">Spread: <span id="spreadVal">0</span> bps</div>
  </div>
  <div class="slider-card">
    <label for="equitySlider">Δ Equity Market (%)</label>
    <input type="range" id="equitySlider" min="-30" max="20" step="1" value="0"/>
    <div class="readout">Market: <span id="equityVal">0</span>%</div>
  </div>
</div>
<div class="scenario-summary" id="scenarioSummary">
  Drag sliders to reprice live.
</div>
<script>
(function() {{
  var P = __PAYLOAD__;
  function fmtCur(x) {{
    var sign = x < 0 ? "-" : "";
    return sign + "$" + Math.abs(x).toLocaleString(undefined, {{maximumFractionDigits: 0}});
  }}
  function pctStr(x) {{
    return (x >= 0 ? "+" : "") + (x * 100).toFixed(2) + "%";
  }}
  function recompute() {{
    var rateBps   = parseFloat(document.getElementById("rateSlider").value);
    var spreadBps = parseFloat(document.getElementById("spreadSlider").value);
    var eqPct     = parseFloat(document.getElementById("equitySlider").value) / 100.0;

    document.getElementById("rateVal").textContent   = rateBps.toFixed(0);
    document.getElementById("spreadVal").textContent = spreadBps.toFixed(0);
    document.getElementById("equityVal").textContent = (eqPct * 100).toFixed(0);

    // Bond repricing: rates hit all bonds; credit only hits corp slice.
    var dyRates   = rateBps / 10000.0;
    var dySpread  = spreadBps / 10000.0;
    var D = P.weightedDuration;
    var C = P.weightedConvexity;

    // Rates component applied to entire bond book
    var bondDeltaRates = (-D * dyRates) + 0.5 * C * dyRates * dyRates;
    // Credit component applied only to corporate bond value
    var bondDeltaCredit = (-D * dySpread) + 0.5 * C * dySpread * dySpread;
    var bondPnl = P.bondValue * bondDeltaRates
                + P.corpBondValue * bondDeltaCredit;

    // Equity: market shock times weighted beta (sector shocks omitted in
    // live mode — they come from the scenarios table).
    var eqDelta = P.weightedBeta * eqPct;
    var eqPnl = P.equityValue * eqDelta;

    var totalPnl = bondPnl + eqPnl;
    var newValue = P.totalValue + totalPnl;
    var totalPct = P.totalValue > 0 ? totalPnl / P.totalValue : 0.0;

    var klass = totalPnl < 0 ? "pnl-neg" : "pnl-pos";
    document.getElementById("scenarioSummary").innerHTML =
        "<strong>Portfolio:</strong> " + fmtCur(P.totalValue) +
        " &rarr; <strong>" + fmtCur(newValue) + "</strong><br>" +
        "Bond P&amp;L: <span class=\\"" + (bondPnl < 0 ? "pnl-neg" : "pnl-pos") + "\\">" +
            fmtCur(bondPnl) + "</span><br>" +
        "Equity P&amp;L: <span class=\\"" + (eqPnl < 0 ? "pnl-neg" : "pnl-pos") + "\\">" +
            fmtCur(eqPnl) + "</span><br>" +
        "Total P&amp;L: <span class=\\"" + klass + "\\">" +
            fmtCur(totalPnl) + " (" + pctStr(totalPct) + ")</span>";
  }}
  ["rateSlider", "spreadSlider", "equitySlider"].forEach(function(id) {{
    document.getElementById(id).addEventListener("input", recompute);
  }});
  recompute();
}})();
</script>
"""
    return html.replace("__PAYLOAD__", payload_json)


def _build_holdings_snapshot(
    holdings: List[Dict[str, Any]], perf_map: Dict[str, Dict[str, Any]], total_value: float
) -> Dict[str, Any]:
    """Compute aggregate sensitivities powering the interactive slider."""
    equity_value = 0.0
    bond_value = 0.0
    corp_bond_value = 0.0
    cash_value = 0.0

    weighted_beta_num = 0.0
    weighted_duration_num = 0.0
    weighted_convexity_num = 0.0

    for h in holdings:
        mv = float(h["market_value"])
        at = h["asset_type"]
        if at == "equity":
            equity_value += mv
            beta = (
                _safe_float(
                    (perf_map.get(h["symbol"].upper()) or {}).get("beta"),
                    DEFAULT_BETA,
                )
                or DEFAULT_BETA
            )
            weighted_beta_num += mv * beta
        elif at == "bond":
            bond_value += mv
            if h.get("is_corporate"):
                corp_bond_value += mv
            d = (
                _safe_float(
                    h.get("modified_duration") or h.get("macaulay_duration"),
                    DEFAULT_MODIFIED_DURATION,
                )
                or DEFAULT_MODIFIED_DURATION
            )
            c = _safe_float(h.get("convexity"), DEFAULT_CONVEXITY) or DEFAULT_CONVEXITY
            weighted_duration_num += mv * d
            weighted_convexity_num += mv * c
        elif at == "cash":
            cash_value += mv
        else:
            # count as equity-ish for slider purposes
            equity_value += mv
            weighted_beta_num += mv * DEFAULT_BETA

    weighted_beta = (weighted_beta_num / equity_value) if equity_value > 0 else DEFAULT_BETA
    weighted_duration = (
        (weighted_duration_num / bond_value) if bond_value > 0 else DEFAULT_MODIFIED_DURATION
    )
    weighted_convexity = (
        (weighted_convexity_num / bond_value) if bond_value > 0 else DEFAULT_CONVEXITY
    )

    return {
        "total_value": total_value,
        "equity_value": equity_value,
        "bond_value": bond_value,
        "corp_bond_value": corp_bond_value,
        "cash_value": cash_value,
        "weighted_beta": weighted_beta,
        "weighted_duration": weighted_duration,
        "weighted_convexity": weighted_convexity,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    # Pull --artifact / --stonkmode first so positional parsing isn't confused.
    from ic_engine.commands._artifact_helpers import pop_artifact_flags

    argv = list(sys.argv[1:])
    artifact_path, stonkmode = pop_artifact_flags(argv)

    try:
        opts = parse_args(argv)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
        return 2

    holdings_path = opts["holdings_path"]
    if not Path(holdings_path).exists():
        print(json.dumps({"error": f"Holdings file not found: {holdings_path}"}))
        return 1

    try:
        holdings, total_value = load_holdings(holdings_path)
    except Exception as e:
        print(json.dumps({"error": f"Failed to load holdings: {e}"}))
        return 1

    perf_map = load_performance(opts["performance_path"])

    # Resolve requested scenario list
    scenarios_to_run: List[Tuple[str, Dict[str, Any]]] = []
    for name in opts["scenarios"]:
        if name == "custom":
            if not opts["custom_shocks"]:
                logger.warning(
                    "Requested 'custom' scenario but no --shocks JSON supplied; skipping"
                )
                continue
            scenarios_to_run.append(("custom", opts["custom_shocks"]))
        elif name in BUILTIN_SCENARIOS:
            scenarios_to_run.append((name, BUILTIN_SCENARIOS[name]))
        else:
            logger.warning(f"Unknown scenario '{name}' — skipping")

    if not scenarios_to_run and opts["custom_shocks"]:
        scenarios_to_run.append(("custom", opts["custom_shocks"]))

    if not scenarios_to_run:
        print(json.dumps({"error": "No valid scenarios requested"}))
        return 1

    use_gpu = _CUPY_AVAILABLE and opts["n_sims"] >= 100_000
    logger.info(
        f"Running {len(scenarios_to_run)} scenario(s); "
        f"Monte Carlo sims={opts['n_sims']} (GPU={use_gpu})"
    )

    scenario_results: List[Dict[str, Any]] = []
    for name, scen in scenarios_to_run:
        try:
            scenario_results.append(
                run_scenario(
                    name,
                    scen,
                    holdings,
                    perf_map,
                    total_value,
                    opts["n_sims"],
                    use_gpu,
                )
            )
        except Exception as e:
            logger.error(f"Scenario '{name}' failed: {e}")
            scenario_results.append({"name": name, "error": str(e)})

    snapshot = _build_holdings_snapshot(holdings, perf_map, total_value)

    result = {
        "scenarios": scenario_results,
        "portfolio_value": round(total_value, 2),
        "holdings_analyzed": len(holdings),
        "n_sims": opts["n_sims"],
        "gpu_accelerated": use_gpu,
        "snapshot": snapshot,
    }

    # Persist full JSON via DisclaimerWrapper when an output path is given,
    # otherwise emit compact JSON to stdout (router-friendly).
    output_path = opts["output_path"]
    if output_path:
        try:
            DisclaimerWrapper.wrap_and_save(
                result,
                output_path,
                analysis_type="Macro Stress Tests",
            )
            logger.info(f"Scenario analysis written to {output_path}")
        except Exception as e:
            logger.warning(f"Could not write {output_path}: {e}")

    compact = {
        "_note": "Compact scenario summary for LLM.",
        "disclaimer": "EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        "portfolio_value": round(total_value, 2),
        "holdings_analyzed": len(holdings),
        "n_sims": opts["n_sims"],
        "scenarios": [
            {
                "name": s.get("name"),
                "equity_impact": s.get("equity_impact"),
                "bond_impact": s.get("bond_impact"),
                "total_impact": s.get("total_impact"),
                "new_value": s.get("new_value"),
                "drawdown_pct": s.get("drawdown_pct"),
                "var_95": s.get("var_95"),
                "var_99": s.get("var_99"),
            }
            for s in scenario_results
            if "error" not in s
        ],
    }
    if output_path:
        compact["output_file"] = str(output_path)

    # Print human-readable summary to stderr
    print(f"\n{'=' * 70}", file=sys.stderr)
    print("💡 Analysis complete. Review the detailed JSON output above.", file=sys.stderr)
    print("   → Bring these findings to your financial advisor.", file=sys.stderr)
    print(f"{'=' * 70}\n", file=sys.stderr)

    print(json.dumps(compact, separators=(",", ":"), default=str))

    if artifact_path:
        try:
            out = build_scenario_artifact(
                result,
                snapshot,
                artifact_path,
                stonkmode=stonkmode,
            )
            print(f"Artifact: {out}")
        except Exception as e:
            logger.warning(f"Artifact generation failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
