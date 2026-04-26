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
commands/_artifact_helpers.py — Per-command artifact builders.

Each builder function takes the command's computed result dict and an output
path, then uses ArtifactGenerator to emit a tailored HTML artifact. Optional
stonkmode narratives and Dr. Stonk term boxes are layered on top when the
appropriate flags are set.

Kept separate from each command's main() so the wiring in those modules
stays minimal — they add ~10 lines of opt-in code apiece.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Bootstrap project root for sibling imports when invoked via `python3 commands/...`
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ic_engine.rendering.artifact_generator import (
    PALETTE,
    ArtifactGenerator,
    detect_terms_in_text,
    extract_dr_stonk_definitions,
    get_stonkmode_narrative,
)

# ---------------------------------------------------------------------------
# Shared: attach stonkmode narrative + Dr. Stonk box if requested
# ---------------------------------------------------------------------------


def _attach_narrative_and_terms(
    artifact: ArtifactGenerator,
    command: str,
    data_summary_for_llm: str,
    text_for_term_detection: str,
    stonkmode: bool,
) -> None:
    """Optionally append stonkmode narrative + Dr. Stonk term box."""
    combined_text = text_for_term_detection

    if stonkmode:
        narration = get_stonkmode_narrative(command, data_summary_for_llm)
        if narration:
            artifact.add_stonkmode_pair(
                lead_name=narration["lead"]["name"],
                lead_text=narration["lead"]["text"],
                foil_name=narration["foil"]["name"],
                foil_text=narration["foil"]["text"],
                lead_archetype=narration["lead"]["archetype"],
                foil_archetype=narration["foil"]["archetype"],
                closer=narration.get("closer"),
            )
            combined_text = (
                f"{text_for_term_detection} {narration['lead']['text']} {narration['foil']['text']}"
            )

    terms = detect_terms_in_text(combined_text)
    if terms:
        defs = extract_dr_stonk_definitions(terms)
        if defs:
            artifact.add_dr_stonk_box(defs)


# ---------------------------------------------------------------------------
# Holdings artifact
# ---------------------------------------------------------------------------


