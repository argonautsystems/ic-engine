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
Attribution Timeline (Phase 2.1 of CDM 5 analytics) — InvestorClaw

Explains why portfolio value/risk changed over a lookback window. Decomposes
per-symbol contribution into factor buckets (market/rate, sector, earnings,
dividend, FX, other) and rolls up to a portfolio-level summary. Emits
timeline buckets (1d, 5d, 1mo) when prior snapshots are available.

Algorithm
---------
For each equity holding with a valid beta and recent return:

    rate_contribution     = beta * (today_return - prior_return)
    sector_momentum       = sector-average return (excl. self)
    earnings_surprise     = EPS beat/miss flag scaled to the return series
    dividend_impact       = (annual_dividend / price) pro-rated over window
    fx_impact             = currency delta for non-USD holdings
    other                 = total_return - sum(above)
    total_contribution    = weight * total_return

Portfolio attribution = weight-sum of per-symbol contributions.

Argv
----
    whatchanged.py <holdings.json> <performance.json>
                   [--days 7]
                   [--prior-dir PATH]
                   [--artifact PATH] [--stonkmode]
                   [output.json]
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── Path bootstrap ─────────────────────────────────────────────────────────
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from ic_engine.config.schema import normalize_portfolio, validate_portfolio  # noqa: E402
from ic_engine.rendering.disclaimer_wrapper import DisclaimerWrapper  # noqa: E402

# Optional dependencies guarded with flags
try:
    import numpy as np  # noqa: F401

    _DEP_NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _DEP_NUMPY_AVAILABLE = False

try:
    import yfinance as yf  # noqa: F401

    _DEP_YFINANCE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _DEP_YFINANCE_AVAILABLE = False

try:
    from commands.analyze_performance_polars import PerformanceAnalyzer  # noqa: F401

    _DEP_ANALYZER_AVAILABLE = True
except ImportError:  # pragma: no cover
    _DEP_ANALYZER_AVAILABLE = False


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ─── Constants ──────────────────────────────────────────────────────────────

TIMELINE_BUCKETS: Tuple[Tuple[str, int], ...] = (
    ("1d", 1),
    ("5d", 5),
    ("1mo", 21),
)

FACTOR_KEYS: Tuple[str, ...] = (
    "market",
    "sector",
    "earnings",
    "dividend",
    "fx",
    "other",
)


