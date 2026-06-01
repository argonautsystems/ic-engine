# SPDX-License-Identifier: Apache-2.0
"""
InvestorClaw v4.x dashboard — server-rendered tabbed UI on :18092.

Each tab is its own FastAPI route returning a complete HTML page.
Section content reuses the engine's render helpers from
``ic_engine.rendering.eod_email_template`` so the dashboard tabs and the
EOD email render with identical styling and data shapes.

Tabs:
    /                     Overview — status + quick actions
    /dashboard/holdings   Top holdings + asset / sector breakdown
    /dashboard/performance Sharpe + Sortino + drawdown + period
    /dashboard/whatchanged Day-over-day attribution + top movers
    /dashboard/scenarios  Stress tests + drawdown / VaR
    /dashboard/bonds      Yield-to-maturity + duration + bond ladder
    /dashboard/analyst    Analyst consensus + price-target spread
    /dashboard/news       Per-holding headlines (with clickable links)
    /dashboard/synthesis  Multi-factor advisor narrative
    /dashboard/reports    Archive of generated EOD HTML reports
    /dashboard/settings   API-key management + provider config
    /dashboard/about      Version + license + disclaimer
"""
from __future__ import annotations

import csv as _csv
import datetime as _dt
import functools
import glob as _glob
import json as _json
import os
import pathlib
import re
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse


REPORTS_DIR = os.environ.get("IC_REPORTS_DIR", "/data/reports")

# Hard cap on portfolio uploads.
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_ALLOWED_UPLOAD_SUFFIXES = frozenset({
    ".csv", ".tsv", ".xls", ".xlsx", ".pdf", ".json", ".ofx", ".qfx"
})


def _running_version() -> str:
    """Read the running ic-engine version (set by Dockerfile in v4.1.39+).

    Falls back to a generic "v4.1.x" placeholder if the env var is unset
    so the dashboard renders cleanly outside a container.
    """
    return os.environ.get("IC_ENGINE_VERSION") or "v4.1.x"

# (slug, label, icon)
# Coverage map (cobol nlq-prompts.json v2.5.0 — 30 NLQs):
#   Overview p23/p24 | Holdings p01/p02/p11/p28 | Performance p03/p04
#   What Changed (delta) | Scenarios | Bonds p14/p15 | Analyst p05
#   News p06/p16/p19 | Markets p17/p18/p21/p22 | Lookup p27
#   Optimize p09/p10/p12/p13 | Cashflow p25 | Peer p26
#   Synthesis p07/p08 | Reports p23/p24 | Settings | About p20/p29/p30
TABS = [
    ("", "Overview", "📊"),
    ("holdings", "Holdings", "📁"),
    ("performance", "Performance", "📈"),
    ("whatchanged", "What Changed", "Δ"),
    ("scenarios", "Scenarios", "⚡"),
    ("bonds", "Bonds", "🏛"),
    ("optimize", "Optimize", "🎚"),
    ("cashflow", "Cashflow", "💵"),
    ("peer", "Peer", "⚖"),
    ("analyst", "Analyst", "🎯"),
    ("news", "News", "📰"),
    ("markets", "Markets", "🌐"),
    ("lookup", "Lookup", "🔎"),
    ("synthesis", "Synthesis", "✦"),
    ("reports", "Reports", "📄"),
    ("settings", "Settings", "⚙"),
    ("about", "About", "ℹ"),
]


def _try_engine_helpers():
    """Import the engine's render helpers; return a stub set if unavailable."""
    try:
        from ic_engine.rendering import eod_email_template as t
        return t
    except ImportError:
        return None


_T = _try_engine_helpers()


@functools.lru_cache(maxsize=1)
def _load_cusip_names() -> dict:
    """Build CUSIP → cleaned bond name from uploaded portfolio CSVs."""
    result: dict = {}
    try:
        data_dir = pathlib.Path(REPORTS_DIR).parent  # /data
        for csv_path in _glob.glob(str(data_dir / "portfolios" / "*.csv")):
            try:
                with open(csv_path) as fh:
                    lines = fh.readlines()
                if len(lines) < 2:
                    continue
                reader = _csv.DictReader(lines[1:])
                for row in reader:
                    sym = row.get("SYMBOL", "").strip()
                    cusip = row.get("CUSIP", "").strip()
                    desc = row.get("DESCRIPTION", "").strip()
                    if sym == "N/A" and len(cusip) == 9 and cusip not in ("N/A", ""):
                        # Strip UBS "BE/R/ RATE x% MATURES date" suffix
                        clean = re.sub(r"\s+BE[/]R[/].*", "", desc)
                        clean = re.sub(r"\s+MATURES.*", "", clean)
                        clean = re.sub(r"\s*\([0-9A-Z]{9}\).*", "", clean)
                        clean = clean.strip().title()
                        # Fix common state/abbreviation casing
                        for abbr in ("SC","TX","GA","IL","CA","CO","WA","NY","NJ","FL","PA","OH","MI","NC","VA","AZ","OR","WI","MN","NV","MD","MA"):
                            clean = re.sub(rf"\b{abbr.title()}\b", abbr, clean)
                        result[cusip] = clean
            except Exception:
                pass
    except Exception:
        pass
    return result


def _cusip_to_name(cusip: str) -> str:
    """Return human-readable bond name for a CUSIP, or empty string."""
    return _load_cusip_names().get(cusip, "")


def _load_json(filename: str) -> Dict[str, Any]:
    path = pathlib.Path(REPORTS_DIR) / filename
    if not path.is_file():
        return {}
    try:
        return _json.loads(path.read_text())
    except Exception:
        return {}


def _coerce_position_count(value) -> int:
    """position_count can be a scalar (older engine) or a dict-by-asset-class
    (current engine writes ``{'equity': 215, 'bond': 38, 'cash': 16, ...}``).
    Sum the dict values; fall back to 0 if neither shape applies.
    """
    if isinstance(value, dict):
        try:
            return sum(int(v or 0) for v in value.values())
        except (TypeError, ValueError):
            return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _shell(active_slug: str, body: str, title: str = "InvestorClaw") -> str:
    """Wrap content in the standard dashboard shell with tab navigation."""
    nav_items = []
    for slug, label, icon in TABS:
        href = "/" if slug == "" else f"/dashboard/{slug}"
        active_cls = " active" if slug == active_slug else ""
        nav_items.append(
            f'<a href="{href}" class="tab{active_cls}">'
            f'<span class="tab-icon">{icon}</span>{label}</a>'
        )
    nav = "".join(nav_items)
    today = _dt.date.today().isoformat()
    version = _running_version()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — InvestorClaw</title>