def build_holdings_artifact(
    compact: Dict[str, Any],
    output_path: str,
    stonkmode: bool = False,
) -> str:
    """Render holdings-command artifact and return the output path."""
    summary = compact.get("summary", {}) or {}
    total_value = summary.get("total_value", 0) or 0
    metadata = {
        "As of": compact.get("as_of", "unknown"),
        "Total": f"${total_value:,.0f}",
        "Equity": f"${summary.get('equity_value', 0):,.0f}",
        "Bond": f"${summary.get('bond_value', 0):,.0f}",
        "Cash": f"${summary.get('cash_value', 0):,.0f}",
        "Unrealized G/L": f"{summary.get('unrealized_gl_pct', 0):+.2f}%",
    }
    artifact = ArtifactGenerator(
        title="Portfolio Holdings Analysis",
        disclaimer="EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        metadata=metadata,
    )

    # Allocation pie (equity/bond/cash/margin)
    alloc_labels: List[str] = []
    alloc_values: List[float] = []
    alloc_colors: List[str] = []
    for key, color_key in (
        ("equity_value", "equity"),
        ("bond_value", "bond"),
        ("cash_value", "cash"),
        ("margin_value", "margin"),
    ):
        val = float(summary.get(key, 0) or 0)
        if val > 0:
            alloc_labels.append(color_key.title())
            alloc_values.append(val)
            alloc_colors.append(PALETTE[color_key])
    if alloc_values:
        artifact.add_pie_chart(
            alloc_labels,
            alloc_values,
            "Asset Allocation",
            col_class="col-4",
            colors=alloc_colors,
        )

    # Sector pie
    sectors = compact.get("sector_weights", {}) or {}
    if sectors:
        artifact.add_pie_chart(
            list(sectors.keys()),
            list(sectors.values()),
            "Sector Allocation (% of Equity)",
            col_class="col-4",
        )

    # Account bar chart
    accounts = compact.get("accounts", {}) or {}
    if accounts:
        acct_names = list(accounts.keys())
        acct_values = [float(a.get("value", 0) or 0) for a in accounts.values()]
        artifact.add_bar_chart(
            acct_names,
            acct_values,
            "Value by Account",
            x_label="Account",
            y_label="Value ($)",
            col_class="col-4",
        )

    # Top holdings table
    top_equity = compact.get("top_equity", []) or []
    if top_equity:
        rows = [
            {
                "Symbol": h.get("symbol", ""),
                "Sector": h.get("sector", ""),
                "Value": f"${h.get('value', 0):,.2f}",
                "Weight": f"{h.get('weight_pct', 0):.2f}%",
                "G/L": f"{h.get('gl_pct', 0):+.2f}%",
                "Type": h.get("type", "equity"),
            }
            for h in top_equity
        ]
        artifact.add_table(
            rows, "Top Holdings", columns=["Symbol", "Sector", "Value", "Weight", "G/L", "Type"]
        )

    # Top bonds table (if any)
    top_bonds = compact.get("top_bonds", []) or []
    if top_bonds:
        bond_rows = [
            {
                "Bond": b.get("name", ""),
                "CUSIP": b.get("cusip", ""),
                "Value": f"${b.get('value', 0):,.2f}",
                "Weight": f"{b.get('weight_pct', 0):.2f}%",
                "Coupon": f"{b.get('coupon', 0) or 0:.2f}%",
                "Maturity": b.get("maturity", "") or "",
            }
            for b in top_bonds
        ]
        artifact.add_table(
            bond_rows,
            "Top Bond Holdings",
            columns=["Bond", "CUSIP", "Value", "Weight", "Coupon", "Maturity"],
        )

    # Summary text for stonkmode narration
    top10 = top_equity[:10]
    summary_lines = [
        f"Total portfolio: ${total_value:,.0f}",
        f"Equity: ${summary.get('equity_value', 0):,.0f}",
        f"Bonds: ${summary.get('bond_value', 0):,.0f}",
        f"Cash: ${summary.get('cash_value', 0):,.0f}",
        f"Unrealized G/L: {summary.get('unrealized_gl_pct', 0):+.1f}%",
        "",
        "TOP 10 HOLDINGS:",
    ]
    for i, h in enumerate(top10, 1):
        summary_lines.append(
            f"  {i}. {h.get('symbol')}: ${h.get('value', 0):,.0f} "
            f"({h.get('weight_pct', 0):.1f}%, G/L {h.get('gl_pct', 0):+.1f}%) - {h.get('sector', '')}"
        )
    data_summary = "\n".join(summary_lines)
    text_for_terms = data_summary  # may pick up "Volatility"/"Sharpe"/etc. in future

    _attach_narrative_and_terms(
        artifact,
        "holdings",
        data_summary,
        text_for_terms,
        stonkmode,
    )
    return str(artifact.save(output_path))


# ---------------------------------------------------------------------------
# Bonds artifact
# ---------------------------------------------------------------------------


def build_bonds_artifact(
    compact: Dict[str, Any],
    output_path: str,
    stonkmode: bool = False,
) -> str:
    """Render bond-analyzer artifact."""
    metadata = {
        "Bond Count": compact.get("bond_count", 0),
        "Total Value": f"${compact.get('total_value', 0):,.2f}",
        "Weighted YTM": f"{compact.get('weighted_avg_ytm_pct', 0):.2f}%",
        "Duration": f"{compact.get('weighted_avg_duration_yrs', 0):.2f} yrs",
        "Duration Risk": compact.get("duration_risk", "n/a"),
        "Avg Credit": compact.get("average_credit_quality", "n/a"),
    }
    artifact = ArtifactGenerator(
        title="Bond Portfolio Analysis",
        disclaimer="EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        metadata=metadata,
    )

    # Maturity ladder bar chart
    ladder = compact.get("maturity_ladder", {}) or {}
    if ladder:
        buckets = list(ladder.keys())
        values = [float(v.get("value", 0)) for v in ladder.values()]
        artifact.add_bar_chart(
            buckets,
            values,
            "Maturity Ladder",
            x_label="Bucket",
            y_label="Market Value ($)",
            col_class="col-6",
            color=PALETTE["bond"],
        )

    # Asset type breakdown pie
    atype = compact.get("asset_type_breakdown", {}) or {}
    if atype:
        artifact.add_pie_chart(
            list(atype.keys()),
            [float(v.get("value", 0)) for v in atype.values()],
            "Asset-Type Breakdown",
            col_class="col-6",
        )

    # Yield curve ladder (approximate — use avg YTM per bucket if available)
    curve_x, curve_y = [], []
    for bucket, data in ladder.items():
        if "avg_ytm" in data:
            curve_x.append(bucket)
            curve_y.append(float(data["avg_ytm"]))
    if curve_x and curve_y:
        artifact.add_line_chart(
            curve_x,
            curve_y,
            "Approximate Yield Curve (by Maturity Bucket)",
            x_label="Maturity Bucket",
            y_label="YTM (%)",
            col_class="col-12",
        )

    # Recommendations
    recs = compact.get("recommendations", []) or []
    if recs:
        rec_rows = [{"Recommendation": str(r)} for r in recs]
        artifact.add_table(rec_rows, "Recommendations", columns=["Recommendation"])

    # Stonkmode narrative summary
    summary_lines = [
        f"Total bond value: ${compact.get('total_value', 0):,.0f}",
        f"Average YTM: {compact.get('weighted_avg_ytm_pct', 0):.2f}%",
        f"Average duration: {compact.get('weighted_avg_duration_yrs', 0):.2f} years",
        f"Duration risk: {compact.get('duration_risk', 'n/a')}",
        f"Credit quality: {compact.get('average_credit_quality', 'n/a')}",
    ]
    if ladder:
        summary_lines.append("")
        summary_lines.append("Maturity ladder:")
        for bucket, data in ladder.items():
            summary_lines.append(
                f"  {bucket}: ${data.get('value', 0):,.0f} ({data.get('pct', 0):.1f}%)"
            )
    data_summary = "\n".join(summary_lines)
    # Always mention these terms so Dr. Stonk picks them up
    text_for_terms = data_summary + " Duration. Yield to Maturity. Coupon Rate. Spread."

    _attach_narrative_and_terms(
        artifact,
        "bonds",
        data_summary,
        text_for_terms,
        stonkmode,
    )
    return str(artifact.save(output_path))


