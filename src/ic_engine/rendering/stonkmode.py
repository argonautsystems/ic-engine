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
Stonkmode — hilarious presentation-layer toggle for InvestorClaw.

Wraps /portfolio command output in commentary from randomly selected pairs
of fictional cable finance TV personalities. The data analysis runs normally;
stonkmode only changes how results are narrated to the user via two LLM calls
(lead + foil).

State file: ~/.investorclaw/stonkmode.json

LLM Configuration Hierarchy (for narrative/stonkmode):
=====================================================

By default, stonkmode uses the SAME LLM as operational analysis unless explicitly
configured otherwise. This avoids forcing users to configure multiple LLMs.

Configuration order (first set wins):
  1. INVESTORCLAW_NARRATIVE_* — explicit stonkmode LLM configuration
  2. INVESTORCLAW_STONKMODE_* — legacy, backward compatibility
  3. INVESTORCLAW_CONSULTATION_* — same as operational/consultative LLM
  4. OPENCLAW_MODEL — operational LLM (via environment setup)
  5. Defaults: http://localhost:11434 / gemma4:e4b (Ollama) or grok-3-fast (OpenAI)

Env vars (prefer NARRATIVE_* for new configs):
  INVESTORCLAW_NARRATIVE_PROVIDER     — "ollama" (default) or "openai_compat"
  INVESTORCLAW_NARRATIVE_ENDPOINT     — e.g. http://localhost:11434 or https://api.together.xyz/v1
  INVESTORCLAW_NARRATIVE_MODEL        — e.g. gemma4:e4b or grok-3-fast
  INVESTORCLAW_NARRATIVE_API_KEY      — optional, for OpenAI-compatible providers

Examples:
  # User only configures operational LLM, stonkmode uses same
  export INVESTORCLAW_CONSULTATION_ENDPOINT=http://localhost:11434
  export INVESTORCLAW_CONSULTATION_MODEL=gemma4:e4b
  # Stonkmode automatically uses same endpoint/model

  # User wants different LLM for stonkmode only
  export INVESTORCLAW_CONSULTATION_MODEL=gemma4:e4b
  export INVESTORCLAW_NARRATIVE_MODEL=gemma4:e2b  # different model for faster narration
  # Stonkmode uses gemma4:e2b, everything else uses gemma4:e4b
"""

from __future__ import annotations

import json
import logging
import os
import random
import re as _re
import urllib.error
import urllib.parse
import urllib.request


def _validate_http_url(url: str) -> str:
    """Return url if scheme is http/https; raise ValueError otherwise."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Refusing non-http(s) URL scheme: {parsed.scheme!r} in {url!r}")
    return url


from pathlib import Path
from typing import Any, Optional

from ic_engine.rendering.dr_stonk import build_dr_stonk_box, extract_terms_used, is_dr_stonk_enabled

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ANSI color palette — 16-color safe, works on any POSIX terminal
# ---------------------------------------------------------------------------

_C: dict[str, str] = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    # Structural chrome
    "grey": "\033[90m",  # box borders
    "white": "\033[97m",  # header text
    # Archetype label colors (bold persona name banner)
    "yellow": "\033[93m",  # high_energy — Blitz, Big Jim energy
    "blue": "\033[94m",  # serious — Prescott, Amara, Carmen
    "green": "\033[92m",  # mentors — sage, experienced
    "cyan": "\033[96m",  # cosmic + policy_veterans
    "magenta": "\033[95m",  # wildcards — Glorb, ARIA-7, chaos
    "red": "\033[91m",  # bears — Victor, Hans doom spiral
    # Archetype body text (dimmer variants for readability)
    "yellow_dim": "\033[33m",
    "blue_dim": "\033[34m",
    "green_dim": "\033[32m",
    "cyan_dim": "\033[36m",
    "magenta_dim": "\033[35m",
    "red_dim": "\033[31m",
    # Data output colors
    "gain": "\033[92m",  # green for positive G/L
    "loss": "\033[91m",  # red for negative G/L
    "ticker": "\033[1m\033[96m",  # bold cyan for ticker symbols
    "value": "\033[97m",  # bright white for dollar values
    "alert_crit": "\033[1m\033[91m",  # bold red — critical alerts
    "alert_med": "\033[93m",  # yellow — medium alerts
    "alert_info": "\033[36m",  # cyan — info
    "footer": "\033[33m",  # yellow for footer/disclaimer
}

# Archetype → label color key
_ARCH_LABEL: dict[str, str] = {
    "high_energy": "yellow",
    "serious": "blue",
    "mentors": "green",
    "policy_veterans": "cyan",
    "wildcards": "magenta",
    "cosmic": "cyan",
    "digital": "magenta",
    "bears": "red",
}

# Archetype → body color key (dimmer, for readability over multiple paragraphs)
_ARCH_BODY: dict[str, str] = {
    "high_energy": "yellow_dim",
    "serious": "blue_dim",
    "mentors": "green_dim",
    "policy_veterans": "cyan_dim",
    "wildcards": "magenta_dim",
    "cosmic": "cyan_dim",
    "digital": "magenta_dim",
    "bears": "red_dim",
}

# Regex: ALL-CAPS words ≥3 chars (pure alpha), for bold emphasis
_CAPS_RE = _re.compile(r"\b[A-Z]{3,}\b")

STATE_FILE = Path.home() / ".investorclaw" / "stonkmode.json"

# ---------------------------------------------------------------------------
# Command aliases → canonical command name for summarizer dispatch
# ---------------------------------------------------------------------------

COMMAND_ALIASES: dict[str, list[str]] = {
    "holdings": ["holdings", "snapshot", "prices"],
    "performance": ["performance", "analyze", "returns"],
    "analyst": ["analyst", "analysts", "ratings"],
    "news": ["news", "sentiment"],
    "bonds": ["bonds", "bond-analysis", "analyze-bonds"],
    "analysis": ["analysis", "portfolio-analysis"],
    "synthesize": [
        "synthesize",
        "multi-factor",
        "recommend",
        "recommendations",
        "synthesize-opportunities",
        "analyze-multi",
    ],
    "fixed-income": ["fixed-income", "fixed-income-analysis", "bond-strategy"],
    "report": ["report", "export", "csv", "excel"],
    "lookup": ["lookup", "query", "detail"],
    "session": ["session", "session-init", "risk-profile", "calibrate"],
    "eod-report": ["eod-report", "eod", "daily-report", "end-of-day"],
}

# Invert: command_alias → canonical_name
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canon, _aliases in COMMAND_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL[_alias] = _canon