# ─── Small helpers ──────────────────────────────────────────────────────────


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _get_portfolio(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return normalized portfolio dict (with equity/bond/... keys)."""
    norm = normalize_portfolio(raw)
    validate_portfolio(norm)
    return norm["portfolio"]


def _equity_positions(
    portfolio: Dict[str, Any],
) -> List[Tuple[str, Dict[str, Any]]]:
    """Return (symbol, entry) list of equity positions with market_value > 0."""
    equity = portfolio.get("equity", {}) or {}
    rows: List[Tuple[str, Dict[str, Any]]] = []
    for sym, entry in equity.items():
        if not isinstance(entry, dict):
            continue
        mv = _safe_float(entry.get("market_value"))
        if mv > 0:
            rows.append((sym, entry))
    return rows


def _weights_from_positions(
    rows: List[Tuple[str, Dict[str, Any]]],
) -> Dict[str, float]:
    total = sum(_safe_float(e.get("market_value")) for _, e in rows)
    if total <= 0:
        return {}
    return {sym: _safe_float(e.get("market_value")) / total for sym, e in rows}


def _unwrap(data: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap DisclaimerWrapper envelopes (`{"data": {...}}`)."""
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        return data["data"]
    return data


def _load_json(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.exists():
        logger.warning("File not found: %s", p)
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to parse %s: %s", p, e)
        return None


def _load_prior_snapshot(
    prior_dir: Optional[str],
    holdings_file: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Load prior holdings + performance from prior_dir.

    Falls back to sibling directory of holdings_file when prior_dir is None.
    Returns (prior_holdings_dict, prior_performance_dict) — either may be None.
    """
    candidates: List[Path] = []
    if prior_dir:
        candidates.append(Path(prior_dir).expanduser())
    # Fallback: sibling of current holdings_file
    candidates.append(Path(holdings_file).expanduser().resolve().parent)

    prior_holdings: Optional[Dict[str, Any]] = None
    prior_performance: Optional[Dict[str, Any]] = None
    for cand in candidates:
        if not cand.exists() or not cand.is_dir():
            continue
        if prior_holdings is None:
            for name in ("holdings_prior.json", "prior_holdings.json", "holdings.json"):
                p = cand / name
                if p.exists():
                    prior_holdings = _load_json(str(p))
                    if prior_holdings is not None:
                        logger.info("Prior holdings loaded: %s", p)
                        break
        if prior_performance is None:
            for name in ("performance_prior.json", "prior_performance.json", "performance.json"):
                p = cand / name
                if p.exists():
                    prior_performance = _load_json(str(p))
                    if prior_performance is not None:
                        logger.info("Prior performance loaded: %s", p)
                        break
        if prior_holdings is not None and prior_performance is not None:
            break
    return prior_holdings, prior_performance


# ─── Performance field extraction ───────────────────────────────────────────


def _perf_per_symbol(performance: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return dict of symbol → per-symbol metrics block.

    Handles both the raw `analyze_performance_polars` output and the
    compact summary variant.
    """
    d = _unwrap(performance)
    # Raw schema: {"performance": {"AAPL": {...}}}
    if isinstance(d.get("performance"), dict):
        return d["performance"]
    # Compact schema: list with "top_performers" entries
    return {}


def _extract_beta(metrics: Dict[str, Any]) -> float:
    beta_block = metrics.get("beta", {}) or {}
    if isinstance(beta_block, dict):
        return _safe_float(beta_block.get("beta"))
    return _safe_float(beta_block)


def _extract_total_return(metrics: Dict[str, Any]) -> float:
    """Extract a period-level total return (decimal, e.g. 0.0523) from metrics."""
    # Preferred: returns.total_return_pct (pct form)
    ret_block = metrics.get("returns", {}) or {}
    if isinstance(ret_block, dict):
        v = ret_block.get("total_return_pct")
        if v is not None:
            return _safe_float(v) / 100.0
        v = ret_block.get("total_return")
        if v is not None:
            return _safe_float(v)
    # Fallback: sharpe_ratio.annual_return (decimal)
    sharpe_block = metrics.get("sharpe_ratio", {}) or {}
    if isinstance(sharpe_block, dict):
        v = sharpe_block.get("annual_return")
        if v is not None:
            return _safe_float(v)
    return 0.0


def _extract_sector(holding: Dict[str, Any]) -> str:
    return (holding.get("sector") or "Unknown") or "Unknown"


def _extract_currency(holding: Dict[str, Any]) -> str:
    cur = holding.get("currency") or holding.get("reportingCurrency") or "USD"
    return str(cur).upper()


# ─── Factor decomposition ───────────────────────────────────────────────────


def decompose_symbol(
    symbol: str,
    holding: Dict[str, Any],
    today_metrics: Dict[str, Any],
    prior_metrics: Optional[Dict[str, Any]],
    sector_avg_return: float,
    days: int,
) -> Dict[str, float]:
    """Return per-symbol factor attribution (all values expressed as decimals).

    Returns a dict with keys: market, sector, earnings, dividend, fx, other,
    and total_return (the observed/assumed period return).
    """
    today_ret = _extract_total_return(today_metrics)
    prior_ret = _extract_total_return(prior_metrics) if prior_metrics else 0.0
    beta = _extract_beta(today_metrics)

    # 1. Market (rate) contribution: beta × Δreturn
    market = beta * (today_ret - prior_ret)

    # 2. Sector momentum: sector-average return (excl. self component)
    sector = sector_avg_return

    # 3. Earnings surprise: scale EPS beat/miss flag by window weight
    eps_flag = holding.get("earnings_surprise") or holding.get("eps_surprise")
    if eps_flag in ("beat", True, 1):
        earnings = 0.02 * (days / 21.0)  # +2% annualized scaled
    elif eps_flag in ("miss", False, -1):
        earnings = -0.02 * (days / 21.0)
    else:
        earnings = 0.0

    # 4. Dividend impact: annual dividend yield pro-rated over window
    price = _safe_float(holding.get("current_price") or holding.get("price"))
    annual_div = _safe_float(holding.get("annual_dividend") or holding.get("dividend_rate"))
    if price > 0 and annual_div > 0:
        dividend = (annual_div / price) * (days / 365.0)
    else:
        dividend = 0.0

    # 5. FX impact: only for non-USD holdings
    currency = _extract_currency(holding)
    fx_delta = _safe_float(holding.get("fx_delta"))
    fx = fx_delta if currency != "USD" else 0.0

    # 6. Other residual
    explained = market + sector + earnings + dividend + fx
    other = today_ret - explained

    return {
        "market": float(market),
        "sector": float(sector),
        "earnings": float(earnings),
        "dividend": float(dividend),
        "fx": float(fx),
        "other": float(other),
        "total_return": float(today_ret),
    }


def _dominant_driver(attribution: Dict[str, float]) -> str:
    """Return the factor key with largest absolute contribution."""
    candidates = {k: attribution.get(k, 0.0) for k in FACTOR_KEYS}
    if not candidates:
        return "other"
    return max(candidates.items(), key=lambda kv: abs(kv[1]))[0]


# ─── Sector helpers ─────────────────────────────────────────────────────────


def _sector_average_returns(
    rows: List[Tuple[str, Dict[str, Any]]],
    per_symbol_perf: Dict[str, Dict[str, Any]],
) -> Dict[str, float]:
    """Average total_return per sector across the given positions."""
    buckets: Dict[str, List[float]] = {}
    for sym, entry in rows:
        sector = _extract_sector(entry)
        metrics = per_symbol_perf.get(sym, {})
        if not metrics:
            continue
        ret = _extract_total_return(metrics)
        buckets.setdefault(sector, []).append(ret)
    return {sec: (sum(vals) / len(vals)) if vals else 0.0 for sec, vals in buckets.items()}


# ─── Core attribution engine ────────────────────────────────────────────────


def run_attribution(
    holdings_file: str,
    performance_file: str,
    days: int = 7,
    prior_dir: Optional[str] = None,
    output_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute attribution timeline and optionally write wrapped JSON.

    Returns the unwrapped result dict (also what the caller sees before
    disclaimers are applied).
    """
    raw = _load_json(holdings_file)
    if raw is None:
        raise FileNotFoundError(f"Holdings file not found: {holdings_file}")
    perf_today = _load_json(performance_file)
    if perf_today is None:
        raise FileNotFoundError(f"Performance file not found: {performance_file}")

    portfolio = _get_portfolio(raw)
    rows = _equity_positions(portfolio)
    if not rows:
        logger.warning("No equity positions found; attribution skipped.")
        return {
            "as_of": datetime.now().isoformat(timespec="seconds"),
            "days": days,
            "error": "no_equity_positions",
        }

    weights = _weights_from_positions(rows)
    perf_today_map = _perf_per_symbol(perf_today)

    # Prior snapshot (best-effort)
    prior_holdings_raw, prior_performance = _load_prior_snapshot(prior_dir, holdings_file)
    prior_perf_map: Dict[str, Dict[str, Any]] = (
        _perf_per_symbol(prior_performance) if prior_performance else {}
    )

    # Sector averages for today's returns
    sector_avg = _sector_average_returns(rows, perf_today_map)

    # Per-symbol attribution — at the primary requested window
    per_symbol: Dict[str, Dict[str, float]] = {}
    contributions: List[Dict[str, Any]] = []
    for sym, entry in rows:
        today_metrics = perf_today_map.get(sym, {})
        if not today_metrics:
            # No metric data for this symbol — skip but keep weight
            continue
        prior_metrics = prior_perf_map.get(sym)
        sector_ret = sector_avg.get(_extract_sector(entry), 0.0)
        attr = decompose_symbol(sym, entry, today_metrics, prior_metrics, sector_ret, days)
        per_symbol[sym] = attr

        w = weights.get(sym, 0.0)
        contribution = w * attr["total_return"]
        driver = _dominant_driver(attr)
        contributions.append(
            {
                "symbol": sym,
                "weight": round(w, 6),
                "return": round(attr["total_return"], 6),
                "contribution": round(contribution, 6),
                "driver": driver,
                "attribution": {k: round(attr[k], 6) for k in FACTOR_KEYS},
            }
        )

    # Portfolio roll-up: weight-sum per-factor
    factor_breakdown: Dict[str, float] = {k: 0.0 for k in FACTOR_KEYS}
    total_return = 0.0
    for c in contributions:
        w = c["weight"]
        total_return += c["contribution"]
        for k in FACTOR_KEYS:
            factor_breakdown[k] += w * c["attribution"][k]

    # Top movers by absolute contribution
    top_movers = sorted(contributions, key=lambda c: abs(c["contribution"]), reverse=True)[:10]

    # Timeline buckets — scale the primary window attribution to 1d/5d/1mo.
    # When the primary window equals a bucket, the bucket mirrors that result;
    # otherwise we pro-rate by days as a simple linear approximation. This is
    # a best-effort view given a single snapshot; a full implementation would
    # replay attribution against historical return series.
    timeline: Dict[str, Dict[str, Any]] = {}
    for label, bucket_days in TIMELINE_BUCKETS:
        scale = (bucket_days / days) if days > 0 else 1.0
        timeline[label] = {
            "days": bucket_days,
            "total_return": round(total_return * scale, 6),
            "factor_breakdown": {k: round(v * scale, 6) for k, v in factor_breakdown.items()},
        }

    result: Dict[str, Any] = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "window_days": days,
        "holdings_analyzed": len(contributions),
        "prior_snapshot_used": prior_performance is not None,
        "attribution_summary": {
            "total_return": round(total_return, 6),
            "factor_breakdown": {k: round(v, 6) for k, v in factor_breakdown.items()},
        },
        "top_movers": [
            {
                "symbol": m["symbol"],
                "contribution": m["contribution"],
                "driver": m["driver"],
            }
            for m in top_movers
        ],
        "timeline": timeline,
        "per_symbol": per_symbol,
    }

    if output_file:
        DisclaimerWrapper.wrap_and_save(result, output_file, "Attribution Timeline")
        logger.info("Attribution timeline saved to %s", output_file)

    return result


# ─── Compact summary + CLI ──────────────────────────────────────────────────


def _build_compact_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """Compact stdout payload (~1–2 KB)."""
    summary = result.get("attribution_summary", {}) or {}
    compact = {
        "as_of": result.get("as_of"),
        "window_days": result.get("window_days"),
        "holdings_analyzed": result.get("holdings_analyzed"),
        "prior_snapshot_used": result.get("prior_snapshot_used"),
        "attribution_summary": summary,
        "top_movers": result.get("top_movers", [])[:5],
        "timeline": result.get("timeline", {}),
    }
    if result.get("error"):
        compact["error"] = result["error"]
    return compact


def _print_3line_summary(result: Dict[str, Any]) -> None:
    """Emit a 3-line human-readable summary to stdout (before the JSON)."""
    summary = result.get("attribution_summary", {}) or {}
    total = _safe_float(summary.get("total_return")) * 100
    breakdown = summary.get("factor_breakdown", {}) or {}

    # Line 1 — window + total
    line1 = (
        f"Attribution over {result.get('window_days', 0)}d: "
        f"{total:+.2f}% total return "
        f"({result.get('holdings_analyzed', 0)} holdings)"
    )

    # Line 2 — top factor contributions
    factors_sorted = sorted(
        breakdown.items(),
        key=lambda kv: abs(_safe_float(kv[1])),
        reverse=True,
    )
    top_factors = (
        ", ".join(f"{k}: {_safe_float(v) * 100:+.2f}%" for k, v in factors_sorted[:3])
        or "no factor data"
    )
    line2 = f"Top factors — {top_factors}"

    # Line 3 — leading mover
    movers = result.get("top_movers", []) or []
    if movers:
        lead = movers[0]
        line3 = (
            f"Leading mover — {lead.get('symbol', '?')}: "
            f"{_safe_float(lead.get('contribution')) * 100:+.2f}% contribution "
            f"(driver: {lead.get('driver', 'other')})"
        )
    else:
        line3 = "Leading mover — none"

    print(line1, file=sys.stderr)
    print(line2, file=sys.stderr)
    print(line3, file=sys.stderr)


def _parse_argv(argv: List[str]):
    """Parse:
    <holdings.json> <performance.json> [--days N] [--prior-dir PATH] [output.json]
    """
    days = 7
    prior_dir: Optional[str] = None
    positional: List[str] = []

    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--days" and i + 1 < len(argv):
            try:
                days = int(argv[i + 1])
            except ValueError:
                raise SystemExit(f"Invalid --days value: {argv[i + 1]!r}")
            i += 2
            continue
        if tok == "--prior-dir" and i + 1 < len(argv):
            prior_dir = argv[i + 1]
            i += 2
            continue
        positional.append(tok)
        i += 1

    if len(positional) < 2:
        raise SystemExit(
            "Usage: whatchanged.py <holdings.json> <performance.json> "
            "[--days N] [--prior-dir PATH] [output.json]"
        )

    holdings_file = positional[0]
    performance_file = positional[1]
    output_file = positional[2] if len(positional) > 2 else None
    return holdings_file, performance_file, days, prior_dir, output_file


if __name__ == "__main__":
    # Artifact flags must be popped before positional parsing
    from commands._artifact_helpers import pop_artifact_flags  # noqa: E402

    _argv = list(sys.argv)
    _artifact_path, _stonkmode = pop_artifact_flags(_argv)
    sys.argv = _argv

    (
        _holdings_file,
        _performance_file,
        _days,
        _prior_dir,
        _output_file,
    ) = _parse_argv(sys.argv)

    # Default output to sibling of holdings_file if none provided
    if _output_file is None:
        _output_file = str(Path(_holdings_file).expanduser().resolve().parent / "whatchanged.json")

    _result = run_attribution(
        holdings_file=_holdings_file,
        performance_file=_performance_file,
        days=_days,
        prior_dir=_prior_dir,
        output_file=_output_file,
    )

    # 3-line summary to stderr so stdout JSON stays machine-parseable
    _print_3line_summary(_result)

    _compact = _build_compact_summary(_result)
    print(json.dumps(_compact, separators=(",", ":")))

    if _output_file:
        logger.info("Full attribution data saved to: %s", _output_file)

    # Optional HTML artifact
    if _artifact_path:
        try:
            from commands._artifact_helpers import (  # noqa: E402
                _attach_narrative_and_terms,
            )
            from rendering.artifact_generator import (  # noqa: E402
                PALETTE,
                ArtifactGenerator,
            )

            summary = _result.get("attribution_summary", {}) or {}
            total_return = _safe_float(summary.get("total_return")) * 100
            breakdown = summary.get("factor_breakdown", {}) or {}

            metadata = {
                "Window": f"{_result.get('window_days', 0)}d",
                "Total Return": f"{total_return:+.2f}%",
                "Holdings": _result.get("holdings_analyzed", 0),
                "Prior Snapshot": ("yes" if _result.get("prior_snapshot_used") else "no"),
            }
            artifact = ArtifactGenerator(
                title="Attribution Timeline",
                disclaimer="EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
                metadata=metadata,
            )

            # Factor breakdown bar chart (percent)
            if breakdown:
                labels = list(breakdown.keys())
                values = [_safe_float(breakdown[k]) * 100 for k in labels]
                artifact.add_bar_chart(
                    labels,
                    values,
                    "Factor Breakdown (% contribution)",
                    x_label="Factor",
                    y_label="Contribution (%)",
                    col_class="col-6",
                    color=PALETTE.get("accent", "#3b82f6"),
                )

            # Timeline totals line chart
            tl = _result.get("timeline", {}) or {}
            if tl:
                tl_labels = list(tl.keys())
                tl_values = [_safe_float(tl[k].get("total_return")) * 100 for k in tl_labels]
                artifact.add_bar_chart(
                    tl_labels,
                    tl_values,
                    "Timeline (% return)",
                    x_label="Bucket",
                    y_label="Return (%)",
                    col_class="col-6",
                    color=PALETTE.get("pos", "#16a34a"),
                )

            # Top movers table
            movers = _result.get("top_movers", []) or []
            if movers:
                rows = [
                    {
                        "Symbol": m.get("symbol", ""),
                        "Contribution": f"{_safe_float(m.get('contribution')) * 100:+.3f}%",
                        "Driver": m.get("driver", ""),
                    }
                    for m in movers
                ]
                artifact.add_table(
                    rows,
                    "Top Movers",
                    columns=["Symbol", "Contribution", "Driver"],
                )

            # Summary narration input
            summary_lines = [
                f"Window: {_result.get('window_days', 0)} days",
                f"Total return: {total_return:+.2f}%",
                f"Holdings analyzed: {_result.get('holdings_analyzed', 0)}",
                "",
                "Factor contributions:",
            ]
            for k, v in breakdown.items():
                summary_lines.append(f"  {k}: {_safe_float(v) * 100:+.3f}%")
            if movers:
                summary_lines.append("")
                summary_lines.append("Top movers:")
                for m in movers[:5]:
                    summary_lines.append(
                        f"  {m.get('symbol', '?')}: "
                        f"{_safe_float(m.get('contribution')) * 100:+.3f}% "
                        f"({m.get('driver', '?')})"
                    )
            data_summary = "\n".join(summary_lines)
            text_for_terms = (
                data_summary + " Beta Alpha Sector Momentum Earnings Surprise Attribution"
            )

            _attach_narrative_and_terms(
                artifact,
                "whatchanged",
                data_summary,
                text_for_terms,
                _stonkmode,
            )
            _out = str(artifact.save(_artifact_path))
            print(f"Artifact: {_out}")
        except Exception as _e:  # pragma: no cover
            logger.warning("Artifact generation failed: %s", _e)