# ---------------------------------------------------------------------------
# Analyst artifact
# ---------------------------------------------------------------------------


def build_analyst_artifact(
    payload: Dict[str, Any],
    output_path: str,
    stonkmode: bool = False,
) -> str:
    """Render analyst-recommendations artifact."""
    recs = payload.get("recommendations", {}) or {}
    metadata = {
        "Symbols Covered": len(recs),
        "Timestamp": payload.get("timestamp", ""),
    }
    artifact = ArtifactGenerator(
        title="Analyst Recommendations",
        disclaimer="EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        metadata=metadata,
    )

    # Rating distribution across portfolio
    rating_counts: Dict[str, int] = {}
    for rec in recs.values():
        consensus = str(rec.get("consensus") or "Unknown")
        rating_counts[consensus] = rating_counts.get(consensus, 0) + 1
    if rating_counts:
        artifact.add_pie_chart(
            list(rating_counts.keys()),
            list(rating_counts.values()),
            "Consensus Rating Distribution",
            col_class="col-6",
        )

    # Top 10 most-covered symbols bar chart
    by_count = sorted(
        recs.items(),
        key=lambda kv: kv[1].get("analyst_count", 0) or 0,
        reverse=True,
    )[:15]
    if by_count:
        artifact.add_bar_chart(
            [sym for sym, _ in by_count],
            [r.get("analyst_count", 0) or 0 for _, r in by_count],
            "Top-15 by Analyst Coverage",
            x_label="Symbol",
            y_label="Analyst Count",
            col_class="col-6",
        )

    # Top bullish (lowest recommendation_mean) table
    def _rec_mean(r: Dict[str, Any]) -> float:
        return r.get("recommendation_mean") or 5.0

    sorted_recs = sorted(recs.items(), key=lambda kv: _rec_mean(kv[1]))
    rows = []
    for sym, r in sorted_recs[:25]:
        rows.append(
            {
                "Symbol": sym,
                "Consensus": r.get("consensus") or "N/A",
                "Mean": f"{r.get('recommendation_mean', 0) or 0:.2f}",
                "Analysts": r.get("analyst_count", 0) or 0,
                "Price": f"${r.get('current_price', 0) or 0:.2f}",
                "Target": f"${r.get('target_price_mean', 0) or 0:.2f}",
                "Buy": r.get("buy_count", 0) or 0,
                "Hold": r.get("hold_count", 0) or 0,
                "Sell": r.get("sell_count", 0) or 0,
            }
        )
    if rows:
        artifact.add_table(
            rows,
            "Top 25 by Consensus (most bullish first)",
            columns=[
                "Symbol",
                "Consensus",
                "Mean",
                "Analysts",
                "Price",
                "Target",
                "Buy",
                "Hold",
                "Sell",
            ],
        )

    summary_lines = [f"Total symbols: {len(recs)}"]
    summary_lines.append("")
    summary_lines.append("Top 5 by consensus:")
    for sym, r in sorted_recs[:5]:
        summary_lines.append(
            f"  {sym}: {r.get('consensus', 'N/A')} "
            f"({r.get('analyst_count', 0)} analysts, "
            f"${r.get('current_price', 0):.2f})"
        )
    data_summary = "\n".join(summary_lines)
    text_for_terms = data_summary

    _attach_narrative_and_terms(
        artifact,
        "analyst",
        data_summary,
        text_for_terms,
        stonkmode,
    )
    return str(artifact.save(output_path))