# Output files per canonical command
_COMMAND_OUTPUT_FILES: dict[str, str] = {
    "holdings": "holdings.json",
    "performance": "performance.json",
    "analyst": "analyst_data.json",
    "news": "portfolio_news.json",
    "bonds": "bond_analysis.json",
    "analysis": "portfolio_analysis.json",
    "synthesize": "portfolio_analysis.json",
    "fixed-income": "fixed_income_analysis.json",
    "report": "portfolio_report.xlsx",
    "lookup": "",  # no output file
    "session": "session_profile.json",
    "eod-report": "",  # composite
}


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state() -> Optional[dict]:
    """Load stonkmode state from disk. Returns None if not found."""
    if not STATE_FILE.exists():
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state: dict) -> None:
    """Write stonkmode state to disk."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_enabled() -> bool:
    """Return True if stonkmode is currently active.

    Respects INVESTORCLAW_STONKMODE_DISABLED=true env var for test isolation.
    """
    import os as _os

    if _os.environ.get("INVESTORCLAW_STONKMODE_DISABLED", "").lower() in ("true", "1", "yes"):
        return False
    state = load_state()
    return bool(state and state.get("enabled"))


# ---------------------------------------------------------------------------
# Stonkmode promo tip  (randomized; used in setup and auto-setup paths)
# ---------------------------------------------------------------------------

_STONKMODE_TIP_DROPS: list[tuple[str, str]] = [
    ("Glorb, Senior Ledger-Keeper of the Seventh Vault", "Profitable, may your ledger be."),
    (
        "King Donny (The Deal Whisperer)",
        "Best mode ever, believe me. Nobody does stonkmode better. That I can tell you.",
    ),
    ("Zsa Zsa Von Portfolio", "Dahlink, do try to diversify — it worked wonders for my marriages."),
    ('Chico "The Vibe" Reyes', "That's the vibe, man. Trust the vibe."),
    ('"Far Out" Farley McGee', "The market, man... it's just the universe, man."),
    ("Blitz Thunderbuy", "THUNDER-BUY ALERT — DO THE HOMEWORK AND UNLEASH THE BEAST!"),
    (
        'Krystal "The Receipt" Kash',
        "And that's the receipt, besties. We are not not bullish on this.",
    ),
    (
        'Zara "Viral" Zhao',
        "Like, literally, that's the play. The algorithm understood the assignment.",
    ),
    ('Priya "HODL" Sharma', "ngmi if you're still in plain mode."),
    ('Victor "The Vulture" Voss', "I'll be here when it happens."),
    ("Hans-Dieter Braun", "This will not end well. But at least you will be informed, ja."),
    (
        "Dr. Amara Osei-Bonsu",
        "The risk is already in the portfolio — we just haven't activated it yet.",
    ),
    (
        'Brick "Diamond Hands" Stonksworth',
        "Diamond Hands Nation, the entertainment layer was always there. You just had to BELIEVE.",
    ),
    (
        'Sal "The Pit" Decibelli',
        "ARE YOU KIDDING ME?! Thirty personalities and you haven't turned this on yet?!",
    ),
    (
        'Wendell "The Pattern" Pruitt',
        "They don't want you to know about stonkmode. But now you do.",
    ),
    ("Professor What?", "I've said too much. Or not enough. Temporal ethics are complicated."),
]


def stonkmode_tip(always: bool = False) -> Optional[str]:
    """Return a formatted stonkmode promo tip string, or None.

    Args:
        always: If True, always return the tip (e.g., end of first-time wizard).
                If False (default), show ~1-in-3 chance (for repeat setup calls).

    Returns the full tip block as a string, or None if the random gate blocks it.
    """
    if not always and random.random() > 0.33:
        return None

    name, tagline = random.choice(_STONKMODE_TIP_DROPS)
    return "\n".join(
        [
            "📊 PRO TIP — STONKMODE:",
            "  Once you have portfolio data, try the entertainment layer:",
            "  /portfolio stonkmode on",
            "  Then run any analysis command to get live commentary from",
            "  30 fictional cable TV finance personalities — bears, bulls,",
            "  crypto maxis, ESG crusaders, a Kardashian, a goblin, and more.",
            "  /portfolio stonkmode off  to return to normal mode.",
            "",
            f"  — {name}",
            f'    "{tagline}"',
        ]
    )


# ---------------------------------------------------------------------------
# Persona selection
# ---------------------------------------------------------------------------


def get_persona(persona_id: str) -> dict:
    """Return persona dict from stonkmode_personas.py."""
    from rendering.stonkmode_personas import PERSONAS

    return PERSONAS[persona_id]


def select_cohost_mode(has_previous_foil_message: bool) -> str:
    """Select interaction mode for this segment.

    50% clap-back (requires previous foil message; falls back to standalone),
    30% standalone, 20% setup.
    """
    roll = random.random()
    if roll < 0.50:
        return "clap-back" if has_previous_foil_message else "standalone"
    elif roll < 0.80:
        return "standalone"
    else:
        return "setup"


# ---------------------------------------------------------------------------
# Output summarization
# ---------------------------------------------------------------------------


def _get_reports_dir() -> Path:
    """Resolve reports directory matching the main pipeline logic."""
    reports_env = os.environ.get("INVESTOR_CLAW_REPORTS_DIR", "").strip()
    if reports_env:
        return Path(reports_env).expanduser()
    return Path.home() / "portfolio_reports"


def _load_json(path: Path) -> Optional[dict]:
    """Load JSON from path, return None on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _summarize_holdings(data: dict) -> str:
    """Summarize holdings compact output for narration."""
    d = data.get("data", data)
    summary = d.get("summary", {})
    total_val = summary.get("total_value", 0)
    equity_val = summary.get("equity_value", 0)
    bond_val = summary.get("bond_value", 0)
    cash_val = summary.get("cash_value", 0)
    gl_pct = summary.get("unrealized_gl_pct", 0)
    positions = summary.get("position_count", {})
    equity_count = positions.get("equity", 0)
    bond_count = positions.get("bond", 0)

    top_equity = d.get("top_equity", [])[:10]
    sectors = d.get("sector_weights", {})

    lines = [
        f"Total portfolio: ${total_val:,.0f}",
        f"Equity: ${equity_val:,.0f} ({equity_count} positions)",
        f"Bonds: ${bond_val:,.0f} ({bond_count} positions)",
        f"Cash: ${cash_val:,.0f}",
        f"Unrealized G/L: {gl_pct:+.1f}%",
        "",
        "Top 10 holdings by value:",
    ]
    for h in top_equity:
        sym = h.get("symbol", "???")
        val = h.get("value", 0)
        wt = h.get("weight_pct", 0)
        gl = h.get("gl_pct", 0)
        lines.append(f"  {sym}: ${val:,.0f} ({wt:.1f}%, G/L {gl:+.1f}%)")

    if sectors:
        lines.append("")
        lines.append("Sector breakdown:")
        for sec, pct in list(sectors.items())[:8]:
            lines.append(f"  {sec}: {pct:.1f}%")

    return "\n".join(lines)


def _summarize_performance(data: dict) -> str:
    """Summarize performance compact output."""
    d = data.get("data", data)
    summary = d.get("summary", d.get("portfolio_summary", {}))
    total_return = summary.get("total_return_pct", summary.get("total_return", 0))
    sharpe = summary.get("sharpe_ratio", None)

    top = d.get("top_performers", d.get("top_equity", []))[:5]
    bottom = d.get("bottom_performers", d.get("worst_performers", []))[:5]

    lines = [f"Total return: {total_return:+.2f}%"]
    if sharpe is not None:
        lines.append(f"Sharpe ratio: {sharpe:.2f}")

    if top:
        lines.append("")
        lines.append("Top 5 performers:")
        for p in top:
            sym = p.get("symbol", "???")
            ret = p.get("return_pct", p.get("gl_pct", 0))
            lines.append(f"  {sym}: {ret:+.1f}%")

    if bottom:
        lines.append("")
        lines.append("Bottom 5 performers:")
        for p in bottom:
            sym = p.get("symbol", "???")
            ret = p.get("return_pct", p.get("gl_pct", 0))
            lines.append(f"  {sym}: {ret:+.1f}%")

    return "\n".join(lines)