<style>
:root {{ color-scheme: dark light; }}
* {{ box-sizing: border-box; }}
body {{
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  margin: 0; padding: 0;
  background: #0d1117; color: #c9d1d9;
}}
@media (prefers-color-scheme: light) {{
  body {{ background: #ffffff; color: #24292f; }}
  header {{ background: #f6f8fa; border-bottom-color: #d0d7de; }}
  header h1 {{ color: #1f2328; }}
  header .meta {{ color: #57606a; }}
  nav {{ background: #f6f8fa; border-bottom-color: #d0d7de; }}
  .tab {{ color: #57606a; }}
  .tab:hover {{ color: #24292f; }}
  h2, h3 {{ color: #1f2328; }}
  .muted {{ color: #57606a; }}
  code {{ background: #eaeef2; color: #24292f; }}
  li {{ border-bottom-color: #d0d7de; }}
  .kpi {{ background: #f6f8fa; border-color: #d0d7de; }}
  .kpi-label {{ color: #57606a; }}
  .kpi-value {{ color: #1f2328; }}
  .empty {{ background: #f6f8fa; border-color: #d0d7de; color: #57606a; }}
  .section-card {{ background: #f6f8fa; border-color: #d0d7de; }}
  th {{ background: #eaeef2; color: #1f2328; }}
  td {{ border-bottom-color: #d0d7de; }}
  form input[type="text"], form input[type="password"], form select, form input[type="file"] {{
    background: #ffffff; border-color: #d0d7de; color: #24292f;
  }}
  .alert-critical {{ background: #ffebe9; border-color: #ff8182; }}
  .alert-high {{ background: #fff8c5; border-color: #d4a72c; }}
  .alert-medium {{ background: #fff8c5; border-color: #d4a72c; }}
  .alert-low {{ background: #f6f8fa; border-color: #d0d7de; }}
  .alert-info {{ background: #ddf4ff; border-color: #54aeff; }}
  .question {{ background: #f6f8fa; border-color: #d0d7de; }}
  .question-title {{ color: #1f2328; }}
  tr:nth-child(even) td {{ background: #f6f8fa; }}
}}
header {{
  background: #161b22; border-bottom: 1px solid #30363d;
  padding: 16px 32px; display: flex; justify-content: space-between; align-items: center;
}}
header h1 {{ font-size: 18px; margin: 0; color: #f0f6fc; }}
header .meta {{ color: #8b949e; font-size: 12px; }}
nav {{
  background: #161b22; border-bottom: 1px solid #30363d;
  padding: 0 16px; overflow-x: auto; white-space: nowrap;
}}
.tab {{
  display: inline-block; padding: 12px 16px; color: #8b949e;
  text-decoration: none; border-bottom: 3px solid transparent;
  transition: color 0.1s, border-color 0.1s;
}}
.tab:hover {{ color: #c9d1d9; }}
.tab.active {{ color: #58a6ff; border-bottom-color: #58a6ff; }}
.tab-icon {{ margin-right: 6px; opacity: 0.85; }}
main {{ padding: 24px 32px; max-width: 1200px; margin: 0 auto; }}
h2 {{ margin: 0 0 16px; font-size: 18px; color: #f0f6fc; }}
.muted {{ color: #8b949e; font-size: 13px; }}
a {{ color: #58a6ff; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
a.primary {{
  display: inline-block; background: #238636; color: #fff;
  padding: 8px 14px; border-radius: 6px; font-weight: 600;
  margin: 4px 0;
}}
a.primary:hover {{ background: #2ea043; text-decoration: none; }}
code {{
  background: #161b22; padding: 2px 6px; border-radius: 3px;
  font-size: 12px; color: #c9d1d9;
}}
ul {{ list-style: none; padding: 0; margin: 0; }}
li {{ padding: 6px 0; border-bottom: 1px solid #21262d; }}
li:last-child {{ border-bottom: none; }}
.kpi-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px; margin: 12px 0;
}}
.kpi {{
  background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  padding: 14px;
}}
.kpi-label {{ color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }}
.kpi-value {{ color: #c9d1d9; font-size: 18px; font-weight: 600; margin-top: 4px; }}
.kpi-positive {{ color: #3fb950; }}
.kpi-negative {{ color: #f85149; }}
.empty {{
  background: #161b22; border: 1px dashed #30363d; border-radius: 6px;
  padding: 24px; text-align: center; color: #8b949e;
}}
.section-card {{
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 16px; margin-bottom: 16px;
}}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{
  text-align: left; padding: 8px 12px; background: #21262d;
  color: #c9d1d9; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.04em; font-weight: 600;
}}
td {{ padding: 6px 12px; border-bottom: 1px solid #21262d; }}
form .row {{ margin-bottom: 12px; }}
form label {{ display: block; color: #8b949e; font-size: 12px; margin-bottom: 4px; }}
form input[type="text"], form input[type="password"] {{
  background: #0d1117; border: 1px solid #30363d; border-radius: 4px;
  padding: 8px 12px; color: #c9d1d9; width: 360px; font-size: 13px;
}}
form select {{
  background: #0d1117; border: 1px solid #30363d; border-radius: 4px;
  padding: 8px 12px; color: #c9d1d9; width: 360px; font-size: 13px;
}}
form input[type="file"] {{
  background: #0d1117; border: 1px solid #30363d; border-radius: 4px;
  padding: 6px; color: #c9d1d9; font-size: 13px;
}}
form.inline {{ display: inline-block; margin: 0; }}
form.inline button.regen {{
  background: #1f6feb; color: #fff; border: 0; padding: 8px 14px;
  border-radius: 6px; font-weight: 600; cursor: pointer; margin-left: 8px;
}}
form.inline button.regen:hover {{ background: #388bfd; }}
form button {{
  background: #238636; color: #fff; border: 0; padding: 8px 14px;
  border-radius: 6px; font-weight: 600; cursor: pointer;
}}
form button:hover {{ background: #2ea043; }}
.footer {{ margin-top: 48px; color: #8b949e; font-size: 12px; padding: 16px 0; border-top: 1px solid #21262d; }}

/* Tablet + mobile (≤768px) — collapse padding, allow wide tables to scroll
   horizontally inside their card, make form inputs full-width, stack KPI
   grid more aggressively. */
@media (max-width: 768px) {{
  header {{ padding: 12px 16px; }}
  header h1 {{ font-size: 16px; }}
  header .meta {{ font-size: 11px; }}
  nav {{ padding: 0 8px; }}
  .tab {{ padding: 10px 12px; font-size: 13px; }}
  .tab-icon {{ margin-right: 4px; }}
  main {{ padding: 16px; max-width: 100%; }}
  h2 {{ font-size: 16px; }}
  .footer {{ padding-left: 16px !important; padding-right: 16px !important; }}
  .kpi-grid {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
  .kpi {{ padding: 10px; }}
  .kpi-value {{ font-size: 16px; }}
  .section-card {{ padding: 12px; overflow-x: auto; }}
  form input[type="text"],
  form input[type="password"] {{ width: 100%; max-width: 100%; }}
  form input[type="file"] {{ width: 100%; }}
  form select {{ width: 100%; }}
  /* Multi-input forms inside section-cards (the primary settings forms
     — save key, backup, restore, upload, routing, lookup) get a
     full-width submit button on mobile for a comfortable tap target.
     Compact per-row buttons (delete-key in keys table, load-template
     in template cards, regenerate inline form) are NOT direct children
     of a section-card and keep their natural width. */
  .section-card > form > button[type="submit"] {{ width: 100%; max-width: 360px; }}
  form.inline {{ display: block; margin-top: 8px; }}
  form.inline button.regen {{ margin-left: 0; margin-top: 8px; }}
  table {{ font-size: 12px; }}
  th, td {{ padding: 6px 8px; }}
}}

/* Phone-narrow (≤480px) — single-column KPI grid, tighter typography. */
@media (max-width: 480px) {{
  .kpi-grid {{ grid-template-columns: 1fr; }}
  header h1 {{ font-size: 15px; }}
  header .meta {{ display: none; }}
  .tab {{ padding: 10px 10px; }}
  main {{ padding: 12px; }}
}}
</style>
</head>
<body>

<header>
  <h1>InvestorClaw</h1>
  <span class="meta">{today} · {_h(version)}</span>
</header>

<nav>{nav}</nav>

<main>
{body}
</main>

<div class="footer" style="padding-left:32px;padding-right:32px;max-width:1200px;margin:0 auto;">
  Educational only — not investment advice ·
  <a href="https://github.com/argonautsystems/InvestorClaw">project home</a> ·
  <a href="https://github.com/mnemos-os/mnemos-ic-runtime">runtime</a>
</div>

</body>
</html>"""


def _section_or_empty(rendered: str, empty_msg: str) -> str:
    """Return rendered HTML or an empty-state placeholder."""
    if rendered and rendered.strip():
        return rendered
    return f'<div class="empty">{empty_msg}</div>'


def _list_recent_eod_reports(limit: int = 30) -> list:
    """Return list of (filename, size_kb, mtime_iso) for recent EOD HTML files."""
    if not os.path.isdir(REPORTS_DIR):
        return []
    out = []
    for path in sorted(
        _glob.glob(os.path.join(REPORTS_DIR, "eod_report_*.html")), reverse=True
    )[:limit]:
        fname = os.path.basename(path)
        size_kb = os.path.getsize(path) / 1024
        mtime = _dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime(
            "%Y-%m-%d %H:%M"
        )
        out.append((fname, size_kb, mtime))
    return out


# ----------------------------------------------------------------------------
# Per-tab handlers
# ----------------------------------------------------------------------------


def _overview(get_init_state, message: str = "") -> str:
    snap = get_init_state()
    today = _dt.date.today().isoformat()
    today_compact = today.replace("-", "")
    eod_today_path = os.path.join(REPORTS_DIR, f"eod_report_{today_compact}.html")
    has_today = os.path.isfile(eod_today_path)
    msg_html = (
        f'<div class="section-card" style="border-color:#3fb950;color:#3fb950;">{_h(message)}</div>'
        if message else ""
    )

    holdings = _load_json("holdings_summary.json")
    # holdings_summary.json shape: top-level keys include "summary" which holds
    # total_value / net_value / equity_value / bond_value / cash_value, plus
    # *_pct columns. Some legacy variants nest it under "data".
    summary = holdings.get("summary") or holdings.get("data", {}) or {}
    total_value = float(summary.get("total_value") or summary.get("net_value") or 0)
    equity_value = float(summary.get("equity_value", 0) or 0)
    bond_value = float(summary.get("bond_value", 0) or 0)
    cash_value = float(summary.get("cash_value", 0) or 0)
    position_count = _coerce_position_count(summary.get("position_count", 0))

    today_card = (
        f'<a href="/reports/eod_report_{today_compact}.html" class="primary">'
        f'Open today\'s EOD report ({today})</a>'
        if has_today
        else f'<p class="muted">No EOD report for {today} yet.</p>'
    )

    recent = _list_recent_eod_reports(7)
    recent_html = (
        "<ul>"
        + "\n".join(
            f'<li><a href="/reports/{fname}">{fname}</a> '
            f'<span class="muted">— {kb:.0f} KB · {mtime}</span></li>'
            for fname, kb, mtime in recent
        )
        + "</ul>"
        if recent
        else '<p class="muted">No reports generated yet. Run the pipeline.</p>'
    )

    body = f"""
{msg_html}
<h2>Status
  <form class="inline" action="/dashboard/regenerate" method="post">
    <button class="regen" type="submit" title="Refresh all sections + re-run analyzers (~3-5 min)">↻ Regenerate</button>
  </form>
</h2>
<div class="kpi-grid">
  <div class="kpi"><div class="kpi-label">Engine</div><div class="kpi-value">{snap['state']}</div></div>
  <div class="kpi"><div class="kpi-label">Init ready</div><div class="kpi-value">{'yes' if snap['ready'] else 'no'}</div></div>
  <div class="kpi"><div class="kpi-label">Total Value</div><div class="kpi-value">${total_value:,.0f}</div></div>
  <div class="kpi"><div class="kpi-label">Positions</div><div class="kpi-value">{position_count}</div></div>
</div>

<h2>Allocation</h2>
<div class="kpi-grid">
  <div class="kpi"><div class="kpi-label">Equity</div><div class="kpi-value">${equity_value:,.0f}</div></div>
  <div class="kpi"><div class="kpi-label">Bonds</div><div class="kpi-value">${bond_value:,.0f}</div></div>
  <div class="kpi"><div class="kpi-label">Cash</div><div class="kpi-value">${cash_value:,.0f}</div></div>
  <div class="kpi"><div class="kpi-label">MCP endpoint</div><div class="kpi-value"><code>:18090/mcp</code></div></div>
</div>

<h2>Today's EOD report</h2>
{today_card}

<h2>Recent EOD reports</h2>
{recent_html}

<h2>Quick links</h2>
<ul>
  <li><a href="/reports/">Browse all reports</a></li>
  <li><a href="/healthz">/healthz</a> · liveness JSON</li>
  <li><a href="/api/version">/api/version</a> · bridge version</li>
</ul>
"""
    return _shell("", body, title="Overview")


def _holdings_tab() -> str:
    """Full holdings detail — KPIs, sector breakdown, accounts, every position."""
    summary_doc = _load_json("holdings_summary.json")
    raw_doc = _load_json(".raw/holdings.json")
    analyst = _load_json("analyst_recommendations_summary.json")

    if not summary_doc:
        body = ('<h2>Holdings</h2><div class="empty">No holdings data yet. '
                "Drop a portfolio file into <code>./portfolios/</code> and "
                "run <code>investorclaw setup</code> in the container.</div>")
        return _shell("holdings", body, title="Holdings")

    summary = summary_doc.get("summary", {}) or {}
    total_value = float(summary.get("total_value") or summary.get("net_value") or 0)
    equity_value = float(summary.get("equity_value", 0) or 0)
    bond_value = float(summary.get("bond_value", 0) or 0)
    cash_value = float(summary.get("cash_value", 0) or 0)
    crypto_value = float(summary.get("crypto_value", 0) or 0)
    position_count = _coerce_position_count(summary.get("position_count", 0))
    ugl = float(summary.get("unrealized_gl", 0) or 0)
    ugl_pct = float(summary.get("unrealized_gl_pct", 0) or 0)
    ugl_color = "kpi-positive" if ugl >= 0 else "kpi-negative"

    kpis = f"""
<h2>Portfolio summary</h2>
<div class="kpi-grid">
  <div class="kpi"><div class="kpi-label">Total Value</div><div class="kpi-value">${total_value:,.0f}</div></div>
  <div class="kpi"><div class="kpi-label">Positions</div><div class="kpi-value">{position_count}</div></div>
  <div class="kpi"><div class="kpi-label">Unrealized G/L</div><div class="kpi-value {ugl_color}">${ugl:,.0f} ({ugl_pct*100:+.2f}%)</div></div>
  <div class="kpi"><div class="kpi-label">Equity</div><div class="kpi-value">${equity_value:,.0f}</div></div>
  <div class="kpi"><div class="kpi-label">Bonds</div><div class="kpi-value">${bond_value:,.0f}</div></div>
  <div class="kpi"><div class="kpi-label">Cash</div><div class="kpi-value">${cash_value:,.0f}</div></div>
  <div class="kpi"><div class="kpi-label">Crypto</div><div class="kpi-value">${crypto_value:,.0f}</div></div>
</div>"""

    # Sector breakdown — engine ships either {sector: weight_float} (current)
    # or {sector: {weight_pct, value}} (legacy). Handle both.
    def _sector_weight(info):
        if isinstance(info, dict):
            return float(info.get("weight_pct", 0) or 0)
        try:
            return float(info or 0)
        except (TypeError, ValueError):
            return 0.0

    sectors = summary_doc.get("sector_weights", {}) or {}
    if sectors:
        sector_rows = []
        for sec, info in sorted(sectors.items(), key=lambda kv: -_sector_weight(kv[1])):
            if isinstance(info, dict):
                weight = float(info.get("weight_pct", 0) or 0)
                value = float(info.get("value", 0) or 0)
            else:
                weight = _sector_weight(info)
                value = total_value * weight / 100
            sector_rows.append(
                f"<tr><td>{_h(sec)}</td>"
                f'<td style="text-align:right;">{weight:.2f}%</td>'
                f'<td style="text-align:right;">${value:,.0f}</td></tr>'
            )
        sector_block = (
            "<h2>Sector allocation</h2><div class=\"section-card\"><table>"
            "<tr><th>Sector</th><th style=\"text-align:right;\">Weight</th>"
            "<th style=\"text-align:right;\">Value</th></tr>"
            + "".join(sector_rows) + "</table></div>"
        )
    else:
        sector_block = ""

    # Accounts breakdown — defensive against engine returning floats or dicts.
    def _acct_value(info):
        if isinstance(info, dict):
            try:
                return float(info.get("value", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        try:
            return float(info or 0)
        except (TypeError, ValueError):
            return 0.0

    accounts = summary_doc.get("accounts", {}) or {}
    if accounts:
        acct_rows = []
        for name, info in sorted(accounts.items(), key=lambda kv: -_acct_value(kv[1])):
            info_dict = info if isinstance(info, dict) else {}
            value = _acct_value(info)
            ftype = _h(str(info_dict.get("financial_type", "—")))
            classification = _h(str(info_dict.get("classification", "—")))
            acct_rows.append(
                f"<tr><td>{_h(name)}</td>"
                f'<td>{ftype}</td>'
                f'<td>{classification}</td>'
                f'<td style="text-align:right;">${value:,.0f}</td>'
                f'<td style="text-align:right;">{(value/total_value*100 if total_value else 0):.1f}%</td></tr>'
            )
        acct_block = (
            "<h2>Accounts</h2><div class=\"section-card\"><table>"
            "<tr><th>Account</th><th>Type</th><th>Class</th>"
            "<th style=\"text-align:right;\">Value</th>"
            "<th style=\"text-align:right;\">Weight</th></tr>"
            + "".join(acct_rows) + "</table></div>"
        )
    else:
        acct_block = ""

    # All positions — pull from .raw/holdings.json (CDM portfolioState.positions)
    positions = (
        raw_doc.get("portfolio", {})
        .get("portfolioState", {})
        .get("positions", [])
    )
    if positions:
        pos_rows = []
        # Sort by market value descending
        positions_sorted = sorted(
            positions,
            key=lambda p: -float(p.get("marketValue", 0) or 0),
        )
        for p in positions_sorted:
            sym = _h(((p.get("asset") or {}).get("securityName")
                      or (p.get("product") or {}).get("productIdentifier", {}).get("identifier", "")))
            sec_type = _h((p.get("asset") or {}).get("securityType", ""))
            sector = _h((p.get("asset") or {}).get("sector", "—"))
            qty = float((p.get("priceQuantity") or {}).get("quantity", {}).get("amount", 0) or 0)
            cur_price = float((p.get("priceQuantity") or {}).get("currentPrice", {}).get("amount", 0) or 0)
            cost_price = float((p.get("priceQuantity") or {}).get("costBasisPrice", {}).get("amount", 0) or 0)
            mv = float(p.get("marketValue", 0) or 0)
            cb = float(p.get("costBasis", 0) or 0)
            gl = float(p.get("unrealizedGainLoss", 0) or 0)
            gl_pct = float(p.get("unrealizedGainLossPct", 0) or 0)
            gl_color = "#3fb950" if gl >= 0 else "#f85149"
            weight = (mv / total_value * 100) if total_value else 0
            pos_rows.append(
                f"<tr>"
                f"<td><strong>{sym}</strong></td>"
                f"<td>{sec_type}</td>"
                f"<td>{sector}</td>"
                f'<td style="text-align:right;">{qty:,.2f}</td>'
                f'<td style="text-align:right;">${cur_price:,.2f}</td>'
                f'<td style="text-align:right;">${cost_price:,.2f}</td>'
                f'<td style="text-align:right;">${mv:,.0f}</td>'
                f'<td style="text-align:right;">{weight:.2f}%</td>'
                f'<td style="text-align:right;color:{gl_color};">${gl:,.0f}</td>'
                f'<td style="text-align:right;color:{gl_color};">{gl_pct*100:+.2f}%</td>'
                f"</tr>"
            )
        pos_table = (
            f"<h2>All positions ({len(positions)})</h2>"
            "<div class=\"section-card\" style=\"overflow-x:auto;\"><table>"
            "<tr><th>Symbol</th><th>Type</th><th>Sector</th>"
            "<th style=\"text-align:right;\">Qty</th>"
            "<th style=\"text-align:right;\">Price</th>"
            "<th style=\"text-align:right;\">Cost</th>"
            "<th style=\"text-align:right;\">Market Value</th>"
            "<th style=\"text-align:right;\">Weight</th>"
            "<th style=\"text-align:right;\">G/L $</th>"
            "<th style=\"text-align:right;\">G/L %</th></tr>"
            + "".join(pos_rows) + "</table></div>"
        )
    else:
        # Fall back to top_equity from summary
        top = summary_doc.get("top_equity", []) or []
        if top:
            rows = []
            for h in top:
                sym = _h(h.get("symbol", ""))
                sec = _h(h.get("sector", "—"))
                value = float(h.get("value", 0) or 0)
                weight = float(h.get("weight_pct", 0) or 0)
                gl = float(h.get("gl_pct", 0) or 0)
                gl_color = "#3fb950" if gl >= 0 else "#f85149"
                rows.append(
                    f"<tr><td><strong>{sym}</strong></td><td>{sec}</td>"
                    f'<td style="text-align:right;">${value:,.0f}</td>'
                    f'<td style="text-align:right;">{weight:.2f}%</td>'
                    f'<td style="text-align:right;color:{gl_color};">{gl:+.2f}%</td></tr>'
                )
            pos_table = (
                f"<h2>Top equity holdings (top {len(top)})</h2>"
                "<div class=\"section-card\"><p class=\"muted\">"
                "Full position-level detail unavailable; install or refresh "
                "the portfolio for the complete table.</p>"
                "<table><tr><th>Symbol</th><th>Sector</th>"
                "<th style=\"text-align:right;\">Value</th>"
                "<th style=\"text-align:right;\">Weight</th>"
                "<th style=\"text-align:right;\">G/L</th></tr>"
                + "".join(rows) + "</table></div>"
            )
        else:
            pos_table = '<div class="empty">No position-level detail.</div>'

    body = kpis + sector_block + acct_block + pos_table
    return _shell("holdings", body, title="Holdings")


def _h(s) -> str:
    """HTML-escape helper."""
    if s is None:
        return ""
    import html
    return html.escape(str(s))


def _http_signup_url(url: Any) -> str | None:
    """Return a safe http(s) signup URL, or None for inert rendering."""
    from urllib.parse import urlparse

    if not isinstance(url, str):
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return parsed.geturl()
    return None


def _csrf_redirect(request: Request, target_path: str) -> RedirectResponse | None:
    """Reject cross-origin dashboard POSTs based on Origin/Referer host."""
    from urllib.parse import quote, urlparse

    request_host = (request.headers.get("host") or request.url.netloc).lower()
    seen_matching_header = False
    for header_name in ("origin", "referer"):
        header_value = request.headers.get(header_name)
        if not header_value:
            continue
        parsed_host = urlparse(header_value).netloc.lower()
        if not parsed_host or parsed_host != request_host:
            msg = "Rejected cross-origin dashboard POST"
            return RedirectResponse(
                url=f"{target_path}?message={quote(msg)}",
                status_code=303,
            )
        seen_matching_header = True
    if not seen_matching_header:
        msg = "Rejected cross-origin dashboard POST"
        return RedirectResponse(
            url=f"{target_path}?message={quote(msg)}",
            status_code=303,
        )
    return None


def _performance_tab() -> str:
    perf = _load_json("performance.json")
    if not perf:
        body = '<h2>Performance</h2><div class="empty">No performance data. Run <code>investorclaw performance</code> in the container.</div>'
        return _shell("performance", body, title="Performance")

    parts = ["<h2>Performance</h2>"]
    data = perf.get("data") or perf
    ps = data.get("portfolio_summary") or {}
    period = (data.get("period") or {})

    # Summary KPI card
    def _pct(v):
        if v is None: return "—"
        try: return f"{float(v)*100:+.2f}%" if abs(float(v)) <= 1 else f"{float(v):+.2f}%"
        except: return "—"
    def _n(v, d=3):
        if v is None: return "—"
        try: return f"{float(v):.{d}f}"
        except: return "—"

    vo = ps.get("weighted_volatility"); sh = ps.get("weighted_sharpe")
    so = ps.get("weighted_sortino"); md = ps.get("weighted_max_drawdown")
    ar = ps.get("weighted_annual_return"); be = ps.get("weighted_beta_to_market")
    period_start = (period.get("start") or data.get("period",{}).get("start","?"))[:10] if isinstance(data.get("period"),dict) else "?"
    analyzed = data.get("holdings_analyzed","?")
    parts.append(f"""<h3>Performance Metrics</h3><div class="section-card">
<div class="kpi-grid">
  <div class="kpi"><div class="kpi-label">Period start</div><div class="kpi-value" style="font-size:15px">{_h(str(period_start))}</div></div>
  <div class="kpi"><div class="kpi-label">Symbols analyzed</div><div class="kpi-value">{analyzed}/{analyzed}</div></div>
  <div class="kpi"><div class="kpi-label">Portfolio volatility</div><div class="kpi-value {'kpi-negative' if vo and float(vo)>0.35 else ''}">{_pct(vo)} ann.</div></div>
  <div class="kpi"><div class="kpi-label">Portfolio Sharpe</div><div class="kpi-value">{_n(sh,2)}</div></div>
  <div class="kpi"><div class="kpi-label">Sortino ratio</div><div class="kpi-value">{_n(so,2)}</div></div>
  <div class="kpi"><div class="kpi-label">Max drawdown</div><div class="kpi-value kpi-negative">{_pct(md)}</div></div>
  <div class="kpi"><div class="kpi-label">Beta vs market</div><div class="kpi-value">{_n(be,2)}</div></div>
  <div class="kpi"><div class="kpi-label">Ann. return</div><div class="kpi-value">{_pct(ar)}</div></div>
</div></div>""")

    # Per-symbol volatility + risk table
    perf_syms = data.get("performance") or {}
    if perf_syms:
        rows_vol = []
        for sym, v in perf_syms.items():
            if not isinstance(v, dict): continue
            vol_d = v.get("volatility") or {}
            if not vol_d.get("_valid"): continue
            ann_vol = vol_d.get("annualized_volatility") or 0
            sha = v.get("sharpe_ratio")
            sha_val = (sha.get("sharpe_ratio") if isinstance(sha, dict) else sha)
            beta_d = v.get("beta") or {}
            beta_val = beta_d.get("beta") if isinstance(beta_d, dict) else beta_d
            rows_vol.append((sym, ann_vol, sha_val, beta_val))

        # Sort by volatility descending — highest risk positions first
        rows_vol.sort(key=lambda x: x[1] if x[1] is not None else 0, reverse=True)

        def _vol_color(v):
            if v is None: return ""
            f = float(v)
            if f > 0.8: return "color:#f85149;"
            if f > 0.5: return "color:#d29922;"
            return "color:#3fb950;"

        rows_html = []
        for sym, vol, sha, beta in rows_vol[:50]:
            vc = _vol_color(vol)
            rows_html.append(
                f'<tr><td><code>{_h(sym)}</code></td>'
                f'<td style="text-align:right;{vc}">{_pct(vol)}</td>'
                f'<td style="text-align:right">{_n(sha,2) if sha is not None else "—"}</td>'
                f'<td style="text-align:right">{_n(beta,2) if beta is not None else "—"}</td>'
                f'</tr>'
            )
        if rows_html:
            parts.append(
                f'<h3>Per-Position Risk (top 50 by volatility, {len(rows_vol)} total)</h3>'
                '<p class="muted">Sorted by annualized volatility. Red = high volatility (&gt;80% ann.).</p>'
                '<div class="section-card"><div style="overflow-x:auto"><table>'
                '<tr><th>Symbol</th><th style="text-align:right">Volatility</th>'
                '<th style="text-align:right">Sharpe</th><th style="text-align:right">Beta</th></tr>'
                + "".join(rows_html) + '</table></div></div>'
            )

    return _shell("performance", "\n".join(parts), title="Performance")


def _whatchanged_tab() -> str:
    wc = _load_json("whatchanged.json")
    if _T:
        body = "<h2>What Changed</h2>\n" + _section_or_empty(
            _T._render_whatchanged(wc),
            "No attribution data. Run <code>investorclaw whatchanged</code> in the container.",
        )
    else:
        body = "<h2>What Changed</h2>\n" + _section_or_empty("", "Engine helpers unavailable.")
    return _shell("whatchanged", body, title="What Changed")


def _scenarios_tab() -> str:
    scen = _load_json("scenario.json")
    if _T:
        body = "<h2>Scenarios &amp; Stress Tests</h2>\n" + _section_or_empty(
            _T._render_scenario(scen),
            "No scenario data. Run <code>investorclaw scenario</code> in the container.",
        )
    else:
        body = "<h2>Scenarios</h2>\n" + _section_or_empty("", "Engine helpers unavailable.")
    return _shell("scenarios", body, title="Scenarios")


def _bonds_tab() -> str:
    bonds = _load_json("bond_analysis.json")
    summary_html = ""
    if _T:
        summary_html = _T._render_bond_summary(bonds)

    individual = (bonds.get("data") or {}).get("individual_bonds") or []

    if individual:
        rows = []
        def _bond_name(b):
            cusip = b.get("cusip") or b.get("symbol") or ""
            # Try exact name from uploaded portfolio CSV first
            csv_name = _cusip_to_name(cusip)
            if csv_name:
                return csv_name
            # Fallback: construct from type + coupon + maturity
            atype = str(b.get("asset_type", "")).lower()
            coupon = b.get("coupon_rate", 0) or 0
            mat = str(b.get("maturity_date", ""))[:7]
            try:
                months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
                yr = mat[2:4]; mn = int(mat[5:7])
                mat_str = f"{months[mn-1]} '{yr}"
            except Exception:
                mat_str = mat
            if atype == "treasury":
                return f"US {'T-Bill' if coupon == 0 else 'Treasury'} {coupon:.2f}% {mat_str}"
            elif "municipal" in atype:
                return f"Muni Bond {coupon:.2f}% {mat_str}"
            elif "corporate" in atype:
                return f"Corp Bond {coupon:.2f}% {mat_str}"
            elif "agency" in atype:
                return f"Agency {coupon:.2f}% {mat_str}"
            return f"{atype.replace('_',' ').title()} {coupon:.2f}% {mat_str}"

        for b in sorted(individual, key=lambda x: x.get("market_value", 0), reverse=True):
            cusip     = _h(b.get("cusip") or b.get("symbol") or "—")
            bname     = _h(_bond_name(b))
            coupon    = f"{b['coupon_rate']:.2f}%" if b.get("coupon_rate") else "—"
            ytm       = f"{b['ytm']:.2f}%" if b.get("ytm") else "—"
            tey       = f"{b['tax_equivalent_yield']:.2f}%" if b.get("tax_equivalent_yield") else "—"
            maturity  = _h(b.get("maturity_date") or "—")
            yrs       = f"{b['years_to_maturity']:.1f}y" if b.get("years_to_maturity") else "—"
            duration  = f"{b['modified_duration']:.2f}" if b.get("modified_duration") else "—"
            mkt_val   = f"${b['market_value']:,.0f}" if b.get("market_value") else "—"
            credit    = _h(b.get("credit_quality_estimate") or "—")
            bucket    = _h(b.get("maturity_bucket") or "—")
            rows.append(f"""<tr>
              <td>{bname}<br><span style="font-family:monospace;font-size:10px;opacity:0.6">{cusip}</span></td>
              <td style="text-align:right">{mkt_val}</td>
              <td style="text-align:right">{coupon}</td>
              <td style="text-align:right">{ytm}</td>
              <td style="text-align:right">{tey}</td>
              <td style="text-align:right">{duration}</td>
              <td style="text-align:right">{yrs}</td>
              <td style="text-align:center">{maturity}</td>
              <td style="text-align:center">{credit}</td>
              <td style="text-align:center">{bucket}</td>
            </tr>""")
        bonds_table = f"""
<h3 style="margin-top:28px">Individual Positions ({len(individual)})</h3>
<div style="overflow-x:auto">
<table style="width:100%;border-collapse:collapse;font-size:12px">
  <thead><tr style="background:#1f6feb;color:#fff">
    <th style="text-align:left;padding:6px 8px">Bond</th>
    <th style="text-align:right;padding:6px 8px">Mkt Value</th>
    <th style="text-align:right;padding:6px 8px">Coupon</th>
    <th style="text-align:right;padding:6px 8px">YTM</th>
    <th style="text-align:right;padding:6px 8px">TEY</th>
    <th style="text-align:right;padding:6px 8px">Mod Dur</th>
    <th style="text-align:right;padding:6px 8px">Yrs</th>
    <th style="text-align:center;padding:6px 8px">Maturity</th>
    <th style="text-align:center;padding:6px 8px">Credit</th>
    <th style="text-align:center;padding:6px 8px">Bucket</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
</div>"""
    else:
        bonds_table = ""

    body = "<h2>Fixed Income</h2>\n" + _section_or_empty(
        summary_html + bonds_table,
        "No bond data (your portfolio may have no bond holdings, or run <code>investorclaw bonds</code>).",
    )
    return _shell("bonds", body, title="Bonds")


def _analyst_tab() -> str:
    a = _load_json("analyst_recommendations_summary.json")
    ad = _load_json("analyst_data.json")
    parts = ["<h2>Analyst Coverage</h2>"]

    # Summary KPIs
    if a:
        cov = a.get("analyst_coverage") or a.get("summary") or {}
        # total_symbols lives in summary sub-dict, not in analyst_coverage
        summary_d = a.get("summary") or {}
        try:
            total = int(
                summary_d.get("total_symbols")
                or cov.get("total_symbols")
                or a.get("total_symbols")
                or 0
            )
        except Exception:
            total = 0
        strong = int(cov.get("strong_coverage", 0) or 0)
        moderate = int(cov.get("moderate_coverage", 0) or 0)
        none_c = int(cov.get("no_coverage", 0) or 0)
        # Use actual count if total still 0
        if total == 0 and recs and isinstance(recs, dict):
            total = len(recs)
        total_d = total or 1
        parts.append(f"""<h3>Analyst Coverage</h3><div class="section-card">
<div class="kpi-grid">
  <div class="kpi"><div class="kpi-label">Symbols analyzed</div><div class="kpi-value">{total}</div></div>
  <div class="kpi"><div class="kpi-label">Strong coverage</div><div class="kpi-value kpi-positive">{strong} ({strong*100//total_d}%)</div></div>
  <div class="kpi"><div class="kpi-label">Moderate coverage</div><div class="kpi-value">{moderate} ({moderate*100//total_d}%)</div></div>
  <div class="kpi"><div class="kpi-label">No coverage</div><div class="kpi-value kpi-negative">{none_c}</div></div>
</div></div>""")

    # Per-stock analyst detail from analyst_data.json
    recs = ad.get("recommendations") if ad else None
    if recs and isinstance(recs, dict):
        # Filter to meaningful entries (has analyst_count > 0)
        entries = [v for v in recs.values() if isinstance(v, dict) and (v.get("analyst_count") or 0) > 0]

        # Sort: Strong Buy first, then Buy, Hold, Sell; within tier by upside desc
        tier_order = {"strong buy": 0, "buy": 1, "hold": 2, "underperform": 3, "sell": 4}
        def _upside(r):
            curr = r.get("current_price") or 0
            tgt = r.get("target_price_mean") or 0
            return (tgt - curr) / curr * 100 if curr > 0 else 0

        entries.sort(key=lambda r: (
            tier_order.get((r.get("consensus") or "").lower(), 5),
            -_upside(r)
        ))

        def _consensus_color(c):
            c = (c or "").lower()
            if "strong buy" in c: return "#3fb950"
            if "buy" in c: return "#58a6ff"
            if "hold" in c: return "#d29922"
            return "#f85149"

        rows = []
        for r in entries[:100]:  # top 100
            sym = _h(r.get("symbol", ""))
            consensus = _h(r.get("consensus") or "—")
            n = r.get("analyst_count", 0)
            buys = r.get("buy_count", 0)
            holds = r.get("hold_count", 0)
            sells = r.get("sell_count", 0)
            curr = r.get("current_price") or 0
            tgt = r.get("target_price_mean") or 0
            upside = _upside(r)
            upside_cls = "kpi-positive" if upside > 0 else ("kpi-negative" if upside < 0 else "")
            yf_url = f"https://finance.yahoo.com/quote/{sym}/analysis/"
            rows.append(
                f'<tr>'
                f'<td><a href="{yf_url}" target="_blank" style="color:#58a6ff"><code>{sym}</code></a></td>'
                f'<td style="color:{_consensus_color(consensus)};font-weight:600">{consensus}</td>'
                f'<td style="text-align:center">{n}</td>'
                f'<td style="text-align:center;color:#3fb950">{buys}</td>'
                f'<td style="text-align:center;color:#d29922">{holds}</td>'
                f'<td style="text-align:center;color:#f85149">{sells}</td>'
                f'<td style="text-align:right">${curr:,.2f}</td>'
                f'<td style="text-align:right">${tgt:,.2f}</td>'
                f'<td style="text-align:right" class="{upside_cls}">{upside:+.1f}%</td>'
                f'</tr>'
            )

        parts.append(
            f'<h3>Analyst Recommendations ({len(entries)} covered)</h3>'
            '<p class="muted">Sorted by consensus tier then upside potential. Click symbol for Yahoo Finance analyst detail.</p>'
            '<div class="section-card"><div style="overflow-x:auto"><table>'
            '<tr><th>Symbol</th><th>Consensus</th><th style="text-align:center">Analysts</th>'
            '<th style="text-align:center">Buy</th><th style="text-align:center">Hold</th>'
            '<th style="text-align:center">Sell</th><th style="text-align:right">Price</th>'
            '<th style="text-align:right">Target</th><th style="text-align:right">Upside</th></tr>'
            + "".join(rows) + '</table></div></div>'
        )
    elif not a:
        parts.append('<div class="empty">No analyst data. Run <code>investorclaw analyst</code>.</div>')

    return _shell("analyst", "\n".join(parts), title="Analyst")


def _news_tab() -> str:
    """Full per-holding news coverage — every headline, sortable, with sentiment + impact."""
    cache = _load_json("portfolio_news_cache.json")
    summary = _load_json("portfolio_news.json")

    if not cache and not summary:
        body = ('<h2>News</h2><div class="empty">No news data yet. '
                "Run <code>investorclaw news</code> in the container.</div>")
        return _shell("news", body, title="News")

    per_symbol = cache.get("per_symbol", {}) if cache else {}
    all_news = cache.get("all_news", []) if cache else []
    timestamp = cache.get("timestamp", "") if cache else ""
    symbols_covered = cache.get("symbols", []) if cache else []
    skipped = cache.get("skipped_symbols", []) if cache else []

    # Top-of-page summary KPIs
    total_items = sum(len(items) for items in per_symbol.values()) if per_symbol else len(all_news)
    sym_with_news = len([s for s, items in per_symbol.items() if items]) if per_symbol else 0
    kpis = f"""
<h2>News coverage</h2>
<div class="kpi-grid">
  <div class="kpi"><div class="kpi-label">Symbols covered</div><div class="kpi-value">{len(symbols_covered)}</div></div>
  <div class="kpi"><div class="kpi-label">With news</div><div class="kpi-value">{sym_with_news}</div></div>
  <div class="kpi"><div class="kpi-label">Total items</div><div class="kpi-value">{total_items}</div></div>
  <div class="kpi"><div class="kpi-label">Last fetch</div><div class="kpi-value" style="font-size:13px;">{_h(timestamp[:19].replace('T',' '))}</div></div>
</div>
"""

    # Editorial summary (posture / tailwinds / risks) from portfolio_news.json
    if summary:
        posture = _h(summary.get("posture", "—"))
        narrative = _h(summary.get("narrative", "")).replace("\n", "<br>")
        tailwinds = summary.get("key_tailwinds", []) or []
        risks = summary.get("key_risks", []) or []
        tailwinds_html = ("<ul>" + "".join(f"<li>{_h(t)}</li>" for t in tailwinds) + "</ul>") if tailwinds else ""
        risks_html = ("<ul>" + "".join(f"<li>{_h(r)}</li>" for r in risks) + "</ul>") if risks else ""

        editorial = f"""
<h2>Editorial summary</h2>
<div class="section-card">
  <p><strong>Posture:</strong> {posture}</p>
  {f'<p>{narrative}</p>' if narrative else ''}
  {f'<p><strong>Key tailwinds</strong></p>{tailwinds_html}' if tailwinds else ''}
  {f'<p><strong>Key risks</strong></p>{risks_html}' if risks else ''}
</div>
"""
    else:
        editorial = ""

    # Per-symbol news listing
    def _sentiment_color(s) -> str:
        s = (str(s) or "").lower()
        if "positive" in s or "bull" in s:
            return "#3fb950"
        if "negative" in s or "bear" in s:
            return "#f85149"
        return "#8b949e"

    def _item_html(item: Dict[str, Any]) -> str:
        title = _h(item.get("title", ""))
        link = item.get("link") or item.get("url") or ""
        safe_link = _http_signup_url(link)
        source = _h(item.get("source", ""))
        date = _h(str(item.get("publish_date", ""))[:19].replace("T", " "))
        sentiment = item.get("sentiment", "neutral")
        sentiment_color = _sentiment_color(sentiment)
        confidence = item.get("confidence")
        impact_pct = item.get("impact_pct")
        summary_text = _h(item.get("summary", ""))[:300]

        meta_bits = [f'<span style="color:{sentiment_color};font-weight:600;">{_h(sentiment)}</span>']
        if confidence is not None:
            try:
                meta_bits.append(f'conf {float(confidence):.0%}')
            except Exception:
                pass
        if impact_pct is not None:
            try:
                meta_bits.append(f'impact {float(impact_pct):+.2f}%')
            except Exception:
                pass
        if source:
            meta_bits.append(_h(source))
        if date:
            meta_bits.append(date)
        meta = ' · '.join(meta_bits)

        title_html = (
            f'<a href="{_h(safe_link)}" target="_blank" rel="noopener noreferrer">{title}</a>'
            if safe_link else title
        )
        return (
            f'<div style="padding:10px 0;border-bottom:1px solid #21262d;">'
            f'<div style="font-weight:600;margin-bottom:4px;">{title_html}</div>'
            f'<div style="font-size:12px;color:#8b949e;margin-bottom:4px;">{meta}</div>'
            + (f'<div style="font-size:13px;color:#c9d1d9;">{summary_text}…</div>' if summary_text else '')
            + '</div>'
        )

    if per_symbol:
        # Sort symbols by item count (most news first), then alphabetically
        symbols_sorted = sorted(
            per_symbol.items(),
            key=lambda kv: (-len(kv[1] or []), kv[0]),
        )
        per_symbol_blocks = []
        for sym, items in symbols_sorted:
            if not items:
                continue
            items_html = "".join(_item_html(it) for it in items)
            per_symbol_blocks.append(
                f'<details style="margin-bottom:16px;">'
                f'<summary style="cursor:pointer;padding:8px 12px;background:#161b22;'
                f'border:1px solid #30363d;border-radius:6px;font-weight:600;">'
                f'<span style="color:#58a6ff;">{_h(sym)}</span> '
                f'<span style="color:#8b949e;font-weight:normal;font-size:12px;">— {len(items)} item{"s" if len(items)!=1 else ""}</span>'
                f'</summary>'
                f'<div style="padding:8px 12px;background:#0d1117;border:1px solid #30363d;border-top:none;border-radius:0 0 6px 6px;">'
                f'{items_html}'
                f'</div></details>'
            )
        per_sym_block = (
            f'<h2>News by holding ({sym_with_news} symbols)</h2>'
            + "".join(per_symbol_blocks)
        )
    else:
        per_sym_block = ""

    skipped_block = ""
    if skipped:
        skipped_block = (
            f'<h2 style="margin-top:32px;">Skipped symbols ({len(skipped)})</h2>'
            f'<div class="section-card"><p class="muted">No news fetched for these holdings '
            "(provider didn't return data, or rate-limit hit):</p>"
            f'<p style="font-family:monospace;font-size:12px;">{_h(", ".join(skipped))}</p></div>'
        )

    body = kpis + editorial + per_sym_block + skipped_block
    return _shell("news", body, title="News")


def _synthesis_tab() -> str:
    syn = _load_json("portfolio_analysis.json")
    if not syn:
        body = '<h2>Synthesis</h2><div class="empty">No synthesis data. Run <code>investorclaw synthesize</code>.</div>'
        return _shell("synthesis", body, title="Synthesis")

    # synthesis is a dict; render the narrative and any FA topics
    narr = (syn.get("data") or syn).get("narrative") or syn.get("narrative") or ""
    parts = ["<h2>Synthesis</h2>"]
    if narr:
        narr_html = "<br>".join(line.strip() for line in narr.splitlines() if line.strip())
        parts.append(f'<div class="section-card">{narr_html}</div>')
    # FA topics
    if _T:
        try:
            from ic_engine.commands.fa_discussion import extract_fa_topics
            topics = extract_fa_topics(pathlib.Path(REPORTS_DIR), preloaded={"synthesize": syn})
            parts.append(_section_or_empty(_T._render_fa_topics(topics), ""))
        except Exception:
            pass
    body = "\n".join(parts)
    return _shell("synthesis", body, title="Synthesis")


def _optimize_tab() -> str:
    """Optimization + rebalance — covers cobol p09/p10/p11/p12/p13."""
    opt = _load_json("optimize.json")
    rebal = _load_json("rebalance.json")
    rebal_tax = _load_json("rebalance_tax.json")
    parts = ["<h2>Portfolio Optimization &amp; Rebalance</h2>"]
    parts.append(
        '<p class="muted">Sharpe-maximizing and minimum-volatility allocations from PyPortfolioOpt, '
        'plus rebalance trades against the target allocation (with optional tax-impact view).</p>'
    )

    def _alloc_block(title: str, alloc: dict, expected_sharpe=None, expected_vol=None) -> str:
        if not alloc:
            return ""
        rows = []
        for sym, w in sorted(alloc.items(), key=lambda kv: -float(kv[1] or 0)):
            try:
                pct = float(w) * 100
            except Exception:
                pct = 0
            if pct < 0.05:
                continue
            rows.append(f'<tr><td><code>{_h(sym)}</code></td><td style="text-align:right;">{pct:.2f}%</td></tr>')
        meta_bits = []
        if expected_sharpe is not None:
            try:
                meta_bits.append(f"Sharpe {float(expected_sharpe):.3f}")
            except Exception:
                pass
        if expected_vol is not None:
            try:
                meta_bits.append(f"Vol {float(expected_vol)*100:.2f}%")
            except Exception:
                pass
        meta = (' <span class="muted">— ' + ' · '.join(meta_bits) + '</span>') if meta_bits else ''
        return (
            f'<h3>{title}{meta}</h3>'
            f'<div class="section-card"><table>'
            f'<tr><th>Symbol</th><th style="text-align:right;">Weight</th></tr>'
            + "".join(rows) + '</table></div>'
        )

    if opt:
        # Two common shapes: {max_sharpe: {...}, min_vol: {...}} or top-level allocation
        ms = opt.get("max_sharpe") or opt.get("data", {}).get("max_sharpe") or {}
        mv = opt.get("min_volatility") or opt.get("data", {}).get("min_volatility") or {}
        parts.append(_alloc_block(
            "Maximum Sharpe allocation",
            ms.get("weights") or ms.get("allocation") or {},
            ms.get("expected_sharpe"),
            ms.get("expected_volatility"),
        ))
        parts.append(_alloc_block(
            "Minimum-volatility allocation",
            mv.get("weights") or mv.get("allocation") or {},
            mv.get("expected_sharpe"),
            mv.get("expected_volatility"),
        ))
        if not ms and not mv:
            # fallback — just dump weights if present
            top = opt.get("weights") or opt.get("data", {}).get("weights") or {}
            parts.append(_alloc_block("Optimal allocation", top))
    else:
        parts.append(
            '<div class="empty">No optimization run yet. '
            'Run <code>investorclaw optimize</code> in the container, or click '
            '<strong>Regenerate</strong> on the Overview tab.</div>'
        )

    # Rebalance trades — current vs target
    def _trades_block(title: str, doc: dict) -> str:
        if not doc:
            return ""
        trades = (
            doc.get("trades")
            or doc.get("data", {}).get("trades")
            or doc.get("rebalance_trades", [])
            or []
        )
        if not trades:
            return ""
        rows = []
        for t in trades[:200]:
            sym = _h(str(t.get("symbol", "")))
            action = _h(str(t.get("action") or t.get("side") or ""))
            current_pct = t.get("current_pct") or t.get("current_weight")
            target_pct = t.get("target_pct") or t.get("target_weight")
            delta = t.get("delta_pct") or t.get("trade_pct")
            dollar = t.get("dollar_amount") or t.get("notional") or t.get("amount")
            tax = t.get("tax_impact") or t.get("tax_cost")

            def _pct(v):
                try:
                    return f"{float(v)*100:+.2f}%" if abs(float(v)) <= 1 else f"{float(v):+.2f}%"
                except Exception:
                    return "—"

            def _money(v):
                try:
                    return f"${float(v):,.0f}"
                except Exception:
                    return "—"

            rows.append(
                f'<tr><td><code>{sym}</code></td><td>{action}</td>'
                f'<td>{_pct(current_pct)}</td><td>{_pct(target_pct)}</td>'
                f'<td>{_pct(delta)}</td><td>{_money(dollar)}</td>'
                f'<td>{_money(tax) if tax is not None else "—"}</td></tr>'
            )
        return (
            f'<h3>{title}</h3>'
            f'<div class="section-card"><table>'
            f'<tr><th>Symbol</th><th>Action</th><th>Current</th><th>Target</th>'
            f'<th>Δ</th><th>Notional</th><th>Tax impact</th></tr>'
            + "".join(rows) + '</table></div>'
        )

    parts.append(_trades_block("Rebalance trades", rebal))
    parts.append(_trades_block("Tax-aware rebalance trades", rebal_tax))
    if not rebal and not rebal_tax:
        parts.append(
            '<h3>Rebalance</h3>'
            '<div class="empty">No rebalance run yet. '
            'Run <code>investorclaw rebalance</code> or <code>investorclaw rebalance-tax</code>.</div>'
        )

    return _shell("optimize", "\n".join(parts), title="Optimize")


def _cashflow_tab() -> str:
    """Projected cash flow — dividends + bond coupons. Covers cobol p25."""
    cf = _load_json("cashflow.json")
    parts = ["<h2>Projected cash flow</h2>"]
    parts.append('<p class="muted">Forward-looking dividends and bond coupons over the next quarter / year.</p>')
    if not cf:
        parts.append(
            '<div class="empty">No cashflow data. '
            'Run <code>investorclaw cashflow</code> in the container.</div>'
        )
        return _shell("cashflow", "\n".join(parts), title="Cashflow")

    data = cf.get("data") or cf
    # ic-engine cashflow.py outputs: annual_total, monthly_cashflow, yield_on_cost
    annual = (
        data.get("annual_total")
        or data.get("total_year") or data.get("year_total") or data.get("annual_income")
    )
    monthly_cf = data.get("monthly_cashflow") or data.get("schedule") or []
    # Derive quarter total and split from monthly breakdown if available
    total_q = data.get("total_quarter") or data.get("quarter_total") or data.get("next_quarter_income")
    if total_q is None and monthly_cf:
        try:
            total_q = sum(float(m.get("total_income") or 0) for m in monthly_cf[:3])
        except Exception:
            total_q = None
    total_y = annual
    div = data.get("dividends_total") or data.get("dividend_income")
    coup = data.get("coupons_total") or data.get("coupon_income")
    if div is None and monthly_cf:
        try:
            div = sum(float(m.get("dividend_income") or 0) for m in monthly_cf)
        except Exception:
            div = None
    if coup is None and monthly_cf:
        try:
            coup = sum(float(m.get("coupon_income") or 0) for m in monthly_cf)
        except Exception:
            coup = None

    yoc = data.get("yield_on_cost")
    yoc_str = f"{float(yoc)*100:.2f}% yield on cost" if yoc else ""

    def _money(v):
        try:
            return f"${float(v):,.0f}"
        except Exception:
            return "—"

    parts.append(f"""<div class="kpi-grid">
  <div class="kpi"><div class="kpi-label">Next quarter</div><div class="kpi-value">{_money(total_q)}</div></div>
  <div class="kpi"><div class="kpi-label">Next 12 months</div><div class="kpi-value">{_money(total_y)}</div></div>
  <div class="kpi"><div class="kpi-label">Dividends (annual)</div><div class="kpi-value">{_money(div)}</div></div>
  <div class="kpi"><div class="kpi-label">Coupons (annual)</div><div class="kpi-value">{_money(coup)}</div></div>
</div>""")
    if yoc_str:
        parts.append(f'<p class="muted">{yoc_str}</p>')

    # Monthly cashflow table (ic-engine format: month, total_income, dividend_income, coupon_income)
    if monthly_cf:
        rows = []
        for item in monthly_cf[:24]:
            month = _h(str(item.get("month", ""))[:7])
            tot = item.get("total_income") or item.get("amount") or item.get("payment")
            div_m = item.get("dividend_income")
            coup_m = item.get("coupon_income")
            rows.append(
                f'<tr><td>{month}</td>'
                f'<td style="text-align:right;">{_money(tot)}</td>'
                f'<td style="text-align:right;">{_money(div_m)}</td>'
                f'<td style="text-align:right;">{_money(coup_m)}</td></tr>'
            )
        parts.append(
            '<h3>Monthly schedule</h3>'
            '<div class="section-card"><table>'
            '<tr><th>Month</th><th style="text-align:right;">Total</th>'
            '<th style="text-align:right;">Dividends</th>'
            '<th style="text-align:right;">Coupons</th></tr>'
            + "".join(rows) + '</table></div>'
        )

    # Calendar events (individual payments)
    events = data.get("calendar_events") or data.get("by_symbol") or data.get("payments") or []
    if events:
        rows = []
        for item in events[:50]:
            sym = _h(str(item.get("symbol", "")))
            kind = _h(str(item.get("type") or item.get("kind") or ""))
            pay_date = _h(str(item.get("date") or item.get("ex_date") or item.get("pay_date") or "")[:10])
            amt = item.get("amount") or item.get("payment")
            rows.append(
                f'<tr><td><code>{sym}</code></td><td>{kind}</td>'
                f'<td>{pay_date}</td><td style="text-align:right;">{_money(amt)}</td></tr>'
            )
        parts.append(
            '<h3>Upcoming payments (next 50)</h3>'
            '<div class="section-card"><table>'
            '<tr><th>Symbol</th><th>Type</th><th>Date</th>'
            '<th style="text-align:right;">Amount</th></tr>'
            + "".join(rows) + '</table></div>'
        )
    return _shell("cashflow", "\n".join(parts), title="Cashflow")


def _peer_tab() -> str:
    """Peer / benchmark comparison — covers cobol p26."""
    peer = _load_json("peer.json")
    parts = ["<h2>Benchmark comparison</h2>"]
    parts.append('<p class="muted">Your portfolio vs broad-market benchmarks (VTI, SPY, AGG, etc.). Returns, Sharpe, drawdown side-by-side.</p>')
    if not peer:
        parts.append(
            '<div class="empty">No peer comparison yet. '
            'Run <code>investorclaw peer</code> in the container.</div>'
        )
        return _shell("peer", "\n".join(parts), title="Peer")

    data = peer.get("data") or peer

    def _n(v, decimals=3):
        if v is None:
            return "—"
        try:
            return f"{float(v):.{decimals}f}"
        except Exception:
            return "—"

    def _pct(v):
        if v is None:
            return "—"
        try:
            f = float(v)
            return f"{f*100:.1f}%"
        except Exception:
            return "—"

    # ic-engine peer.py outputs: benchmark, beta_matrix, active_share, style_scores,
    # factor_tilts, overweight_sectors, underweight_sectors, holdings_analyzed
    bm = data.get("benchmark", "SPY")
    beta_matrix = data.get("beta_matrix") or {}
    active_share = data.get("active_share")
    style_scores = data.get("style_scores") or {}
    factor_tilts = data.get("factor_tilts") or {}
    over = data.get("overweight_sectors") or []
    under = data.get("underweight_sectors") or []

    # KPI grid — beta vs benchmarks + active share
    kpis = []
    for k, v in beta_matrix.items():
        label = k.replace("vs_", "β vs ").upper()
        kpis.append(f'<div class="kpi"><div class="kpi-label">{_h(label)}</div><div class="kpi-value">{_n(v, 2)}</div></div>')
    if active_share is not None:
        kpis.append(f'<div class="kpi"><div class="kpi-label">Active share</div><div class="kpi-value">{_pct(active_share)}</div></div>')
    if kpis:
        parts.append(f'<h3>Portfolio vs {_h(bm)}</h3><div class="kpi-grid">{"".join(kpis)}</div>')

    # Factor tilts table
    if factor_tilts:
        rows = []
        for factor, info in factor_tilts.items():
            if not isinstance(info, dict):
                continue
            port_v = info.get("portfolio")
            bench_v = info.get("spy") or info.get("benchmark")
            tilt = _h(str(info.get("tilt", "")))
            port_fmt = "Infinity" if port_v == float("inf") else _n(port_v, 2)
            rows.append(
                f'<tr><td>{_h(factor.replace("_", " ").title())}</td>'
                f'<td style="text-align:right;">{port_fmt}</td>'
                f'<td style="text-align:right;">{_n(bench_v, 2)}</td>'
                f'<td><span style="color:#58a6ff">{tilt}</span></td></tr>'
            )
        if rows:
            parts.append(
                '<h3>Factor tilts vs benchmark</h3>'
                '<div class="section-card"><table>'
                '<tr><th>Factor</th><th style="text-align:right;">Portfolio</th>'
                '<th style="text-align:right;">Benchmark</th><th>Tilt</th></tr>'
                + "".join(rows) + '</table></div>'
            )

    # Style scores
    if style_scores:
        kpis = []
        for k, v in style_scores.items():
            label = k.replace("_vs_", " vs ").replace("_", " ").title()
            kpis.append(f'<div class="kpi"><div class="kpi-label">{_h(label)}</div><div class="kpi-value">{_n(v, 2)}</div></div>')
        parts.append(f'<h3>Style scores</h3><div class="kpi-grid">{"".join(kpis)}</div>')

    # Overweight / underweight sectors
    def _sector_block(title, items):
        if not items:
            return ""
        rows = []
        for s in items[:10]:
            sec = _h(str(s.get("sector", "")))
            delta = s.get("delta")
            rows.append(f'<tr><td>{sec}</td><td style="text-align:right;">{_pct(delta)}</td></tr>')
        return (
            f'<h3>{title}</h3>'
            '<div class="section-card"><table>'
            '<tr><th>Sector</th><th style="text-align:right;">Δ vs benchmark</th></tr>'
            + "".join(rows) + '</table></div>'
        )

    parts.append(_sector_block("Overweight sectors", over))
    parts.append(_sector_block("Underweight sectors", under))

    # Legacy format: explicit benchmarks list
    benchmarks = data.get("benchmarks") or data.get("comparison") or []
    if benchmarks:
        rows = []
        for b in benchmarks:
            sym = _h(str(b.get("symbol") or b.get("benchmark") or ""))
            tot = b.get("total_return") or b.get("return")
            ann = b.get("annualized_return")
            sh = b.get("sharpe")
            dd = b.get("max_drawdown")
            corr = b.get("correlation")
            beta = b.get("beta")

            def _p(v):
                if v is None:
                    return "—"
                try:
                    f = float(v)
                    return f"{f*100:+.2f}%" if abs(f) <= 1 else f"{f:+.2f}%"
                except Exception:
                    return "—"

            rows.append(
                f'<tr><td><code>{sym}</code></td><td>{_p(tot)}</td><td>{_p(ann)}</td>'
                f'<td>{_n(sh)}</td><td>{_p(dd)}</td><td>{_n(corr)}</td><td>{_n(beta)}</td></tr>'
            )
        parts.append(
            '<h3>Benchmarks</h3>'
            '<div class="section-card"><table>'
            '<tr><th>Symbol</th><th>Total return</th><th>Annualized</th>'
            '<th>Sharpe</th><th>Max DD</th><th>Corr</th><th>Beta</th></tr>'
            + "".join(rows) + '</table></div>'
        )
    return _shell("peer", "\n".join(parts), title="Peer")


def _markets_tab() -> str:
    """Broad-market / crypto / FX context — covers cobol p17/p18/p21/p22."""
    mk = _load_json("markets.json")
    parts = ["<h2>Markets snapshot</h2>"]
    parts.append('<p class="muted">Indices, crypto, FX. Updated by <code>investorclaw markets</code> (or auto-refreshed by Regenerate).</p>')
    if not mk:
        parts.append(
            '<div class="empty">No markets snapshot yet. '
            'Run <code>investorclaw markets</code> or click <strong>Regenerate</strong>.</div>'
        )
        return _shell("markets", "\n".join(parts), title="Markets")

    data = mk.get("data") or mk

    def _block(title: str, items: list) -> str:
        if not items:
            return ""

        def _money(v):
            if v is None:
                return "—"
            try:
                return f"${float(v):,.2f}"
            except (ValueError, TypeError):
                return "—"

        def _chg_pct(v):
            """Format change_pct stored as percentage points (1.5 = +1.5%).
            Guard: if |v| > 15 the data is stale/invalid — show '—' to avoid
            misleading +60% intraday figures."""
            if v is None:
                return "—"
            try:
                f = float(v)
                if abs(f) > 15:
                    return "—"
                return f"{f:+.2f}%"
            except (ValueError, TypeError):
                return "—"

        rows = []
        for it in items:
            sym = _h(str(it.get("symbol") or it.get("ticker") or it.get("name") or ""))
            name = _h(str(it.get("name") or ""))
            price = it.get("price") or it.get("last")
            chg = it.get("change_pct") or it.get("pct_change") or it.get("change_percent")

            color = ""
            try:
                f = float(chg or 0)
                if abs(f) <= 15:
                    color = "color:#3fb950;" if f > 0 else ("color:#f85149;" if f < 0 else "")
            except (ValueError, TypeError):
                pass
            rows.append(
                f'<tr><td><code>{sym}</code></td><td>{name}</td>'
                f'<td style="text-align:right;">{_money(price)}</td>'
                f'<td style="text-align:right;{color}">{_chg_pct(chg)}</td></tr>'
            )
        return (
            f'<h3>{title}</h3>'
            f'<div class="section-card"><table>'
            f'<tr><th>Symbol</th><th>Name</th>'
            f'<th style="text-align:right;">Price</th><th style="text-align:right;">Change</th></tr>'
            + "".join(rows) + '</table></div>'
        )

    parts.append(_block("Indices", data.get("indices") or []))
    parts.append(_block("Crypto", data.get("crypto") or []))
    parts.append(_block("Foreign exchange", data.get("forex") or data.get("fx") or []))
    parts.append(_block("Fixed income / yields", data.get("rates") or data.get("yields") or []))

    summary = data.get("summary") or data.get("narrative")
    if summary:
        parts.append(
            '<h3>Summary</h3>'
            f'<div class="section-card">{_h(str(summary)).replace(chr(10),"<br>")}</div>'
        )
    return _shell("markets", "\n".join(parts), title="Markets")


def _lookup_tab(symbol: str = "", message: str = "") -> str:
    """Per-ticker lookup — covers cobol p27."""
    msg_html = (
        f'<div class="section-card" style="border-color:#3fb950;color:#3fb950;">{_h(message)}</div>'
        if message else ""
    )
    sym_clean = "".join(c for c in (symbol or "").upper() if c.isalnum() or c in "-.")[:12]

    form = f"""<h2>Symbol lookup</h2>
<p class="muted">Quick quote + fundamentals for any ticker. Uses the same providers as the engine (yfinance / Massive / Finnhub fallback).</p>
<div class="section-card">
  <form action="/dashboard/lookup" method="get">
    <div class="row">
      <label>Symbol (e.g. AAPL, MSFT, BTC-USD)</label>
      <input type="text" name="symbol" value="{_h(sym_clean)}" placeholder="AAPL" required>
    </div>
    <button type="submit">Look up</button>
  </form>
</div>
"""

    detail = ""
    if sym_clean:
        # Look for a cached lookup; fall back to a hint if missing.
        lk = _load_json(f"lookup_{sym_clean.lower()}.json") or _load_json(f"lookup/{sym_clean}.json")
        if lk:
            d = lk.get("data") or lk
            name = _h(str(d.get("name") or d.get("longName") or sym_clean))
            price = d.get("price") or d.get("last") or d.get("regularMarketPrice")
            chg = d.get("change_pct") or d.get("regularMarketChangePercent")
            sector = _h(str(d.get("sector") or ""))
            industry = _h(str(d.get("industry") or ""))
            mcap = d.get("market_cap") or d.get("marketCap")
            pe = d.get("pe_ratio") or d.get("trailingPE")
            div_y = d.get("dividend_yield") or d.get("dividendYield")
            summary = _h(str(d.get("summary") or d.get("longBusinessSummary") or ""))[:1200]

            def _money(v):
                if v is None:
                    return "—"
                try:
                    return f"${float(v):,.2f}"
                except Exception:
                    return "—"

            def _big(v):
                if v is None:
                    return "—"
                try:
                    f = float(v)
                    if f >= 1e12: return f"${f/1e12:.2f}T"
                    if f >= 1e9: return f"${f/1e9:.2f}B"
                    if f >= 1e6: return f"${f/1e6:.2f}M"
                    return f"${f:,.0f}"
                except Exception:
                    return "—"

            def _pct(v):
                if v is None:
                    return "—"
                try:
                    f = float(v)
                    return f"{f*100:+.2f}%" if abs(f) <= 1 else f"{f:+.2f}%"
                except Exception:
                    return "—"

            detail = f"""<h3>{_h(sym_clean)} — {name}</h3>
<div class="kpi-grid">
  <div class="kpi"><div class="kpi-label">Last price</div><div class="kpi-value">{_money(price)}</div></div>
  <div class="kpi"><div class="kpi-label">Change</div><div class="kpi-value">{_pct(chg)}</div></div>
  <div class="kpi"><div class="kpi-label">Market cap</div><div class="kpi-value">{_big(mcap)}</div></div>
  <div class="kpi"><div class="kpi-label">P/E (trailing)</div><div class="kpi-value">{(f'{float(pe):.2f}' if pe is not None else '—')}</div></div>
  <div class="kpi"><div class="kpi-label">Dividend yield</div><div class="kpi-value">{_pct(div_y)}</div></div>
  <div class="kpi"><div class="kpi-label">Sector</div><div class="kpi-value" style="font-size:14px;">{sector or '—'}</div></div>
</div>
{f'<div class="section-card"><p>{summary}</p></div>' if summary else ''}
"""
        else:
            detail = (
                f'<div class="empty">No cached lookup for <code>{_h(sym_clean)}</code>. '
                f'Run <code>investorclaw lookup {_h(sym_clean)}</code> in the container '
                f'to populate (or wait for the next Regenerate sweep).</div>'
            )

    body = msg_html + form + detail
    return _shell("lookup", body, title="Lookup")


def _reports_tab() -> str:
    reports = _list_recent_eod_reports(50)
    if not reports:
        body = '<h2>Reports</h2><div class="empty">No EOD reports generated yet.</div>'
    else:
        rows = []
        for fname, kb, mtime in reports:
            rows.append(
                f'<tr><td><a href="/reports/{fname}">{fname}</a></td>'
                f'<td>{kb:.0f} KB</td><td class="muted">{mtime}</td></tr>'
            )
        table = (
            "<table><tr><th>Report</th><th>Size</th><th>Generated</th></tr>"
            + "".join(rows)
            + "</table>"
        )
        body = f"""<h2>Reports archive</h2>
<p class="muted">{len(reports)} EOD reports on disk. Click a row to view in this browser, or download the HTML.</p>
<div class="section-card">{table}</div>
<h2>JSON snapshots</h2>
<p class="muted">Raw section data behind the dashboard tabs.</p>
<ul>"""
        json_files = sorted(_glob.glob(os.path.join(REPORTS_DIR, "*.json")))[:100]
        for jp in json_files:
            jname = os.path.basename(jp)
            jsize = os.path.getsize(jp) / 1024
            body += f'<li><a href="/reports/{jname}">{jname}</a> <span class="muted">— {jsize:.1f} KB</span></li>'
        body += "</ul>"
    return _shell("reports", body, title="Reports")


def _priority_badge(priority: str) -> str:
    """Color-coded pill for a key recommendation priority."""
    style_map = {
        "strongly_recommended": ("#f85149", "STRONGLY RECOMMENDED"),
        "required": ("#f85149", "REQUIRED"),
        "recommended": ("#d29922", "RECOMMENDED"),
        "optional": ("#8b949e", "OPTIONAL"),
    }
    color, label = style_map.get(
        priority, ("#8b949e", priority.upper().replace("_", " ") if priority else "OPTIONAL")
    )
    return (
        f'<span style="background:{color};color:#0d1117;padding:2px 8px;'
        f'border-radius:10px;font-size:10px;font-weight:700;'
        f'white-space:nowrap;">{_h(label)}</span>'
    )


def _settings_tab(
    get_keys_status,
    recommendations: list | None = None,
    backups: list | None = None,
    templates: list | None = None,
    routing: dict | None = None,
    diagnostics: dict | None = None,
    message: str = "",
) -> str:
    status = get_keys_status()
    configured = set(status.get("configured", []) or [])
    settable = list(status.get("settable", []) or [])
    msg_html = (
        f'<div class="section-card" style="border-color:#3fb950;color:#3fb950;">{_h(message)}</div>'
        if message
        else ""
    )

    rec_by_name = {r.get("name"): r for r in (recommendations or []) if r.get("name")}
    all_names = sorted(set(settable) | set(rec_by_name.keys()) | configured)

    rows = []
    for name in all_names:
        rec = rec_by_name.get(name, {})
        priority = rec.get("priority", "optional")
        reason = rec.get("reason", "")
        signup_url = rec.get("signup_url", "")
        safe_signup_url = _http_signup_url(signup_url)
        is_configured = name in configured
        js_safe_name = _h(_json.dumps(name))

        status_cell = (
            '<span class="kpi-positive">configured</span>'
            if is_configured
            else '<span class="muted">not set</span>'
        )
        signup_cell = (
            f'<a href="{_h(safe_signup_url)}" target="_blank" rel="noopener noreferrer">sign up</a>'
            if safe_signup_url
            else '<span class="muted">sign up</span>' if signup_url else "—"
        )
        delete_cell = (
            f'<form action="/dashboard/settings/keys/delete" method="post" '
            f'style="margin:0;display:inline;" '
            f'onsubmit="return confirm(\'Delete \' + {js_safe_name} + \'?\');">'
            f'<input type="hidden" name="key_name" value="{_h(name)}">'
            f'<button type="submit" style="background:#21262d;color:#f85149;'
            f'border:1px solid #30363d;padding:4px 10px;border-radius:4px;'
            f'font-size:11px;cursor:pointer;">delete</button>'
            f'</form>'
            if is_configured
            else "—"
        )
        rows.append(
            f"<tr><td><code>{_h(name)}</code></td>"
            f"<td>{_priority_badge(priority)}</td>"
            f"<td>{status_cell}</td>"
            f'<td class="muted" style="max-width:420px;">{_h(reason) or "—"}</td>'
            f"<td>{signup_cell}</td>"
            f"<td>{delete_cell}</td></tr>"
        )

    keys_table = (
        '<div class="section-card" style="overflow-x:auto;"><table>'
        '<tr><th>Key</th><th>Priority</th><th>Status</th>'
        '<th>Why</th><th>Sign up</th><th></th></tr>'
        + "".join(rows)
        + "</table></div>"
    )

    datalist = (
        '<datalist id="settable_keys">'
        + "".join(f'<option value="{_h(k)}">' for k in settable)
        + "</datalist>"
    )

    set_form = f"""<form action="/dashboard/settings/keys" method="post">
  <div class="row">
    <label>Key name (allowlisted — see suggestions)</label>
    <input type="text" name="key_name" list="settable_keys" placeholder="TOGETHER_API_KEY" required>
    {datalist}
  </div>
  <div class="row">
    <label>Value (saved to /data/keys.env mode 0600 inside the container)</label>
    <input type="password" name="key_value" placeholder="tgp_v1_..." required>
  </div>
  <button type="submit">Save key</button>
</form>"""

    # Encrypted backup section (v4.1.40).
    backup_rows_html = ""
    backups = backups or []
    if backups:
        backup_rows = []
        for b in backups:
            fname = _h(b.get("filename", ""))
            size_kb = float(b.get("size_bytes", 0) or 0) / 1024
            created = _h(b.get("created", "—"))
            kdf = _h(b.get("kdf", "—"))
            backup_rows.append(
                f'<tr><td><code>{fname}</code></td>'
                f'<td style="text-align:right;">{size_kb:.1f} KB</td>'
                f'<td class="muted">{created}</td>'
                f'<td class="muted" style="font-size:11px;">{kdf}</td></tr>'
            )
        backup_rows_html = (
            '<div class="section-card"><table>'
            '<tr><th>File</th><th style="text-align:right;">Size</th>'
            '<th>Created</th><th>KDF</th></tr>'
            + "".join(backup_rows)
            + "</table></div>"
        )
    else:
        backup_rows_html = (
            '<div class="empty">No encrypted backups yet. '
            'Create one below — without a backup, keys do not migrate to a new host.</div>'
        )

    backup_form = """<form action="/dashboard/settings/keys_backup" method="post">
  <div class="row">
    <label>Passphrase (min 12 chars — without it the backup is unrecoverable)</label>
    <input type="password" name="passphrase" minlength="12" required>
  </div>
  <div class="row">
    <label>Optional label (alphanumeric / underscore / hyphen, ≤32 chars)</label>
    <input type="text" name="label" pattern="[A-Za-z0-9_-]{0,32}" maxlength="32">
  </div>
  <button type="submit">Create encrypted backup</button>
</form>"""

    restore_form = """<form action="/dashboard/settings/keys_restore" method="post"
  onsubmit="return confirm('Restore will overwrite /data/keys.env. Continue?');">
  <div class="row">
    <label>Passphrase</label>
    <input type="password" name="passphrase" required>
  </div>
  <div class="row">
    <label>Backup file (leave blank for most recent)</label>
    <input type="text" name="backup_path" placeholder="(auto-pick most recent)">
  </div>
  <button type="submit" style="background:#d29922;">Restore from backup</button>
</form>"""

    # Portfolio upload — list current files + form to add a new one.
    portfolio_files = []
    pdir = pathlib.Path(os.environ.get("IC_PORTFOLIO_DIR", "/data/portfolios"))
    if pdir.is_dir():
        try:
            for p in sorted(pdir.iterdir()):
                if p.is_file() and not p.name.startswith("."):
                    portfolio_files.append((p.name, p.stat().st_size))
        except OSError:
            pass

    files_rows = "".join(
        f'<tr><td><code>{_h(name)}</code></td>'
        f'<td style="text-align:right;">{size/1024:.1f} KB</td></tr>'
        for name, size in portfolio_files
    ) or '<tr><td colspan="2" class="muted">No portfolio files uploaded yet.</td></tr>'

    upload_max_mb = _MAX_UPLOAD_BYTES // (1024 * 1024)
    upload_form = f"""<form action="/dashboard/upload" method="post" enctype="multipart/form-data">
  <div class="row">
    <label>Portfolio file (CSV, XLSX, PDF — saved to <code>/data/portfolios/</code>; max {upload_max_mb} MB)</label>
    <input type="file" name="portfolio_file" required accept=".csv,.tsv,.xls,.xlsx,.pdf,.json,.ofx,.qfx">
  </div>
  <button type="submit">Upload &amp; refresh</button>
</form>"""

    # Pre-built templates — let first-time users start with a canonical
    # allocation (Boglehead 3-fund, 60/40, All-Weather, etc.) before they
    # have a real broker statement to upload.
    templates = templates or []
    if templates:
        template_cards = []
        for t in templates:
            slug = _h(t.get("slug", ""))
            js_safe_name = _h(_json.dumps(t.get("name", "")))
            template_cards.append(
                f'<div style="background:#161b22;border:1px solid #30363d;'
                f'border-radius:6px;padding:14px;margin-bottom:12px;">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;'
                f'gap:12px;flex-wrap:wrap;">'
                f'<div style="flex:1;min-width:240px;">'
                f'<h3 style="margin:0 0 4px 0;font-size:15px;">{_h(t.get("name", ""))}</h3>'
                f'<p class="muted" style="margin:0 0 6px 0;">{_h(t.get("description", ""))}</p>'
                f'<p style="margin:0;font-size:12px;color:#8b949e;">'
                f'<code>{_h(t.get("positions", ""))}</code> '
                f'<span style="opacity:0.7;">— ~${t.get("notional", 0):,.0f} starter</span>'
                f'</p>'
                f'</div>'
                f'<form action="/dashboard/settings/template" method="post" style="margin:0;"'
                f' onsubmit="return confirm(\'Drop the \' + {js_safe_name} + '
                f'\' template into /data/portfolios/ and queue regenerate?\');">'
                f'<input type="hidden" name="slug" value="{slug}">'
                f'<button type="submit" style="background:#1f6feb;color:#fff;border:0;'
                f'padding:8px 14px;border-radius:6px;font-weight:600;cursor:pointer;">'
                f'Load template</button>'
                f'</form>'
                f'</div>'
                f'<details style="margin-top:8px;">'
                f'<summary style="cursor:pointer;color:#8b949e;font-size:12px;">Why this allocation?</summary>'
                f'<p style="margin:6px 0 0 0;font-size:13px;color:#c9d1d9;">{_h(t.get("rationale", ""))}</p>'
                f'</details>'
                f'</div>'
            )
        templates_block = "".join(template_cards)
    else:
        templates_block = (
            '<div class="empty">No starter templates available.</div>'
        )

    # Provider routing section.
    routing = routing or {}
    valid_providers = routing.get("valid_providers", []) or []
    current_primary = (routing.get("primary") or "auto").lower()
    current_chain = routing.get("fallback_chain") or []

    primary_options = ['<option value="auto">auto (engine default)</option>']
    for p in valid_providers:
        sel = " selected" if p == current_primary else ""
        primary_options.append(
            f'<option value="{_h(p)}"{sel}>{_h(p)}</option>'
        )
    chain_value = _h(",".join(current_chain))

    routing_form = f"""<form action="/dashboard/settings/provider_routing" method="post">
  <div class="row">
    <label>Primary price provider (or <code>auto</code> for engine routing)</label>
    <select name="primary">
      {''.join(primary_options)}
    </select>
  </div>
  <div class="row">
    <label>Fallback chain (comma-separated, in order)</label>
    <input type="text" name="fallback_chain" value="{chain_value}"
      placeholder="yfinance,massive,finnhub" pattern="[a-zA-Z0-9_,\\s]*">
    <p class="muted" style="margin-top:4px;font-size:12px;">
      Valid: {", ".join(_h(p) for p in valid_providers) or "—"}.
      Empty clears the override and uses the engine's per-operation
      routing table.
    </p>
  </div>
  <button type="submit">Save routing</button>
</form>"""

    # Provider diagnostics — render the "last-test" panel with a
    # per-provider Test button. `diagnostics` is a {provider_name:
    # last_result_dict} mapping; missing entries render as "not tested
    # yet". Only diagnostics for providers in the supported registry
    # surface here.
    diagnostics = diagnostics or {}
    supported = diagnostics.get("supported_providers") or []
    last_results = diagnostics.get("results") or {}

    diag_rows = []
    for prov in supported:
        result = last_results.get(prov) or {}
        ok = result.get("ok")
        configured = result.get("configured", True)
        latency_ms = result.get("latency_ms")
        err = result.get("error")
        sample = result.get("response_sample")
        checked_at = result.get("checked_at")

        if not result:
            badge = '<span class="muted">not tested yet</span>'
            detail_text = ""
        elif ok:
            badge = ('<span style="background:#3fb950;color:#0d1117;'
                     'padding:2px 8px;border-radius:10px;font-size:10px;'
                     'font-weight:700;">OK</span>')
            bits = []
            if latency_ms is not None:
                bits.append(f"{latency_ms} ms")
            if sample:
                bits.append(_h(str(sample)[:100]))
            detail_text = " · ".join(bits)
        elif not configured:
            badge = ('<span style="background:#8b949e;color:#0d1117;'
                     'padding:2px 8px;border-radius:10px;font-size:10px;'
                     'font-weight:700;">UNCONFIGURED</span>')
            detail_text = _h(str(err or "")[:200])
        else:
            badge = ('<span style="background:#f85149;color:#0d1117;'
                     'padding:2px 8px;border-radius:10px;font-size:10px;'
                     'font-weight:700;">FAIL</span>')
            bits = []
            if latency_ms is not None and latency_ms > 0:
                bits.append(f"{latency_ms} ms")
            if err:
                bits.append(_h(str(err)[:200]))
            detail_text = " · ".join(bits)

        ts_text = (
            f'<span class="muted" style="font-size:11px;">@ {_h(checked_at)}</span>'
            if checked_at else ""
        )

        diag_rows.append(
            f'<tr>'
            f'<td><code>{_h(prov)}</code></td>'
            f'<td>{badge}</td>'
            f'<td class="muted" style="font-size:12px;max-width:480px;">'
            f'{detail_text}'
            f'{ts_text}'
            f'</td>'
            f'<td>'
            f'<form action="/dashboard/settings/diagnostics" method="post" '
            f'style="margin:0;display:inline;">'
            f'<input type="hidden" name="provider" value="{_h(prov)}">'
            f'<button type="submit" style="background:#21262d;color:#58a6ff;'
            f'border:1px solid #30363d;padding:4px 10px;border-radius:4px;'
            f'font-size:11px;cursor:pointer;">Test</button>'
            f'</form>'
            f'</td>'
            f'</tr>'
        )

    if diag_rows:
        diagnostics_block = (
            '<div class="section-card" style="overflow-x:auto;"><table>'
            '<tr><th>Provider</th><th>Status</th><th>Detail</th><th></th></tr>'
            + "".join(diag_rows) + "</table></div>"
        )
    else:
        diagnostics_block = (
            '<div class="empty">Diagnostics not wired (provider_diagnostics module unavailable).</div>'
        )

    if current_primary == "auto" and not current_chain:
        routing_state = (
            '<p class="muted">No override active — ic-engine uses its '
            'default per-operation routing table (yfinance + massive + '
            'finnhub depending on op type).</p>'
        )
    else:
        bits = []
        if current_primary != "auto":
            bits.append(
                f'Primary: <code style="color:#58a6ff;">{_h(current_primary)}</code>'
            )
        if current_chain:
            bits.append(
                'Fallback chain: <code>'
                + " → ".join(_h(c) for c in current_chain)
                + "</code>"
            )
        routing_state = (
            '<div class="section-card"><p>'
            + " · ".join(bits)
            + "</p></div>"
        )

    body = f"""<h2>Settings — provider keys</h2>
{msg_html}
<p class="muted">Keys persist to <code>/data/keys.env</code> inside the named Docker volume,
mode 0600. Allowlisted names only — arbitrary key names are rejected. Priority is sized
to your portfolio (more holdings → MASSIVE_API_KEY becomes strongly recommended).</p>

{keys_table}

<h2>Add or update a key</h2>
<div class="section-card">
{set_form}
</div>

<h2>Encrypted key backup</h2>
<p class="muted">Backups encrypt <code>/data/keys.env</code> with scrypt + AES-256-GCM and write
armored ASCII files to <code>/data/backups/</code>. Use them to migrate keys across hosts —
the file is safe to scp / email / clipboard. Passphrase is enforced server-side; without
it, the backup is permanently unrecoverable.</p>
{backup_rows_html}

<h3>Create a backup</h3>
<div class="section-card">
{backup_form}
</div>

<h3>Restore from a backup</h3>
<div class="section-card">
{restore_form}
</div>

<h2>Portfolio files</h2>
<p class="muted">Files are auto-detected by <code>investorclaw setup</code>. Drop a CSV / XLSX / PDF
broker statement here; the upload triggers a refresh and the new positions appear in the Holdings tab.</p>
<div class="section-card">
  <table>
    <tr><th>File</th><th style="text-align:right;">Size</th></tr>
    {files_rows}
  </table>
</div>

<h2>Upload a portfolio file</h2>
<div class="section-card">
{upload_form}
</div>

<h2>Starter templates</h2>
<p class="muted">No broker statement yet? Load a canonical allocation
to explore the dashboard. Each template drops a starter CSV into
<code>/data/portfolios/</code> and queues a regenerate. Templates are
not investment advice — they are well-known canonical allocations
(Boglehead, 60/40, Ray-Dalio All-Weather) surfaced as starting points
only.</p>
{templates_block}

<h2>Provider routing</h2>
<p class="muted">Override ic-engine's price-data fallback chain. Use
this when you have a premium provider key (e.g. Massive)
and want it consulted first for every price + history fetch. Empty
or <code>auto</code> = engine default routing. Note: setting
<code>MASSIVE_API_KEY</code> via the key forms auto-pins primary to
<code>massive</code> (only when primary is currently <code>auto</code>;
explicit overrides are preserved).</p>
{routing_state}
<div class="section-card">
{routing_form}
</div>

<h2>Configuration snapshot</h2>
<p class="muted">Download a JSON snapshot containing all portfolios,
stonkmode persona state, provider routing, and configured key NAMES
(values are NOT included — keys move between hosts via the encrypted
backup above). Restoring overwrites portfolios + routing + persona
on the target.</p>
<div class="section-card">
  <p>
    <a href="/dashboard/settings/export.json" class="primary"
       style="background:#1f6feb;">Download config snapshot</a>
  </p>
  <p class="muted" style="font-size:12px;">
    Filename: <code>investorclaw-config-&lt;version&gt;-&lt;timestamp&gt;.json</code>.
    Combine with the encrypted keys backup above for a complete
    cross-host migration kit.
  </p>
</div>

<h3>Restore from snapshot</h3>
<div class="section-card">
  <form action="/dashboard/settings/import_config" method="post"
    enctype="multipart/form-data"
    onsubmit="return confirm('Restore will overwrite portfolios + routing + stonkmode on this host. Continue?');">
    <div class="row">
      <label>Snapshot JSON file</label>
      <input type="file" name="snapshot_file" required accept=".json,application/json">
    </div>
    <button type="submit" style="background:#d29922;">Restore from snapshot</button>
  </form>
</div>

<h2>Provider diagnostics</h2>
<p class="muted">Verify each configured provider answers a real
request. Tests fire on demand only — no automatic background polling
— so the rate-limited free-tier providers (NewsAPI 100/day,
AlphaVantage 5/min, MarketAux 100/day) don't burn quota on every
dashboard load.</p>
{diagnostics_block}
"""
    return _shell("settings", body, title="Settings")


def _about_tab() -> str:
    version = _running_version()
    body = f"""<h2>About InvestorClaw</h2>
<div class="section-card">
  <p><strong>InvestorClaw {_h(version)}</strong> — deterministic-first portfolio analyzer.</p>
  <p>Real money math, no LLM math. Holdings, performance (Sharpe + Sortino, max drawdown,
  beta, value-at-risk), bond duration, FRED yield curves, sector breakdowns, scenario
  rebalancing. The engine is pure Python; the narrator is an LLM with strict
  no-fabrication validation against an HMAC-signed envelope.</p>
</div>

<h2>License</h2>
<div class="section-card">
  <p>Substantive code (bridge, dashboard, Dockerfile, tests): <strong>Apache 2.0</strong>.</p>
  <p>Distribution-edge artifacts (SKILL.md, compose.yml, install.yaml, agent-skills/**):
  <strong>MIT-0</strong> (MIT No Attribution).</p>
</div>

<h2>Repos</h2>
<ul>
  <li><a href="https://github.com/argonautsystems/InvestorClaw">argonautsystems/InvestorClaw</a> — umbrella project (this repo)</li>
  <li><a href="https://github.com/mnemos-os/mnemos-ic-runtime">mnemos-os/mnemos-ic-runtime</a> — v4.x dockerized-skill runtime</li>
  <li><a href="https://github.com/argonautsystems/ic-engine">argonautsystems/ic-engine</a> — Python engine source</li>
</ul>

<h2>Glossary</h2>
<p class="muted">Concept definitions for portfolio metrics shown in the dashboard tabs.</p>
<div class="section-card">
  <dl style="margin:0;">
    <dt><strong>Sharpe ratio</strong></dt>
    <dd class="muted" style="margin-bottom:8px;">Excess return per unit of total volatility. Higher is better; 1.0 is solid, 2.0+ is excellent.</dd>
    <dt><strong>Sortino ratio</strong></dt>
    <dd class="muted" style="margin-bottom:8px;">Like Sharpe, but only penalizes downside volatility. More representative for asymmetric portfolios.</dd>
    <dt><strong>Maximum drawdown</strong></dt>
    <dd class="muted" style="margin-bottom:8px;">Largest peak-to-trough decline over the analyzed window. Smaller (closer to 0) is better.</dd>
    <dt><strong>Beta</strong></dt>
    <dd class="muted" style="margin-bottom:8px;">Sensitivity to a benchmark (typically S&amp;P 500). β=1.0 moves with the market, &gt;1 amplifies, &lt;1 dampens.</dd>
    <dt><strong>Value-at-Risk (VaR 95%)</strong></dt>
    <dd class="muted" style="margin-bottom:8px;">Estimated maximum loss over one period at 95% confidence. The 5% tail beyond this is your "bad day".</dd>
    <dt><strong>Yield-to-maturity (YTM)</strong></dt>
    <dd class="muted" style="margin-bottom:8px;">Total annualized return on a bond if held to maturity, factoring in coupons and price.</dd>
    <dt><strong>Duration</strong></dt>
    <dd class="muted" style="margin-bottom:8px;">Bond's sensitivity to interest-rate moves. Higher duration = more rate risk.</dd>
    <dt><strong>Sharpe-maximizing allocation</strong></dt>
    <dd class="muted" style="margin-bottom:8px;">Weights chosen to maximize risk-adjusted return; computed via PyPortfolioOpt against historical returns + covariance.</dd>
    <dt><strong>Minimum-volatility allocation</strong></dt>
    <dd class="muted" style="margin-bottom:8px;">Weights chosen to minimize portfolio variance — the safest mix of your symbols, not necessarily the highest-return.</dd>
  </dl>
</div>

<h2>First-time setup</h2>
<div class="section-card">
  <ol style="margin:0;padding-left:20px;color:#c9d1d9;">
    <li style="margin-bottom:6px;">Drop your broker statement (CSV / XLSX / PDF) into <code>/data/portfolios/</code> via the <a href="/dashboard/settings">Settings</a> upload form.</li>
    <li style="margin-bottom:6px;">Add at least one provider key on <a href="/dashboard/settings">Settings</a> — TOGETHER for narrative, FINNHUB for analyst, NEWSAPI/MARKETAUX for news.</li>
    <li style="margin-bottom:6px;">Click <strong>↻ Regenerate</strong> on the <a href="/">Overview</a> tab — the engine refreshes every section (~3-5 min for a 200-position portfolio).</li>
    <li>Browse the tabs: Holdings → Performance → Bonds → Analyst → News.</li>
  </ol>
</div>

<h2>Disclaimer</h2>
<div class="section-card" style="border-color:#d29922;color:#e3b341;">
  <p><strong>Educational analysis only — NOT investment advice.</strong></p>
  <p>InvestorClaw is not a fiduciary advisor and does not execute trades, move money,
  or authenticate to brokerage accounts. Always consult a qualified financial advisor
  before acting on any analysis output.</p>
</div>
"""
    return _shell("about", body, title="About")


# ----------------------------------------------------------------------------
# FastAPI wiring
# ----------------------------------------------------------------------------


async def _safe_call(fn):
    """Invoke a sync-or-async zero-arg callable; swallow exceptions silently
    so a background-fired task never crashes the event loop.
    """
    try:
        result = fn()
        import inspect as _inspect
        if _inspect.iscoroutine(result):
            await result
    except Exception:
        # Background task — log via structlog if available, else swallow.
        try:
            import structlog
            structlog.get_logger("dashboard").warning(
                "background_task_failed", task=getattr(fn, "__name__", str(fn))
            )
        except Exception:
            pass


async def _maybe_await(value):
    """Await `value` if it's a coroutine; otherwise return it."""
    import inspect as _inspect
    if _inspect.iscoroutine(value):
        return await value
    return value


def attach_to(
    app: FastAPI,
    get_init_state,
    get_keys_status,
    set_key,
    regenerate=None,
    get_keys_recommend=None,
    delete_key=None,
    backup_keys=None,
    restore_keys=None,
    list_backups=None,
    list_templates=None,
    apply_template=None,
    get_provider_routing=None,
    set_provider_routing=None,
    export_config=None,
    import_config=None,
    diagnostics_check=None,
    diagnostics_supported=None,
) -> None:
    """Mount all dashboard tab routes on the given FastAPI app.

    Parameters
    ----------
    get_init_state : callable -> dict
        Returns the engine init snapshot (state, ready, current_stage, elapsed_ms).
    get_keys_status : callable -> dict
        Returns ``{"configured": [...], "settable": [...]}``.
    set_key : callable(name, value) -> dict
        Persists a single key, returns ``{"configured": [...], "rejected": [...]}``.
    regenerate : optional async callable() -> dict
        Triggers a full refresh + analyzer sweep. Called from the Regenerate
        button on the Overview tab. Should return quickly (background-fired).
    get_keys_recommend : optional callable -> dict
        Returns ``{"recommendations": [...]}`` with size-aware priority +
        rationale + signup_url per key (v4.1.38 #44 / dashboard #94).
    delete_key : optional callable(name) -> dict
        Deletes a single configured key.
    backup_keys : optional callable(passphrase, label) -> dict
        Creates an encrypted backup of /data/keys.env (v4.1.40 #96).
    restore_keys : optional callable(passphrase, backup_path) -> dict
        Restores keys from an encrypted backup file.
    list_backups : optional callable -> dict
        Returns ``{"backups": [...]}`` with metadata (no decrypt).
    list_templates : optional callable -> list[dict]
        Returns the pre-built portfolio template registry for the
        Settings tab "Starter templates" section. Each entry: ``slug``,
        ``name``, ``description``, ``rationale``, ``positions`` summary.
    apply_template : optional callable(slug) -> dict
        Writes the template's CSV into ``/data/portfolios/`` and returns
        ``{"applied": True, "filename"}`` or ``{"error": "..."}``.
    get_provider_routing : optional callable -> dict
        Returns ``{"primary", "fallback_chain", "valid_providers", ...}``
        from ``provider_routing.load_routing`` (v4.2.0).
    set_provider_routing : optional callable(primary, fallback_chain) -> dict
        Persists the routing config and mirrors it into ``os.environ``.
        Returns ``{"saved": True, ...}`` or ``{"error": "..."}``.
    export_config : optional async callable -> dict
        Returns the v4.3.0 config-snapshot JSON (delegates to
        ``portfolio_export``). Surfaced as a browser download via
        GET /dashboard/settings/export.json.
    import_config : optional async callable(snapshot: dict) -> dict
        Restores from a config-snapshot JSON (delegates to
        ``portfolio_import``). Surfaced as a multipart upload via
        POST /dashboard/settings/import_config.
    diagnostics_check : optional async callable(provider_name: str) -> dict
        Runs a single provider health check. Wired to the per-row
        "Test" button on the Settings tab "Provider diagnostics"
        table (v4.3.1). Tests fire on demand only — never auto-poll.
    diagnostics_supported : optional callable -> list[str]
        Returns the list of provider names with health-check support
        (mirrors `provider_diagnostics.supported_providers()`).

    Diagnostic results persist in an in-memory dict scoped to this
    closure. Restarting the bridge clears the cache; the user can
    re-run any check on demand.
    CSRF policy
        Mutating dashboard POSTs compare any Origin or Referer header against
        the request Host and reject mismatches with a 303 redirect. This keeps
        the no-session dashboard compatible with IC_DASHBOARD_BIND deployments
        while accepting same-origin browser submissions.
    """

    # In-memory cache of last diagnostic-check result per provider.
    # Scoped to this closure so a bridge restart clears it; persistence
    # would mean cross-request results leaking on shared deployments.
    _diagnostics_results: dict[str, dict] = {}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def overview(message: str = "") -> HTMLResponse:
        return HTMLResponse(_overview(get_init_state, message=message))

    @app.get("/dashboard/", include_in_schema=False)
    async def dashboard_root() -> RedirectResponse:
        return RedirectResponse(url="/", status_code=303)

    @app.get("/dashboard/holdings", response_class=HTMLResponse, include_in_schema=False)
    async def holdings() -> HTMLResponse:
        return HTMLResponse(_holdings_tab())

    @app.get("/dashboard/performance", response_class=HTMLResponse, include_in_schema=False)
    async def performance() -> HTMLResponse:
        return HTMLResponse(_performance_tab())

    @app.get("/dashboard/whatchanged", response_class=HTMLResponse, include_in_schema=False)
    async def whatchanged() -> HTMLResponse:
        return HTMLResponse(_whatchanged_tab())

    @app.get("/dashboard/scenarios", response_class=HTMLResponse, include_in_schema=False)
    async def scenarios() -> HTMLResponse:
        return HTMLResponse(_scenarios_tab())

    @app.get("/dashboard/bonds", response_class=HTMLResponse, include_in_schema=False)
    async def bonds() -> HTMLResponse:
        return HTMLResponse(_bonds_tab())

    @app.get("/dashboard/analyst", response_class=HTMLResponse, include_in_schema=False)
    async def analyst() -> HTMLResponse:
        return HTMLResponse(_analyst_tab())

    @app.get("/dashboard/news", response_class=HTMLResponse, include_in_schema=False)
    async def news() -> HTMLResponse:
        return HTMLResponse(_news_tab())

    @app.get("/dashboard/optimize", response_class=HTMLResponse, include_in_schema=False)
    async def optimize() -> HTMLResponse:
        return HTMLResponse(_optimize_tab())

    @app.get("/dashboard/cashflow", response_class=HTMLResponse, include_in_schema=False)
    async def cashflow() -> HTMLResponse:
        return HTMLResponse(_cashflow_tab())

    @app.get("/dashboard/peer", response_class=HTMLResponse, include_in_schema=False)
    async def peer() -> HTMLResponse:
        return HTMLResponse(_peer_tab())

    @app.get("/dashboard/markets", response_class=HTMLResponse, include_in_schema=False)
    async def markets() -> HTMLResponse:
        return HTMLResponse(_markets_tab())

    @app.get("/dashboard/lookup", response_class=HTMLResponse, include_in_schema=False)
    async def lookup(symbol: str = "", message: str = "") -> HTMLResponse:
        return HTMLResponse(_lookup_tab(symbol=symbol, message=message))

    @app.get("/dashboard/synthesis", response_class=HTMLResponse, include_in_schema=False)
    async def synthesis() -> HTMLResponse:
        return HTMLResponse(_synthesis_tab())

    @app.get("/dashboard/reports", response_class=HTMLResponse, include_in_schema=False)
    async def reports() -> HTMLResponse:
        return HTMLResponse(_reports_tab())

    @app.get("/dashboard/settings", response_class=HTMLResponse, include_in_schema=False)
    async def settings(message: str = "") -> HTMLResponse:
        status = await _maybe_await(get_keys_status())
        recommendations: list = []
        if get_keys_recommend is not None:
            try:
                rec_result = await _maybe_await(get_keys_recommend())
                recommendations = rec_result.get("recommendations") or []
            except Exception:
                recommendations = []
        backups: list = []
        if list_backups is not None:
            try:
                backups_result = await _maybe_await(list_backups())
                backups = backups_result.get("backups") or []
            except Exception:
                backups = []
        templates: list = []
        if list_templates is not None:
            try:
                templates = await _maybe_await(list_templates()) or []
            except Exception:
                templates = []
        routing: dict = {}
        if get_provider_routing is not None:
            try:
                routing = await _maybe_await(get_provider_routing()) or {}
            except Exception:
                routing = {}
        diagnostics: dict = {}
        if diagnostics_supported is not None:
            try:
                supported = await _maybe_await(diagnostics_supported())
                diagnostics = {
                    "supported_providers": list(supported) if supported else [],
                    "results": dict(_diagnostics_results),
                }
            except Exception:
                diagnostics = {}
        return HTMLResponse(
            _settings_tab(
                lambda: status,
                recommendations=recommendations,
                backups=backups,
                templates=templates,
                routing=routing,
                diagnostics=diagnostics,
                message=message,
            )
        )

    @app.post("/dashboard/settings/keys", include_in_schema=False)
    async def settings_save_key(request: Request) -> RedirectResponse:
        if redirect := _csrf_redirect(request, "/dashboard/settings"):
            return redirect
        form = await request.form()
        name = (form.get("key_name") or "").strip()
        value = (form.get("key_value") or "").strip()
        if not name or not value:
            return RedirectResponse(
                url="/dashboard/settings?message=Missing+name+or+value",
                status_code=303,
            )
        result = await _maybe_await(set_key(name, value))
        rejected = result.get("rejected") or []
        if rejected:
            msg = f"Key {name} rejected (not in allowlist)"
        else:
            msg = f"Saved {name}"
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/dashboard/settings?message={quote(msg)}",
            status_code=303,
        )

    @app.post("/dashboard/settings/keys/delete", include_in_schema=False)
    async def settings_delete_key(request: Request) -> RedirectResponse:
        from urllib.parse import quote
        if redirect := _csrf_redirect(request, "/dashboard/settings"):
            return redirect
        form = await request.form()
        name = (form.get("key_name") or "").strip()
        if not name:
            return RedirectResponse(
                url="/dashboard/settings?message=Missing+key+name",
                status_code=303,
            )
        if delete_key is None:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Delete not wired')}",
                status_code=303,
            )
        status = await _maybe_await(get_keys_status())
        settable = set(status.get("settable", []) or [])
        if name not in settable:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote(f'Key {name} rejected (not in allowlist)')}",
                status_code=303,
            )
        result = await _maybe_await(delete_key(name))
        if result.get("deleted"):
            msg = f"Deleted {name}"
        else:
            msg = f"Could not delete {name}: {result.get('detail') or result.get('error') or 'unknown'}"
        return RedirectResponse(
            url=f"/dashboard/settings?message={quote(msg)}",
            status_code=303,
        )

    @app.post("/dashboard/settings/keys_backup", include_in_schema=False)
    async def settings_keys_backup(request: Request) -> RedirectResponse:
        from urllib.parse import quote
        if redirect := _csrf_redirect(request, "/dashboard/settings"):
            return redirect
        form = await request.form()
        passphrase = form.get("passphrase") or ""
        label = (form.get("label") or "").strip()
        if backup_keys is None:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Backup not wired')}",
                status_code=303,
            )
        result = await _maybe_await(backup_keys(passphrase, label))
        if "error" in result:
            err = result.get("detail") or result.get("error")
            msg = f"Backup failed: {err}"
        else:
            fname = result.get("filename", "backup")
            msg = f"Backup created: {fname}"
        return RedirectResponse(
            url=f"/dashboard/settings?message={quote(msg)}",
            status_code=303,
        )

    @app.post("/dashboard/settings/keys_restore", include_in_schema=False)
    async def settings_keys_restore(request: Request) -> RedirectResponse:
        from urllib.parse import quote
        if redirect := _csrf_redirect(request, "/dashboard/settings"):
            return redirect
        form = await request.form()
        passphrase = form.get("passphrase") or ""
        backup_path = (form.get("backup_path") or "").strip()
        if restore_keys is None:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Restore not wired')}",
                status_code=303,
            )
        result = await _maybe_await(restore_keys(passphrase, backup_path))
        if "error" in result:
            err = result.get("hint") or result.get("detail") or result.get("error")
            msg = f"Restore failed: {err}"
        else:
            n = len(result.get("key_names") or [])
            msg = f"Restored {n} key{'s' if n != 1 else ''} from backup"
        return RedirectResponse(
            url=f"/dashboard/settings?message={quote(msg)}",
            status_code=303,
        )

    @app.post("/dashboard/settings/template", include_in_schema=False)
    async def settings_apply_template(request: Request) -> RedirectResponse:
        from urllib.parse import quote
        if redirect := _csrf_redirect(request, "/dashboard/settings"):
            return redirect
        form = await request.form()
        slug = (form.get("slug") or "").strip()
        if not slug:
            return RedirectResponse(
                url="/dashboard/settings?message=Missing+template+slug",
                status_code=303,
            )
        if apply_template is None:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Templates not wired')}",
                status_code=303,
            )
        result = await _maybe_await(apply_template(slug))
        if "error" in result:
            err = result.get("detail") or result.get("error")
            msg = f"Template apply failed: {err}"
        else:
            fname = result.get("filename", "template.csv")
            name = result.get("name", slug)
            n_rows = result.get("rows", 0)
            msg = f"Loaded {name} ({n_rows} positions) → {fname}; refresh queued"
            # Fire setup in the background — same pattern as upload.
            if regenerate is not None:
                try:
                    import asyncio as _asyncio
                    _asyncio.create_task(_safe_call(regenerate))
                except Exception:
                    pass
        return RedirectResponse(
            url=f"/dashboard/settings?message={quote(msg)}",
            status_code=303,
        )

    @app.post("/dashboard/settings/provider_routing", include_in_schema=False)
    async def settings_provider_routing(request: Request) -> RedirectResponse:
        from urllib.parse import quote
        if redirect := _csrf_redirect(request, "/dashboard/settings"):
            return redirect
        if set_provider_routing is None:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Provider routing not wired')}",
                status_code=303,
            )
        form = await request.form()
        primary = (form.get("primary") or "").strip()
        chain_raw = (form.get("fallback_chain") or "").strip()
        chain = [c.strip() for c in chain_raw.split(",") if c.strip()]
        result = await _maybe_await(set_provider_routing(primary, chain))
        if "error" in result:
            err = result.get("detail") or result.get("error")
            msg = f"Routing save failed: {err}"
        else:
            saved_primary = result.get("primary", primary)
            saved_chain = result.get("fallback_chain", chain)
            chain_str = ",".join(saved_chain) if saved_chain else "(none)"
            msg = f"Routing saved — primary: {saved_primary}, chain: {chain_str}"
        return RedirectResponse(
            url=f"/dashboard/settings?message={quote(msg)}",
            status_code=303,
        )

    @app.post("/dashboard/settings/diagnostics", include_in_schema=False)
    async def settings_run_diagnostic(request: Request) -> RedirectResponse:
        from urllib.parse import quote
        if redirect := _csrf_redirect(request, "/dashboard/settings"):
            return redirect
        if diagnostics_check is None:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Diagnostics not wired')}",
                status_code=303,
            )
        form = await request.form()
        provider = (form.get("provider") or "").strip()
        if not provider:
            return RedirectResponse(
                url="/dashboard/settings?message=Missing+provider",
                status_code=303,
            )
        result = await _maybe_await(diagnostics_check(provider))
        # Cache the result for the next GET render. Errors are still
        # cached so the user sees what failed last.
        if isinstance(result, dict):
            _diagnostics_results[provider] = result
            ok = result.get("ok")
            if ok:
                latency = result.get("latency_ms")
                msg = f"{provider}: OK ({latency} ms)"
            elif not result.get("configured", True):
                msg = f"{provider}: unconfigured ({result.get('error')})"
            else:
                msg = f"{provider}: FAIL ({result.get('error')})"
        else:
            msg = f"{provider}: invalid diagnostic result"
        return RedirectResponse(
            url=f"/dashboard/settings?message={quote(msg)}",
            status_code=303,
        )

    @app.get("/dashboard/settings/export.json", include_in_schema=False)
    async def settings_export_config(request: Request):
        """Stream the portfolio_export() JSON snapshot as a downloadable file.

        Read-only endpoint — GET, no CSRF needed (no state mutation).
        Filename includes timestamp + version so the user can keep
        multiple snapshots side-by-side.
        """
        from fastapi.responses import JSONResponse
        if export_config is None:
            return JSONResponse(
                {"error": "config_export_not_wired"}, status_code=503
            )
        snapshot = await _maybe_await(export_config())
        version = re.sub(
            r"[^A-Za-z0-9._-]",
            "",
            str(snapshot.get("engine_version") or _running_version()),
        ) or "unknown"
        ts = re.sub(
            r"[^A-Za-z0-9._-]",
            "",
            _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        ) or "unknown"
        filename = f"investorclaw-config-{version}-{ts}.json"
        return JSONResponse(
            snapshot,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.post("/dashboard/settings/import_config", include_in_schema=False)
    async def settings_import_config(request: Request) -> RedirectResponse:
        from urllib.parse import quote
        if redirect := _csrf_redirect(request, "/dashboard/settings"):
            return redirect
        if import_config is None:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Config import not wired')}",
                status_code=303,
            )
        try:
            form = await request.form()
        except Exception as e:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Form parse failed: ' + str(e))}",
                status_code=303,
            )
        upload = form.get("snapshot_file")
        if upload is None or not getattr(upload, "filename", ""):
            return RedirectResponse(
                url="/dashboard/settings?message=No+snapshot+file+selected",
                status_code=303,
            )
        # Cap covers the full multipart snapshot upload; portfolio_export's
        # per-portfolio-file cap in upgrade.py is smaller (~5 MB).
        max_bytes = _MAX_UPLOAD_BYTES
        chunks = []
        total = 0
        while True:
            chunk = await upload.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return RedirectResponse(
                    url=f"/dashboard/settings?message={quote(f'Snapshot too large: > {max_bytes//1024//1024} MB cap')}",
                    status_code=303,
                )
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            snapshot = _json.loads(body)
        except (_json.JSONDecodeError, RecursionError) as e:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Snapshot JSON parse failed: ' + str(e))}",
                status_code=303,
            )
        except Exception as e:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Snapshot JSON parse failed: ' + str(e))}",
                status_code=303,
            )
        result = await _maybe_await(import_config(snapshot))
        if "error" in result:
            err = result.get("detail") or result.get("error")
            msg = f"Snapshot import failed: {err}"
        else:
            imported = result.get("imported", {})
            n_p = imported.get("portfolios", 0)
            n_sm = "yes" if imported.get("stonkmode") else "no"
            n_pr = "yes" if imported.get("provider_routing") else "no"
            warns = len(result.get("warnings") or [])
            msg = (
                f"Snapshot imported — portfolios:{n_p}, stonkmode:{n_sm}, "
                f"routing:{n_pr}, warnings:{warns}"
            )
            # Fire regenerate sweep so the new state is materialized.
            if regenerate is not None:
                try:
                    import asyncio as _asyncio
                    _asyncio.create_task(_safe_call(regenerate))
                except Exception:
                    pass
        return RedirectResponse(
            url=f"/dashboard/settings?message={quote(msg)}",
            status_code=303,
        )

    @app.get("/dashboard/about", response_class=HTMLResponse, include_in_schema=False)
    async def about() -> HTMLResponse:
        return HTMLResponse(_about_tab())

    @app.post("/dashboard/upload", include_in_schema=False)
    async def upload_portfolio(request: Request) -> RedirectResponse:
        """Multipart upload — write to /data/portfolios/, then trigger setup."""
        from urllib.parse import quote
        if redirect := _csrf_redirect(request, "/dashboard/settings"):
            return redirect
        try:
            form = await request.form()
        except Exception as e:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Upload parse failed: ' + str(e))}",
                status_code=303,
            )
        upload = form.get("portfolio_file")
        if upload is None or not getattr(upload, "filename", ""):
            return RedirectResponse(
                url="/dashboard/settings?message=No+file+selected",
                status_code=303,
            )
        # Sanitize filename — strip path components, allow alphanumeric + . _ -
        raw_name = os.path.basename(upload.filename)
        safe_name = "".join(c for c in raw_name if c.isalnum() or c in "._-") or "portfolio.upload"
        if len(safe_name) > 200:
            safe_name = safe_name[-200:]

        # Extension allowlist — reject unexpected file types early.
        suffix = pathlib.Path(safe_name).suffix.lower()
        if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
            allowed = ", ".join(sorted(_ALLOWED_UPLOAD_SUFFIXES))
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote(f'File type {suffix!r} not allowed. Use: {allowed}')}",
                status_code=303,
            )

        pdir = pathlib.Path(os.environ.get("IC_PORTFOLIO_DIR", "/data/portfolios"))
        try:
            pdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Cannot create dir: ' + str(e))}",
                status_code=303,
            )

        # Path containment — ensure dest stays inside pdir after resolution.
        dest = (pdir / safe_name).resolve()
        if not str(dest).startswith(str(pdir.resolve()) + os.sep) and dest != pdir.resolve():
            return RedirectResponse(
                url="/dashboard/settings?message=Invalid+filename",
                status_code=303,
            )
        total_bytes = 0
        try:
            with dest.open("wb") as f:
                while True:
                    chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > _MAX_UPLOAD_BYTES:
                        try:
                            dest.unlink()
                        except OSError:
                            pass
                        return RedirectResponse(
                            url=f"/dashboard/settings?message={quote(f'File too large: {total_bytes//1024//1024} MB > {_MAX_UPLOAD_BYTES//1024//1024} MB cap')}",
                            status_code=303,
                        )
                    f.write(chunk)
            try:
                dest.chmod(0o644)
            except OSError:
                pass
        except Exception as e:
            try:
                dest.unlink()
            except OSError:
                pass
            return RedirectResponse(
                url=f"/dashboard/settings?message={quote('Write failed: ' + str(e))}",
                status_code=303,
            )

        # Fire setup in the background — don't block the redirect.
        if regenerate is not None:
            try:
                import asyncio as _asyncio
                _asyncio.create_task(_safe_call(regenerate))
            except Exception:
                pass

        return RedirectResponse(
            url=f"/dashboard/settings?message={quote(f'Saved {safe_name} ({total_bytes//1024} KB) — refresh queued')}",
            status_code=303,
        )

    @app.post("/dashboard/regenerate", include_in_schema=False)
    async def regenerate_now(request: Request) -> RedirectResponse:
        """Fire the regenerate callable in the background; redirect immediately."""
        from urllib.parse import quote
        if redirect := _csrf_redirect(request, "/"):
            return redirect
        if regenerate is None:
            return RedirectResponse(
                url=f"/?message={quote('Regenerate not wired (no callable provided)')}",
                status_code=303,
            )
        try:
            import asyncio as _asyncio
            _asyncio.create_task(_safe_call(regenerate))
            msg = "Regeneration started — refresh + analyzer sweep takes ~3-5 min for a 200-position portfolio."
        except Exception as e:
            msg = f"Failed to start regenerate: {e}"
        return RedirectResponse(
            url=f"/?message={quote(msg)}",
            status_code=303,
        )