# ---------------------------------------------------------------------------
# Lookup artifact
# ---------------------------------------------------------------------------


def build_lookup_artifact(
    result: Dict[str, Any],
    output_path: str,
    stonkmode: bool = False,
) -> str:
    """Render lookup-command artifact (single-symbol detail)."""
    symbol = result.get("symbol") or result.get("Symbol") or "Unknown"
    metadata = {"Symbol": symbol}
    # Surface a couple of common fields when present
    for k in ("price", "current_price", "sector", "asset_type", "consensus", "weight_pct", "value"):
        if k in result:
            v = result[k]
            label = k.replace("_", " ").title()
            if isinstance(v, (int, float)) and k != "consensus":
                if "pct" in k:
                    metadata[label] = f"{v:.2f}%"
                elif k in ("price", "current_price", "value"):
                    metadata[label] = f"${v:,.2f}"
                else:
                    metadata[label] = f"{v:,.2f}"
            else:
                metadata[label] = str(v)

    artifact = ArtifactGenerator(
        title=f"Lookup: {symbol}",
        disclaimer="EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        metadata=metadata,
    )

    # Flatten result into a key/value table
    flat_rows: List[Dict[str, Any]] = []
    for k, v in result.items():
        if isinstance(v, (list, dict)):
            continue
        flat_rows.append({"Field": k, "Value": v})
    if flat_rows:
        artifact.add_table(flat_rows, "Details", columns=["Field", "Value"], sortable=False)

    # Nested list views — typical for bond lookups returning multiple CUSIPs
    for list_key in ("bonds", "holdings", "news", "results"):
        if isinstance(result.get(list_key), list) and result[list_key]:
            sample = result[list_key][0]
            if isinstance(sample, dict):
                artifact.add_table(
                    result[list_key],
                    f"{list_key.title()} ({len(result[list_key])})",
                )

    summary_lines = [f"Symbol: {symbol}"]
    for k in ("price", "current_price", "sector", "consensus", "value"):
        if k in result:
            summary_lines.append(f"{k}: {result[k]}")
    data_summary = "\n".join(summary_lines)
    _attach_narrative_and_terms(
        artifact,
        "lookup",
        data_summary,
        data_summary,
        stonkmode,
    )
    return str(artifact.save(output_path))


# ---------------------------------------------------------------------------
# Performance artifact
# ---------------------------------------------------------------------------