def _summarize_analyst(data: dict) -> str:
    """Summarize analyst data compact output."""
    d = data.get("data", data)
    recs = d.get("recommendations", {})
    if not recs:
        return "No analyst recommendations available."

    # Sort by recommendation_mean (lower = more bullish)
    sorted_recs = sorted(
        recs.items(),
        key=lambda kv: kv[1].get("recommendation_mean", 5),
    )

    lines = [f"Total symbols: {len(recs)}", "", "Top 5 by consensus:"]
    for sym, rec in sorted_recs[:5]:
        consensus = rec.get("consensus", "N/A")
        count = rec.get("analyst_count", 0)
        price = rec.get("current_price", 0)
        lines.append(f"  {sym}: {consensus} ({count} analysts, ${price:.2f})")

    return "\n".join(lines)


def _summarize_news(data: dict) -> str:
    """Summarize news compact output."""
    d = data.get("data", data)
    posture = d.get("posture", "Neutral")
    impact = d.get("impact_summary", {})
    net_impact = impact.get("net_impact", 0)
    positive_count = impact.get("positive", 0)
    negative_count = impact.get("negative", 0)
    narrative = d.get("narrative", "")

    top_pos = d.get("top_positive", [])[:3]
    top_neg = d.get("top_negative", [])[:3]

    lines = [
        f"Overall posture: {posture}",
        f"Net impact: {net_impact:+.2f}",
        f"Positive stories: {positive_count}, Negative: {negative_count}",
    ]
    if narrative:
        lines.append(f"Narrative: {narrative[:200]}")

    if top_pos:
        lines.append("")
        lines.append("Top positive movers:")
        for item in top_pos:
            lines.append(f"  {item.get('symbol', '???')}: {item.get('title', '')[:60]}")

    if top_neg:
        lines.append("")
        lines.append("Top negative movers:")
        for item in top_neg:
            lines.append(f"  {item.get('symbol', '???')}: {item.get('title', '')[:60]}")

    return "\n".join(lines)


def _summarize_bonds(data: dict) -> str:
    """Summarize bond analysis compact output."""
    d = data.get("data", data)
    summary = d.get("summary", d.get("portfolio_summary", {}))
    avg_ytm = summary.get("avg_ytm", summary.get("average_ytm", 0))
    avg_dur = summary.get("avg_duration", summary.get("average_duration", 0))
    total_val = summary.get("total_value", summary.get("total_bond_value", 0))

    holdings = d.get("holdings", d.get("bonds", []))[:5]
    credit = d.get("credit_quality", d.get("credit_mix", {}))

    lines = [
        f"Total bond value: ${total_val:,.0f}",
        f"Average YTM: {avg_ytm:.2f}%",
        f"Average duration: {avg_dur:.1f} years",
    ]

    if holdings:
        lines.append("")
        lines.append("Top holdings by YTM:")
        for h in holdings:
            name = h.get("name", h.get("symbol", "???"))
            ytm = h.get("ytm", h.get("yield_to_maturity", 0))
            lines.append(f"  {name}: {ytm:.2f}% YTM")

    if credit:
        lines.append("")
        lines.append("Credit quality mix:")
        for grade, pct in list(credit.items())[:5]:
            lines.append(f"  {grade}: {pct:.1f}%")

    return "\n".join(lines)


def _summarize_with_top10(data: dict, label: str = "PORTFOLIO") -> str:
    """Shared helper: build the rich top-10 rundown injected into analysis/synthesize prompts.

    Relies on top_equity being injected by summarize_for_narration from holdings.json.
    """
    # Top-level summary figures (from injected _holdings_summary or portfolio_summary)
    hs = data.get("_holdings_summary", data.get("portfolio_summary", {}))
    total_val = hs.get("total_value", 0)
    equity_val = hs.get("equity_value", 0)
    gl_pct = hs.get("unrealized_gl_pct", 0)
    pos = hs.get("position_count", {})
    eq_count = pos.get("equity", 0) if isinstance(pos, dict) else 0

    # Sector weights from portfolio_analysis or injected
    d = data.get("data", data)
    sectors = d.get("sector_weights", {})
    if not sectors:
        an = d.get("analysis", {})
        sectors = an.get("sectors", an.get("sector_weights", {}))

    # Alerts from analysis
    alerts = d.get("alerts", [])

    lines = [
        f"=== {label} ===",
        f"Total portfolio: ${total_val:,.0f} | Equity: ${equity_val:,.0f} ({eq_count} positions)",
        f"Unrealized G/L: {gl_pct:+.1f}%",
        "",
    ]

    # Top-10 holdings with full detail — the core data for running commentary
    top_equity = data.get("top_equity", [])
    if top_equity:
        lines.append("TOP 10 HOLDINGS (comment on EACH one by name):")
        for i, h in enumerate(top_equity[:10], 1):
            sym = h.get("symbol", "???")
            val = h.get("value", 0)
            wt = h.get("weight_pct", 0)
            gl = h.get("gl_pct", 0)
            sec = h.get("sector", "")
            htype = h.get("type", "")
            tag = f" [{htype.upper()}]" if htype and htype != "equity" else ""
            lines.append(
                f"  {i:2}. {sym}{tag}: ${val:,.0f} | {wt:.1f}% of portfolio | "
                f"G/L {gl:+.1f}% | {sec}"
            )
    else:
        lines.append("(No individual holdings data available — comment on overall allocation)")

    if sectors:
        lines.append("")
        lines.append("Sector breakdown:")
        # sectors may be {name: pct}, {name: {weight_pct: ...}}, or {breakdown: [...]}
        breakdown = sectors.get("breakdown") if isinstance(sectors, dict) else None
        if breakdown and isinstance(breakdown, list):
            for item in breakdown[:8]:
                sname = item.get("sector", "?")
                spct = item.get("sector_pct", item.get("weight_pct", 0))
                lines.append(
                    f"  {sname}: {spct * 100:.1f}%" if spct < 2 else f"  {sname}: {spct:.1f}%"
                )
        elif isinstance(sectors, dict):
            for sec, pct in list(sectors.items())[:8]:
                if sec == "breakdown":
                    continue
                val2 = (
                    pct
                    if isinstance(pct, (int, float))
                    else (pct.get("weight_pct", 0) if isinstance(pct, dict) else 0)
                )
                lines.append(f"  {sec}: {val2:.1f}%")

    if alerts:
        lines.append("")
        lines.append("Notable alerts:")
        for a in alerts[:3]:
            if isinstance(a, dict):
                msg = a.get("message", a.get("description", str(a)))
                lines.append(f"  ! {msg[:100]}")

    return "\n".join(lines)