def build_performance_artifact(
    compact: Dict[str, Any],
    output_path: str,
    stonkmode: bool = False,
) -> str:
    """Render performance-analyzer artifact."""
    d = compact.get("data", compact) if isinstance(compact, dict) else {}
    summary = d.get("summary", d.get("portfolio_summary", {})) or {}
    metadata = {
        "Period": d.get("period", "ytd"),
        "Weighted Volatility": f"{summary.get('weighted_volatility', 0) or 0:.4f}",
        "Weighted Sharpe": f"{summary.get('weighted_sharpe', 0) or 0:.4f}",
        "Holdings Analyzed": d.get("holdings_analyzed", d.get("holdings_valid", 0)),
    }
    artifact = ArtifactGenerator(
        title="Portfolio Performance Analysis",
        disclaimer="EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        metadata=metadata,
    )

    # Top / bottom performers (compact summary may provide these directly)
    top = d.get("top_performers", []) or []
    bottom = d.get("bottom_performers", d.get("worst_performers", [])) or []

    if top:
        artifact.add_bar_chart(
            [p.get("symbol", "?") for p in top[:10]],
            [float(p.get("return_pct", p.get("gl_pct", 0)) or 0) for p in top[:10]],
            "Top 10 Performers (% return)",
            x_label="Symbol",
            y_label="Return (%)",
            col_class="col-6",
            color=PALETTE["pos"],
        )

    if bottom:
        artifact.add_bar_chart(
            [p.get("symbol", "?") for p in bottom[:10]],
            [float(p.get("return_pct", p.get("gl_pct", 0)) or 0) for p in bottom[:10]],
            "Bottom 10 Performers (% return)",
            x_label="Symbol",
            y_label="Return (%)",
            col_class="col-6",
            color=PALETTE["neg"],
        )

    # Per-symbol risk/return table from `performance`
    perf = d.get("performance", {}) or {}
    rows = []
    for sym, metrics in perf.items():
        if not isinstance(metrics, dict):
            continue
        vol = metrics.get("volatility", {}) or {}
        sharpe_block = metrics.get("sharpe_ratio", {}) or {}
        ret_block = metrics.get("returns", {}) or {}
        rows.append(
            {
                "Symbol": sym,
                "Return": f"{ret_block.get('total_return_pct', ret_block.get('total_return', 0) or 0):.2f}%",
                "Volatility": f"{vol.get('annualized_volatility', 0) or 0:.4f}",
                "Sharpe": f"{sharpe_block.get('sharpe_ratio', 0) or 0:.4f}",
                "Max DD": f"{(metrics.get('max_drawdown', {}) or {}).get('max_drawdown_pct', 0) or 0:.2f}%",
                "Beta": f"{(metrics.get('beta', {}) or {}).get('beta', 0) or 0:.3f}",
            }
        )
    # Order by symbol alphabetically so sort buttons feel consistent
    rows.sort(key=lambda r: r["Symbol"])
    if rows:
        artifact.add_table(
            rows,
            "Per-Symbol Risk / Return",
            columns=["Symbol", "Return", "Volatility", "Sharpe", "Max DD", "Beta"],
            max_rows=200,
        )

    summary_lines = [
        f"Period: {d.get('period', 'ytd')}",
        f"Weighted volatility: {summary.get('weighted_volatility', 0) or 0:.4f}",
        f"Weighted Sharpe: {summary.get('weighted_sharpe', 0) or 0:.4f}",
        f"Holdings analyzed: {d.get('holdings_analyzed', d.get('holdings_valid', 0))}",
    ]
    if top:
        summary_lines.append("")
        summary_lines.append("Top 5 performers:")
        for p in top[:5]:
            summary_lines.append(
                f"  {p.get('symbol', '?')}: {p.get('return_pct', p.get('gl_pct', 0)):+.1f}%"
            )
    if bottom:
        summary_lines.append("")
        summary_lines.append("Bottom 5 performers:")
        for p in bottom[:5]:
            summary_lines.append(
                f"  {p.get('symbol', '?')}: {p.get('return_pct', p.get('gl_pct', 0)):+.1f}%"
            )
    data_summary = "\n".join(summary_lines)
    # Always surface risk terms
    text_for_terms = data_summary + " Sharpe Ratio Volatility Beta Max Drawdown Value at Risk"

    _attach_narrative_and_terms(
        artifact,
        "performance",
        data_summary,
        text_for_terms,
        stonkmode,
    )
    return str(artifact.save(output_path))


# ---------------------------------------------------------------------------
# News artifact
# ---------------------------------------------------------------------------