def _summarize_analysis(data: dict) -> str:
    """Summarize portfolio analysis with full top-10 holdings."""
    return _summarize_with_top10(data, label="PORTFOLIO ANALYSIS")


def _summarize_synthesize(data: dict) -> str:
    """Summarize multi-factor synthesis output with full top-10 holdings."""
    return _summarize_with_top10(data, label="MULTI-FACTOR SYNTHESIS")


def _summarize_fixed_income(data: dict) -> str:
    """Summarize fixed-income analysis output."""
    d = data.get("data", data)
    curve = d.get("curve_positioning", d.get("yield_curve", {}))
    duration = d.get("duration_targets", d.get("duration", {}))
    benchmarks = d.get("benchmark_comparisons", d.get("benchmarks", {}))

    lines = []
    if curve:
        lines.append(f"Curve positioning: {json.dumps(curve, default=str)[:120]}")
    if duration:
        lines.append(f"Duration targets: {json.dumps(duration, default=str)[:120]}")
    if benchmarks:
        lines.append(f"Benchmark comparisons: {json.dumps(benchmarks, default=str)[:120]}")

    return "\n".join(lines) if lines else "Fixed-income analysis results available."


def _summarize_report(data: dict) -> str:
    """Summarize export report output."""
    d = data.get("data", data)
    filename = d.get("output_file", d.get("filename", "portfolio_report"))
    total = d.get("summary", {}).get("total_value", 0)
    lines = [f"Report exported: {filename}"]
    if total:
        lines.append(f"Total portfolio value: ${total:,.0f}")
    return "\n".join(lines)


def _summarize_lookup(data: dict) -> str:
    """Summarize lookup output."""
    d = data.get("data", data)
    symbol = d.get("symbol", "???")
    price = d.get("current_price", d.get("price", 0))
    sector = d.get("sector", "Unknown")
    consensus = d.get("consensus", "")
    lines = [f"Symbol: {symbol}", f"Price: ${price:.2f}", f"Sector: {sector}"]
    if consensus:
        lines.append(f"Consensus: {consensus}")
    return "\n".join(lines)


def _summarize_session(data: dict) -> str:
    """Summarize session init output."""
    d = data.get("data", data)
    risk = d.get("risk_profile", d.get("heat_level", "Unknown"))
    horizon = d.get("investment_horizon", d.get("horizon", "Unknown"))
    return f"Risk profile: {risk}\nInvestment horizon: {horizon}"


def _summarize_eod_report(data: dict) -> str:
    """Summarize end-of-day report output."""
    d = data.get("data", data)
    pnl = d.get("daily_pnl", d.get("portfolio_pnl", 0))
    biggest_up = d.get("biggest_gainer", d.get("top_mover_up", {}))
    biggest_down = d.get("biggest_loser", d.get("top_mover_down", {}))

    lines = [f"Daily P&L: {pnl:+.2f}%" if isinstance(pnl, (int, float)) else f"Daily P&L: {pnl}"]
    if biggest_up:
        sym = biggest_up.get("symbol", "???")
        chg = biggest_up.get("change_pct", biggest_up.get("return_pct", 0))
        lines.append(f"Biggest gainer: {sym} ({chg:+.1f}%)")
    if biggest_down:
        sym = biggest_down.get("symbol", "???")
        chg = biggest_down.get("change_pct", biggest_down.get("return_pct", 0))
        lines.append(f"Biggest loser: {sym} ({chg:+.1f}%)")

    return "\n".join(lines)


_SUMMARIZERS: dict[str, Any] = {
    "holdings": _summarize_holdings,
    "performance": _summarize_performance,
    "analyst": _summarize_analyst,
    "news": _summarize_news,
    "bonds": _summarize_bonds,
    "analysis": _summarize_analysis,
    "synthesize": _summarize_synthesize,
    "fixed-income": _summarize_fixed_income,
    "report": _summarize_report,
    "lookup": _summarize_lookup,
    "session": _summarize_session,
    "eod-report": _summarize_eod_report,
}


_TICKER_RE = _re.compile(r"\b([A-Z]{2,5})\b")  # 2+ chars — drops G/L single-letter noise
_TICKER_STOPWORDS = {
    # Common all-caps financial abbreviations that are not tickers
    "ETF",
    "YTD",
    "BUY",
    "SELL",
    "HOLD",
    "STRONG",
    "DATA",
    "TOP",
    "GL",
    "THE",
    "AND",
    "FOR",
    "NOT",
    "ALL",
    "NEW",
    "NET",
    "AVG",
    "MAX",
    "MIN",
    "USD",
    "NAV",
    "AUM",
    "IPO",
    "CEO",
    "CFO",
    "EPS",
    "ROE",
    "ROA",
    "PE",
    "TIPS",
    "YTM",
    "OAS",
    "WAL",
    "DV01",
    "CUSIP",
    "PNL",
    "TOTAL",
    "SECTOR",
    "EQUITY",
    "BONDS",
    "CASH",
    "BOND",
    "BREAKDOWN",
    "POSITIONS",
    "PORTFOLIO",
    "HOLDINGS",
    "VALUE",
    "ANALYST",
    "CONSENSUS",
    "TARGET",
    "RATING",
    "COUNT",
    "TECHNOLOGY",
    "HEALTHCARE",
    "INDUSTRIALS",
    "FINANCIAL",
    "SERVICES",
    "CONSUMER",
    "MATERIALS",
    "ENERGY",
    "UTILITIES",
    "REAL",
    "ESTATE",
}


def extract_tickers_from_summary(data_summary: str) -> list:
    """Extract likely ticker symbols from a narration data summary string.

    Requires 2+ uppercase characters to drop G/L noise. Uses a stopword
    blocklist for common financial abbreviations. Returns a deduplicated
    list in order of first appearance.
    """
    seen: dict = {}
    for m in _TICKER_RE.finditer(data_summary):
        t = m.group(1)
        if t not in _TICKER_STOPWORDS and t not in seen:
            seen[t] = None
    return list(seen.keys())