def build_news_artifact(
    report: Dict[str, Any],
    output_path: str,
    stonkmode: bool = False,
) -> str:
    """Render portfolio-news artifact."""
    narr = report.get("portfolio_narrative", {}) or {}
    impact = report.get("impact_summary", {}) or {}
    metadata = {
        "Posture": narr.get("overall_posture", report.get("posture", "neutral")),
        "Positive": impact.get("positive", report.get("positive_count", 0)),
        "Negative": impact.get("negative", report.get("negative_count", 0)),
        "Net Impact": f"{impact.get('net_impact', report.get('net_impact', 0)):+.2f}",
    }
    artifact = ArtifactGenerator(
        title="Portfolio News & Sentiment",
        disclaimer="EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        metadata=metadata,
    )

    # Positive / negative counts bar
    artifact.add_bar_chart(
        ["Positive", "Negative"],
        [
            float(impact.get("positive", report.get("positive_count", 0)) or 0),
            float(impact.get("negative", report.get("negative_count", 0)) or 0),
        ],
        "Sentiment Counts",
        x_label="Class",
        y_label="Articles",
        col_class="col-4",
    )

    # Top movers bar (symbol → portfolio_impact $)
    top_pos = report.get("top_positive_movers", report.get("top_positive", [])) or []
    top_neg = report.get("top_negative_movers", report.get("top_negative", [])) or []

    if top_pos:
        artifact.add_bar_chart(
            [m.get("symbol", "?") for m in top_pos[:10]],
            [float(m.get("portfolio_impact", m.get("impact", 0)) or 0) for m in top_pos[:10]],
            "Top Positive Movers ($ impact)",
            x_label="Symbol",
            y_label="Impact ($)",
            col_class="col-4",
            color=PALETTE["pos"],
        )
    if top_neg:
        artifact.add_bar_chart(
            [m.get("symbol", "?") for m in top_neg[:10]],
            [float(m.get("portfolio_impact", m.get("impact", 0)) or 0) for m in top_neg[:10]],
            "Top Negative Movers ($ impact)",
            x_label="Symbol",
            y_label="Impact ($)",
            col_class="col-4",
            color=PALETTE["neg"],
        )

    # Narrative text
    if narr.get("narrative"):
        artifact.add_narrative_block(
            "Portfolio Narrative",
            str(narr.get("narrative", "")),
            persona="Portfolio Desk",
        )

    # Recent headlines table
    headlines: List[Dict[str, Any]] = []
    for m in (list(top_pos) + list(top_neg))[:30]:
        headlines.append(
            {
                "Symbol": m.get("symbol", "?"),
                "Sentiment": m.get("sentiment", ""),
                "Impact": f"${m.get('portfolio_impact', m.get('impact', 0)):,.0f}",
                "Title": (m.get("title") or "")[:120],
            }
        )
    if headlines:
        artifact.add_table(
            headlines,
            "Top Headlines",
            columns=["Symbol", "Sentiment", "Impact", "Title"],
        )

    summary_lines = [
        f"Overall posture: {metadata['Posture']}",
        f"Net impact: {metadata['Net Impact']}",
        f"Positive stories: {metadata['Positive']}, Negative: {metadata['Negative']}",
    ]
    if narr.get("narrative"):
        summary_lines.append(f"Narrative: {narr['narrative'][:300]}")
    data_summary = "\n".join(summary_lines)
    _attach_narrative_and_terms(
        artifact,
        "news",
        data_summary,
        data_summary,
        stonkmode,
    )
    return str(artifact.save(output_path))


# ---------------------------------------------------------------------------
# argv helpers (re-exported so commands need only one import)
# ---------------------------------------------------------------------------


def pop_argv_flag(argv_list: List[str], flag: str) -> bool:
    """Destructively remove a boolean flag from argv_list. Returns whether it
    was present."""
    present = False
    while flag in argv_list:
        argv_list.remove(flag)
        present = True
    return present


def pop_artifact_flags(argv_list: List[str]) -> tuple[Optional[str], bool]:
    """Destructively remove --artifact/--stonkmode from argv_list.

    Returns (artifact_path, stonkmode_enabled). We modify the list in place
    so downstream argparse / positional parsing isn't confused by the new
    flags.

    Callers that parse argv positionally AND are in `_VERBOSE_AWARE_COMMANDS`
    (which gets `--verbose` auto-appended by the router) must also call
    `pop_argv_flag(argv_list, "--verbose")` before positional parsing to
    avoid `--verbose` being interpreted as an output-file path. Scripts
    that read `--verbose` for real behavior (analyze_performance_polars,
    fetch_analyst_recommendations_parallel, fetch_portfolio_news,
    portfolio_analyzer) must NOT strip it here.
    """
    artifact_path: Optional[str] = None
    stonk: bool = False

    # --artifact PATH
    if "--artifact" in argv_list:
        idx = argv_list.index("--artifact")
        if idx + 1 < len(argv_list):
            artifact_path = argv_list[idx + 1]
            del argv_list[idx : idx + 2]
        else:
            del argv_list[idx : idx + 1]

    # --stonkmode (boolean)
    if "--stonkmode" in argv_list:
        stonk = True
        argv_list.remove("--stonkmode")

    # Fallback: stonkmode state file (only used when artifact is requested;
    # avoid silently running LLM calls for non-artifact command invocations)
    if artifact_path and not stonk:
        try:
            from ic_engine.rendering.stonkmode import is_enabled

            stonk = bool(is_enabled())
        except Exception:
            stonk = False

    return artifact_path, stonk