def summarize_for_narration(command: str, reports_dir: Path) -> Optional[str]:
    """Load compact output and produce a text summary for LLM narration.

    Returns None if no data can be loaded.
    """
    canonical = _ALIAS_TO_CANONICAL.get(command, command)
    output_file = _COMMAND_OUTPUT_FILES.get(canonical, "")
    if not output_file or not output_file.endswith(".json"):
        return None

    # Resolution order: dated subdir → flat base dir → holdings_summary variant
    import datetime as _dt

    today_str = _dt.date.today().isoformat()
    candidates = [
        reports_dir / today_str / output_file,  # dated: portfolio_reports/2026-04-14/file.json
        reports_dir / output_file,  # flat:  portfolio_reports/file.json
    ]
    if canonical == "holdings":
        candidates += [
            reports_dir / today_str / "holdings_summary.json",
            reports_dir / "holdings_summary.json",
        ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return None

    data = _load_json(path)
    if not data:
        return None

    # analysis/synthesize JSON doesn't carry per-holding breakdown.
    # Inject top_equity from holdings_summary.json (the compact holdings file).
    if canonical in ("analysis", "synthesize"):
        for h_path in [
            reports_dir / today_str / "holdings_summary.json",
            reports_dir / "holdings_summary.json",
            reports_dir / today_str / "holdings.json",
            reports_dir / "holdings.json",
        ]:
            if h_path.exists():
                h_raw = _load_json(h_path)
                if h_raw:
                    # holdings_summary.json is flat (no "data" wrapper)
                    h_inner = h_raw.get("data", h_raw)
                    top_eq = h_inner.get("top_equity", [])
                    if top_eq:
                        data["top_equity"] = top_eq[:10]
                        data["_holdings_summary"] = h_inner.get("summary", {})
                break

    summarizer = _SUMMARIZERS.get(canonical)
    if not summarizer:
        return None

    try:
        return summarizer(data)
    except Exception as exc:
        logger.debug("Summarizer failed for %s: %s", canonical, exc)
        return None


# ---------------------------------------------------------------------------
# Command briefings (context for the LLM about what the command does)
# ---------------------------------------------------------------------------

COMMAND_BRIEFINGS: dict[str, str] = {
    "holdings": (
        "This is a portfolio holdings snapshot -- the viewer just ran "
        "a command to see what they own, current prices, position sizes, "
        "and sector allocation."
    ),
    "performance": (
        "This is a performance analysis -- returns, gains/losses, "
        "top and bottom performers. The viewer wants to know how "
        "their portfolio has been doing."
    ),
    "analyst": (
        "This is analyst consensus data -- Wall Street ratings, "
        "buy/hold/sell recommendations, and analyst counts for "
        "the viewer's holdings."
    ),
    "news": (
        "This is a news sentiment scan -- recent headlines affecting "
        "the viewer's holdings, with positive and negative movers "
        "identified."
    ),
    "bonds": (
        "This is a bond portfolio analysis -- yields, durations, "
        "credit quality, and maturity profiles for fixed-income "
        "holdings."
    ),
    "analysis": (
        "This is a portfolio analysis -- sector allocation, "
        "diversification assessment, and risk flags for the overall "
        "portfolio."
    ),
    "synthesize": (
        "This is a multi-factor synthesis -- combining analyst "
        "ratings, news sentiment, and portfolio metrics to identify "
        "top opportunities and key risks."
    ),
    "fixed-income": (
        "This is a fixed-income strategy analysis -- yield curve "
        "positioning, duration targets, and benchmark comparisons "
        "for the bond allocation."
    ),
    "report": (
        "The viewer just exported their portfolio to a spreadsheet "
        "report. Comment on the fact they're getting organized."
    ),
    "lookup": ("The viewer looked up detail on a specific symbol. React to what they found."),
    "session": (
        "The viewer just set up their investor profile -- risk "
        "tolerance, investment horizon. Comment on their approach."
    ),
    "eod-report": (
        "This is the end-of-day portfolio report -- the day's "
        "biggest moves, overall P&L, and key market events."
    ),
}


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_lead_system_prompt(
    lead: dict,
    foil: dict,
    command: str,
    cohost_mode: str,
    previous_foil_message: Optional[str],
    ticker_allowlist: Optional[list] = None,
) -> str:
    """System prompt for the lead persona."""
    canonical = _ALIAS_TO_CANONICAL.get(command, command)
    COMMAND_BRIEFINGS.get(canonical, "The viewer ran a portfolio command.")

    parts = [
        f"You are {lead['name']}, a cable financial television personality.",
        f"Character: {lead['description']}",
        f"Voice: {lead['voice_markers']}",
        "",
        f"Your co-host today is {foil['name']}.",
        "",
        "RULES:",
        "- Stay completely in character.",
        "- Be entertaining and funny while referencing the actual data.",
        "- Keep your response to 3-5 sentences maximum.",
        "- Do NOT give actual investment advice.",
        "- Reference specific numbers from the data summary.",
        "- ONLY reference tickers, securities, and figures that appear in the DATA "
        "section. Do NOT invent or introduce any ticker, fund, or holding not "
        "present in the data — not as an example, not as a comparison, not at all.",
        "- Do NOT use markdown formatting. Plain text only.",
        "- Keep lines under 60 characters for mobile display.",
    ]

    if ticker_allowlist:
        parts.append(
            f"- PERMITTED TICKERS (the complete list — no others exist in this portfolio): "
            f"{', '.join(ticker_allowlist)}. Any ticker not on this list is fabricated and MUST NOT appear."
        )

    if cohost_mode == "clap-back" and previous_foil_message:
        parts.append(f"\nYour co-host {foil['name']} just said:")
        parts.append(f'"{previous_foil_message}"')
        parts.append("React to their take while covering the new data.")
    elif cohost_mode == "setup":
        parts.append(
            f"\nSet up your co-host {foil['name']} with a question "
            "or challenge about this data at the end of your take."
        )

    return "\n".join(parts)


def build_lead_user_prompt(
    lead: dict,
    foil: dict,
    command: str,
    data_summary: str,
    cohost_mode: str,
    previous_foil_message: Optional[str],
    message_history: list,
) -> str:
    """User prompt for the lead persona."""
    canonical = _ALIAS_TO_CANONICAL.get(command, command)
    briefing = COMMAND_BRIEFINGS.get(canonical, "The viewer ran a portfolio command.")

    # Detect whether we have a top-10 list to walk through
    has_top10 = "TOP 10 HOLDINGS" in data_summary

    if has_top10:
        delivery_instruction = (
            f"Deliver your {lead['name']} take as a full running commentary. "
            "Go through EACH of the top 10 holdings listed above ONE BY ONE — "
            "name each ticker explicitly and react to it in your character voice. "
            "Then close with a punchy overall portfolio verdict. "
            "Write 2-3 full paragraphs. Be vivid, specific, and escalating. "
            "Reference the actual dollar values, weights, and G/L percentages. "
            "Do NOT list holdings like a table — weave them into flowing commentary. "
            "Do NOT use bullet points. Plain prose paragraphs only."
        )
    else:
        delivery_instruction = (
            f"Deliver your {lead['name']} take on this data. "
            "2-3 full paragraphs in character, referencing real numbers. "
            "Be vivid, specific, and escalating in your delivery."
        )

    parts = [
        f"SEGMENT: {briefing}",
        "",
        "DATA:",
        data_summary,
        "",
        delivery_instruction,
    ]

    if previous_foil_message and cohost_mode == "clap-back":
        parts += [
            "",
            f"{foil['name']} previously said:",
            f'"{previous_foil_message}"',
            "OPEN with a one-line retort to that, then launch into your commentary.",
        ]
    elif cohost_mode == "setup":
        parts.append(f"\nEND with a tee-up for {foil['name']}.")

    return "\n".join(parts)


def build_foil_system_prompt(
    lead: dict,
    foil: dict,
    command: str,
    ticker_allowlist: Optional[list] = None,
) -> str:
    """System prompt for the foil persona."""
    parts = [
        f"You are {foil['name']}, a cable financial television personality.",
        f"Character: {foil['description']}",
        f"Voice: {foil['voice_markers']}",
        "",
        f"You are responding to your co-host {lead['name']}.",
        "",
        "RULES:",
        "- Stay completely in character.",
        "- React to what the lead just said with full paragraphs — pick apart their "
        "specific claims, mock their logic in character, then deliver your own angle.",
        "- Be entertaining, witty, and escalating. Each paragraph should land harder.",
        "- Reference the actual holdings, numbers, and arguments your co-host made.",
        "- ONLY reference tickers and securities your co-host mentioned or that appear "
        "in the original data. Do NOT invent, add, or compare against any ticker, "
        "fund, or holding not already in play — not as an example, not at all.",
        "- Write 2-3 full paragraphs. Plain prose only — no bullet points, no lists.",
        "- In your FINAL paragraph, weave in the disclaimer that this is entertainment "
        "satire and not financial advice — but deliver it ENTIRELY in your own voice. "
        "Do NOT read it like a legal disclaimer. Make it funny and in-character.",
        "- Do NOT give actual investment advice.",
        "- Do NOT use markdown formatting.",
    ]

    if ticker_allowlist:
        parts.append(
            f"- PERMITTED TICKERS (the complete list — no others exist in this portfolio): "
            f"{', '.join(ticker_allowlist)}. Any ticker not on this list is fabricated and MUST NOT appear."
        )

    return "\n".join(parts)


def build_foil_user_prompt(
    lead: dict,
    foil: dict,
    lead_commentary: str,
    foil_history: list,
) -> str:
    """User prompt for the foil persona."""
    # Cap lead_commentary to prevent context overflow when input is long
    # (single-LLM mode produces longer lead text; uncapped it eats foil token budget)
    # Flatten newlines — multi-line lead text breaks gemma4:e4b's turn-structure parsing,
    # causing it to burn all num_predict tokens on internal formatting and return empty output.
    lead_flat = " ".join(lead_commentary.split())
    lead_excerpt = lead_flat[:800] if len(lead_flat) > 800 else lead_flat

    parts = [
        f"{lead['name']} just said:",
        f'"{lead_excerpt}"',
        "",
        f"Now deliver your {foil['name']} response as 2-3 full paragraphs. "
        "Pick apart their specific claims in your character voice, escalate with each paragraph, "
        "and weave the entertainment disclaimer into your final paragraph naturally.",
    ]

    if foil_history:
        parts.append("")
        parts.append("Your previous commentary this session:")
        for msg in foil_history[-2:]:
            parts.append(f'  "{msg[:100]}"')

    return "\n".join(parts)


def build_closer_system_prompt(lead: dict, foil: dict) -> str:
    """System prompt for the eod-report closer (lead wraps up the show)."""
    return "\n".join(
        [
            f"You are {lead['name']}, wrapping up today's show.",
            f"Character: {lead['description']}",
            f"Voice: {lead['voice_markers']}",
            "",
            f"Your co-host today was {foil['name']}.",
            "",
            "RULES:",
            "- Deliver a 2-3 sentence show closer / sign-off.",
            "- Reference the day's key takeaway from the exchange.",
            "- Stay in character. Be memorable.",
            "- Do NOT introduce any ticker or holding not discussed on the show today.",
            "- Do NOT use markdown. Plain text only.",
            "- Keep lines under 60 characters.",
        ]
    )


def build_closer_user_prompt(lead_commentary: str, foil_response: str) -> str:
    """User prompt for the closer."""
    return "\n".join(
        [
            "Here's what was said on today's show:",
            f"Lead: {lead_commentary[:200]}",
            f"Foil: {foil_response[:200]}",
            "",
            "Wrap up the show with a memorable sign-off.",
        ]
    )


def build_reaction_system_prompt(lead: dict, foil: dict) -> str:
    """System prompt for the foil reaction to the closer."""
    return "\n".join(
        [
            f"You are {foil['name']}, reacting to the show closer.",
            f"Character: {foil['description']}",
            f"Voice: {foil['voice_markers']}",
            "",
            "RULES:",
            "- One sentence reaction or quip to the sign-off.",
            "- Stay in character.",
            "- Do NOT introduce any ticker or holding not discussed on the show today.",
            "- Do NOT use markdown. Plain text only.",
        ]
    )


def build_reaction_user_prompt(lead_commentary: str, foil_response: str) -> str:
    """User prompt for the reaction."""
    return (
        f'The host just signed off with: "{lead_commentary[:200]}" -- give your one-line reaction.'
    )


# ---------------------------------------------------------------------------
# LLM calls (via Ollama, matching existing ConsultationClient pattern)
# ---------------------------------------------------------------------------


def generate_narration(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 300,
    temperature: float = 0.9,
) -> str:
    """Call the configured LLM for stonkmode narration.

    Provider selection via INVESTORCLAW_NARRATIVE_PROVIDER:
      - "ollama" (default): Ollama /api/generate — local GPU host
      - "openai_compat": OpenAI-compatible /chat/completions — MiniMax, Grok, Claude, etc.

    Falls back gracefully to empty string if unavailable.
    Backward compatible with INVESTORCLAW_STONKMODE_PROVIDER.
    """
    provider = (
        os.environ.get("INVESTORCLAW_NARRATIVE_PROVIDER")
        or os.environ.get("INVESTORCLAW_STONKMODE_PROVIDER")
        or "ollama"
    ).lower()
    if provider == "openai_compat":
        return _narrate_openai(system_prompt, user_prompt, max_tokens, temperature)
    return _narrate_ollama(system_prompt, user_prompt, max_tokens, temperature)


def _narrate_ollama(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Ollama /api/generate path — defaults to operational LLM unless explicitly configured.

    Configuration order (first set wins):
      1. INVESTORCLAW_NARRATIVE_ENDPOINT / INVESTORCLAW_NARRATIVE_MODEL (explicit narrative config)
      2. INVESTORCLAW_STONKMODE_ENDPOINT / INVESTORCLAW_STONKMODE_MODEL (legacy, backward compat)
      3. INVESTORCLAW_CONSULTATION_ENDPOINT / INVESTORCLAW_CONSULTATION_MODEL (uses same as operational)
      4. Default: http://localhost:11434 / gemma4:e4b
    """
    # Narrative endpoint: prefer explicit narrative config, fallback to consultation, then defaults
    endpoint = (
        os.environ.get("INVESTORCLAW_NARRATIVE_ENDPOINT")
        or os.environ.get("INVESTORCLAW_STONKMODE_ENDPOINT")
        or os.environ.get("INVESTORCLAW_CONSULTATION_ENDPOINT")
        or "http://localhost:11434"
    )
    endpoint = endpoint.rstrip("/")

    # Narrative model: prefer explicit narrative config, fallback to consultation, then defaults
    model = (
        os.environ.get("INVESTORCLAW_NARRATIVE_MODEL")
        or os.environ.get("INVESTORCLAW_STONKMODE_MODEL")
        or os.environ.get("INVESTORCLAW_CONSULTATION_MODEL")
        or "gemma4:e4b"
    )

    payload = json.dumps(
        {
            "model": model,
            "system": system_prompt,
            "prompt": user_prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "num_ctx": 8192,
                "temperature": temperature,
            },
        }
    ).encode()

    try:
        _validate_http_url(f"{endpoint}/api/generate")
    except ValueError as exc:
        logger.debug("Stonkmode Ollama URL rejected: %s", exc)
        return ""

    req = urllib.request.Request(
        f"{endpoint}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
            result = body.get("response", "").strip()
            if result:
                return result
            # Empty response: gemma4-series models occasionally burn all num_predict
            # tokens on non-visible formatting tokens under rapid sequential inference.
            # One retry is sufficient to recover.
            if attempt == 0:
                logger.debug("Stonkmode Ollama returned empty response, retrying once")
                import time as _time

                _time.sleep(1)
        except Exception as exc:
            logger.debug("Stonkmode Ollama call failed: %s", exc)
            return ""
    return ""


def _narrate_openai(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """OpenAI-compatible /chat/completions path — Grok, Claude, GPT, etc.

    Configuration order (first set wins):
      1. INVESTORCLAW_NARRATIVE_ENDPOINT / INVESTORCLAW_NARRATIVE_MODEL (explicit narrative config)
      2. INVESTORCLAW_STONKMODE_ENDPOINT / INVESTORCLAW_STONKMODE_MODEL (legacy, backward compat)
      3. INVESTORCLAW_CONSULTATION_ENDPOINT / INVESTORCLAW_CONSULTATION_MODEL (uses same as operational)
      4. Default: https://api.openai.com/v1 / grok-3-fast

    API key uses same fallback chain but defaults to empty (some providers don't require auth).
    """
    # Use NARRATIVE_* vars (new naming), fall back to STONKMODE_*, then consultation, then defaults
    endpoint = (
        os.environ.get("INVESTORCLAW_NARRATIVE_ENDPOINT")
        or os.environ.get("INVESTORCLAW_STONKMODE_ENDPOINT")
        or os.environ.get("INVESTORCLAW_CONSULTATION_ENDPOINT")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    api_key = (
        os.environ.get("INVESTORCLAW_NARRATIVE_API_KEY")
        or os.environ.get("INVESTORCLAW_STONKMODE_API_KEY")
        or os.environ.get("INVESTORCLAW_CONSULTATION_API_KEY")
        or ""
    )
    model = (
        os.environ.get("INVESTORCLAW_NARRATIVE_MODEL")
        or os.environ.get("INVESTORCLAW_STONKMODE_MODEL")
        or os.environ.get("INVESTORCLAW_CONSULTATION_MODEL")
        or "grok-3-fast"
    )

    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    ).encode()

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        _validate_http_url(f"{endpoint}/chat/completions")
    except ValueError as exc:
        logger.debug("Stonkmode cloud LLM URL rejected: %s", exc)
        return ""

    req = urllib.request.Request(
        f"{endpoint}/chat/completions",
        data=payload,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        choices = body.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "").strip()
        return ""
    except Exception as exc:
        logger.debug("Stonkmode cloud LLM call failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def render_stonkmode_output(
    lead: dict,
    foil: dict,
    lead_commentary: str,
    foil_response: str,
    command: str,
    closer: str | None = None,
) -> None:
    """Print the formatted stonkmode block to stdout with ANSI colors."""
    R = _C["reset"]
    width = 57

    lead_arch = lead.get("archetype", "wildcards")
    foil_arch = foil.get("archetype", "wildcards")
    lead_lc = _C.get(_ARCH_LABEL.get(lead_arch, "white"), "")
    foil_lc = _C.get(_ARCH_LABEL.get(foil_arch, "white"), "")
    lead_bc = _C.get(_ARCH_BODY.get(lead_arch, ""), "")
    foil_bc = _C.get(_ARCH_BODY.get(foil_arch, ""), "")
    border = _C["grey"]
    BW = _C["bold"] + _C["white"]

    header = f"  STONKMODE  {lead['name']} ▸ {foil['name']}"
    if len(header) > width - 2:
        header = header[: width - 5] + "..."

    print()
    print(f"{border}┌{'─' * width}┐{R}")
    print(f"{border}│{BW}{header:<{width}}{R}{border}│{R}")
    print(f"{border}└{'─' * width}┘{R}")
    print()

    # Lead block
    lead_label = lead["name"].upper()
    if len(lead_label) > 50:
        lead_label = lead_label[:47] + "..."
    print(f"{_C['bold']}{lead_lc}▌ {lead_label}{R}")
    _print_wrapped(lead_commentary, width=58, body_color=lead_bc)
    print()

    # Foil block
    foil_label = foil["name"].upper()
    if len(foil_label) > 50:
        foil_label = foil_label[:47] + "..."
    print(f"{_C['bold']}{foil_lc}▌ {foil_label}{R}")
    _print_wrapped(foil_response, width=58, body_color=foil_bc)
    print()

    # Closer (lead wraps up the show — use lead color, dimmer)
    if closer:
        print(f"{_C['dim']}{lead_lc}▌ SIGN-OFF{R}")
        _print_wrapped(closer, width=58, body_color=_C["dim"])
        print()

    # Footer
    print(f"{border}{'─' * width}{R}")
    print(f"{_C['footer']}  ⚠  Seriously silly. The math is real. The people are not.{R}")
    print(f"{_C['footer']}  ⚠  Not your FA. Not a substitute for your FA. Talk to your FA.{R}")
    print()


def _apply_emphasis(line: str, body_color: str = "") -> str:
    """Bold ALL-CAPS words (≥3 chars) for humor emphasis. Restore body_color after."""
    R = _C["reset"]
    B = _C["bold"]

    def _bold_caps(m: _re.Match) -> str:
        word = m.group()
        if body_color:
            return f"{R}{B}{word}{R}{body_color}"
        return f"{B}{word}{R}"

    emphasized = _CAPS_RE.sub(_bold_caps, line)
    if body_color:
        return f"{body_color}{emphasized}{R}"
    return emphasized


def _print_wrapped(text: str, width: int = 58, body_color: str = "") -> None:
    """Print text with word-wrapping and per-word ANSI emphasis for ALL-CAPS."""
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            print()
            continue
        # Wrap on plain text so len() counts are accurate (no invisible ANSI bytes)
        words = paragraph.split()
        line = ""
        for word in words:
            if line and len(line) + 1 + len(word) > width:
                print(_apply_emphasis(line, body_color))
                line = word
            else:
                line = f"{line} {word}" if line else word
        if line:
            print(_apply_emphasis(line, body_color))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def maybe_narrate(command: str, skill_dir: Path) -> None:
    """Called after a successful command run.

    If stonkmode is enabled, reads the compact output, generates
    narration, and renders to stdout. Updates session_message_history
    in state.
    """
    state = load_state()
    if not state or not state.get("enabled"):
        return

    lead_id = state.get("lead_id")
    foil_id = state.get("foil_id")
    if not lead_id or not foil_id:
        return

    # Resolve canonical command
    canonical = _ALIAS_TO_CANONICAL.get(command, command)
    if canonical not in _COMMAND_OUTPUT_FILES:
        return

    # Skip commands with no JSON output
    output_file = _COMMAND_OUTPUT_FILES.get(canonical, "")
    if not output_file or not output_file.endswith(".json"):
        # report / lookup / eod-report with no standard file
        if canonical not in ("report", "lookup", "eod-report"):
            return

    try:
        lead = get_persona(lead_id)
        foil = get_persona(foil_id)
    except KeyError:
        return

    reports_dir = _get_reports_dir()
    data_summary = summarize_for_narration(command, reports_dir)
    if not data_summary:
        # For commands without JSON output, provide a minimal summary
        if canonical in ("report",):
            data_summary = "The viewer exported their portfolio report."
        elif canonical in ("lookup",):
            data_summary = "The viewer looked up a specific symbol."
        else:
            return

    # Session history
    message_history = state.get("session_message_history", [])
    foil_history = [m.get("foil", "") for m in message_history if m.get("foil")]

    # Previous foil message for clap-back mode
    previous_foil = foil_history[-1] if foil_history else None
    cohost_mode = select_cohost_mode(has_previous_foil_message=bool(previous_foil))

    # Build prompts and generate lead commentary — timed for the JSON envelope
    import time as _time

    t0 = _time.monotonic()

    # Extract tickers present in this data to enforce data-grounding in both prompts.
    # Injected as an explicit positive allowlist — "do not invent" alone is insufficient;
    # models hallucinate well-known tickers (e.g. NVDA) from persona priors.
    ticker_allowlist = extract_tickers_from_summary(data_summary)

    lead_sys = build_lead_system_prompt(
        lead, foil, command, cohost_mode, previous_foil, ticker_allowlist
    )
    lead_user = build_lead_user_prompt(
        lead,
        foil,
        command,
        data_summary,
        cohost_mode,
        previous_foil,
        message_history,
    )
    lead_commentary = generate_narration(lead_sys, lead_user, max_tokens=1000)
    if not lead_commentary:
        print(
            json.dumps(
                {
                    "stonkmode_narration": {
                        "is_entertainment": True,
                        "is_satire": True,
                        "is_investment_advice": False,
                        "consultation_mode": "deactivated",
                        "error": "narration_unavailable",
                        "error_detail": "LLM offline or unreachable",
                    }
                }
            )
        )
        return

    # Generate foil response
    foil_sys = build_foil_system_prompt(lead, foil, command, ticker_allowlist)
    foil_user = build_foil_user_prompt(lead, foil, lead_commentary, foil_history)
    foil_response = generate_narration(foil_sys, foil_user, max_tokens=900)
    if not foil_response:
        foil_response = "(no response)"

    # For eod-report: run closer pipeline (65% closer, 35% reaction)
    closer = None
    if canonical == "eod-report":
        roll = random.random()
        if roll < 0.65:
            closer_sys = build_closer_system_prompt(lead, foil)
            closer_user = build_closer_user_prompt(lead_commentary, foil_response)
            closer = generate_narration(closer_sys, closer_user, max_tokens=150)
        else:
            reaction_sys = build_reaction_system_prompt(lead, foil)
            reaction_user = build_reaction_user_prompt(lead_commentary, foil_response)
            closer = generate_narration(reaction_sys, reaction_user, max_tokens=100)

    inference_ms = int((_time.monotonic() - t0) * 1000)

    # Provider: prefer explicit narrative config, fallback to consultation config (operational LLM)
    _provider = (
        os.environ.get("INVESTORCLAW_NARRATIVE_PROVIDER")
        or os.environ.get("INVESTORCLAW_STONKMODE_PROVIDER")
        or os.environ.get("INVESTORCLAW_CONSULTATION_PROVIDER")
        or "ollama"
    ).lower()
    if _provider == "openai_compat":
        _endpoint = (
            os.environ.get("INVESTORCLAW_NARRATIVE_ENDPOINT")
            or os.environ.get("INVESTORCLAW_STONKMODE_ENDPOINT")
            or os.environ.get("INVESTORCLAW_CONSULTATION_ENDPOINT")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        model = (
            os.environ.get("INVESTORCLAW_NARRATIVE_MODEL")
            or os.environ.get("INVESTORCLAW_STONKMODE_MODEL")
            or os.environ.get("INVESTORCLAW_CONSULTATION_MODEL")
            or "grok-3-fast"
        )
    else:
        _endpoint = (
            os.environ.get("INVESTORCLAW_NARRATIVE_ENDPOINT")
            or os.environ.get("INVESTORCLAW_STONKMODE_ENDPOINT")
            or os.environ.get("INVESTORCLAW_CONSULTATION_ENDPOINT")
            or "http://localhost:11434"
        ).rstrip("/")
        model = (
            os.environ.get("INVESTORCLAW_NARRATIVE_MODEL")
            or os.environ.get("INVESTORCLAW_STONKMODE_MODEL")
            or os.environ.get("INVESTORCLAW_CONSULTATION_MODEL")
            or "gemma4:e4b"
        )
    endpoint = _endpoint

    # Extract financial terms from narration for Dr. Stonk educational box
    narration_text = f"{lead_commentary} {foil_response}"
    if closer:
        narration_text += f" {closer}"
    terms_in_narration = extract_terms_used({"narration": narration_text})
    dr_stonk_box = build_dr_stonk_box(terms_in_narration) if is_dr_stonk_enabled() else ""

    # Emit structured JSON for the agent to consume and present.
    # consultation_mode: "deactivated" — HMAC, fingerprint, synthesis_basis,
    # and verbatim_required rules DO NOT APPLY to this block.
    print(
        json.dumps(
            {
                "stonkmode_narration": {
                    "is_entertainment": True,
                    "is_satire": True,
                    "is_investment_advice": False,
                    "consultation_mode": "deactivated",
                    "satire_disclaimer": (
                        "STONKMODE \u2014 Seriously silly. "
                        "AI-generated entertainment satire — not analysis, not advice. "
                        "The math is real. The analysis is real. The people are not. "
                        "Fictional cable TV characters only. "
                        "Not financial advice. Not a substitute for your actual financial advisor, "
                        "who went to school for this and does not have a catchphrase. "
                        "Do not YOLO your retirement account based on anything said above."
                    ),
                    "lead": {
                        "id": lead_id,
                        "name": lead["name"],
                        "archetype": lead["archetype"],
                    },
                    "foil": {
                        "id": foil_id,
                        "name": foil["name"],
                        "archetype": foil["archetype"],
                    },
                    "pairing_dynamic": state.get("pairing_dynamic", ""),
                    "command": command,
                    "cohost_mode": cohost_mode,
                    "narration": {
                        "lead": lead_commentary,
                        "foil": foil_response,
                        "closer": closer,
                    },
                    "model": f"{model}@{endpoint}",
                    "inference_ms": inference_ms,
                    "dr_stonk": {
                        "enabled": is_dr_stonk_enabled(),
                        "terms_explained": sorted(terms_in_narration)
                        if is_dr_stonk_enabled()
                        else [],
                        "box": dr_stonk_box,
                    }
                    if is_dr_stonk_enabled() and terms_in_narration
                    else None,
                }
            }
        )
    )

    # Update session history (keep last 5)
    segment_count = state.get("segment_count", 0) + 1
    message_history.append(
        {
            "command": command,
            "lead": lead_commentary[:200],
            "foil": foil_response[:200],
        }
    )
    if len(message_history) > 5:
        message_history = message_history[-5:]

    state["segment_count"] = segment_count
    state["session_message_history"] = message_history
    save_state(state)
