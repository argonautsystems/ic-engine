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
InvestorClaw Lookup Utility — agent-safe targeted reads from .raw/ data files.

This script is the ONLY sanctioned way for an agent to read specific data from
the full (non-compact) portfolio_reports/.raw/ files.  It extracts exactly the
requested slice and returns it as compact JSON to stdout — never the whole file.

Usage (via skill router):
  /portfolio lookup AAPL                          (positional ticker)
  /portfolio lookup --symbol AAPL                (flag variant)
  /portfolio lookup AAPL --file analyst          (positional + file)
  /portfolio lookup --top 10 --file performance  (top N for performance)
  /portfolio lookup --accounts                   (account summary)
  /portfolio lookup --file analyst --top 20      (multi-arg query)

Arguments:
  TICKER             Ticker or CUSIP to look up (positional, optional)
  --reports-dir      Path to portfolio_reports/ (injected by router)
  --symbol TICKER    Extract a single symbol from holdings or analyst data (flag variant)
  --file FILE        Which raw file to query: holdings (default) | analyst | performance | bonds
  --top N            Return top N records (performance: by return_pct desc)
  --accounts         List accounts summary from holdings (no symbol required)
  --fields f1,f2     Comma-separated list of fields to return per record
  --artifact PATH    Write HTML artifact to this path
  --stonkmode        Include stonkmode narrative in artifact

Exit codes: 0 on success, 1 on missing file or symbol not found.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_path(reports_dir: Path, filename: str) -> Path:
    """
    Resolve the path to a raw data file.

    Tries two locations:
    1. reports_dir/.raw/filename (dated subdirectory, typical case)
    2. reports_dir/../.raw/filename (parent directory, fallback for shared .raw/)

    This handles both single-day runs and multi-day queries against shared data.
    """
    # First try: reports_dir/.raw/filename
    primary = reports_dir / ".raw" / filename
    if primary.exists():
        return primary

    # Fallback: parent_dir/.raw/filename (for shared .raw/ directory)
    fallback = reports_dir.parent / ".raw" / filename
    return fallback


def _load_raw(reports_dir: Path, filename: str) -> dict | list | None:
    path = _raw_path(reports_dir, filename)
    if not path.exists():
        print(
            json.dumps(
                {"error": f"{filename} not found. Run the relevant /portfolio command first."}
            )
        )
        return None
    with open(path) as f:
        return json.load(f)


def _try_load_raw(reports_dir: Path, filename: str) -> dict | list | None:
    """Like _load_raw but returns None silently if the file is missing (no error output)."""
    path = _raw_path(reports_dir, filename)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _filter_fields(record: dict, fields: list[str] | None) -> dict:
    if not fields:
        return record
    return {k: v for k, v in record.items() if k in fields}


def _position_identifier(pos: dict) -> str:
    product = pos.get("product", {}) if isinstance(pos, dict) else {}
    asset = pos.get("asset", {}) if isinstance(pos, dict) else {}
    ident = product.get("productIdentifier") or product.get("product_identifier") or {}
    if not isinstance(ident, dict):
        ident = {}
    asset_ident = asset.get("productIdentifier") or asset.get("product_identifier") or {}
    if not isinstance(asset_ident, dict):
        asset_ident = {}
    return str(
        ident.get("identifier")
        or asset_ident.get("identifier")
        or pos.get("symbol")
        or pos.get("ticker")
        or ""
    )


def _holding_value(holding: dict) -> float:
    for key in ("value", "market_value", "marketValue"):
        value = holding.get(key)
        if value is not None:
            return value
    return 0.0


# ---------------------------------------------------------------------------
# Query handlers
# ---------------------------------------------------------------------------


def query_holdings_symbol(reports_dir: Path, symbol: str, fields: list[str] | None) -> int:
    """Extract a single position from .raw/holdings.json."""
    cdm = _load_raw(reports_dir, "holdings.json")
    if cdm is None:
        return 1

    positions = cdm.get("portfolio", {}).get("portfolioState", {}).get("positions", [])
    matches = []
    sym_upper = symbol.upper()

    for pos in positions:
        if _position_identifier(pos).upper() == sym_upper:
            matches.append(pos)

    if not matches:
        portfolio = cdm.get("portfolio", {}) if isinstance(cdm, dict) else {}
        for asset_class in ("equity", "bond", "cash", "margin", "crypto", "futures", "metals"):
            bucket = portfolio.get(asset_class, {})
            if not isinstance(bucket, dict):
                continue
            for sym, holding in bucket.items():
                if str(sym).upper() != sym_upper or not isinstance(holding, dict):
                    continue
                matches.append(
                    {
                        "symbol": sym,
                        "asset_class": asset_class,
                        "asset_type": asset_class,
                        **holding,
                        "value": _holding_value(holding),
                    }
                )

    if not matches:
        print(json.dumps({"error": f"Symbol '{symbol}' not found in holdings."}))
        return 1

    result = {"symbol": sym_upper, "positions": [_filter_fields(m, fields) for m in matches]}
    print(json.dumps(result, indent=2, default=str))
    return 0


def query_holdings_accounts(reports_dir: Path) -> int:
    """Return account-level summary from .raw/holdings.json."""
    cdm = _load_raw(reports_dir, "holdings.json")
    if cdm is None:
        return 1

    summary = cdm.get("portfolio", {}).get("summary", {})
    accounts = cdm.get("portfolio", {}).get("accounts", {})
    result = {
        "accounts_summary": accounts,
        "portfolio_summary": summary,
    }
    print(json.dumps(result, indent=2, default=str))
    return 0


def query_analyst_symbol(reports_dir: Path, symbol: str, fields: list[str] | None) -> int:
    """
    Extract a single symbol from analyst data.

    Fallback chain (richest → most available):
      1. .raw/analyst_data.json          — full payload written by fetch_analyst script
      2. .raw/analyst_recommendations_tier3_enriched.json — enriched subset (20 symbols)
      3. analyst_recommendations_summary.json  — compact summary in main reports dir
    """
    sym_upper = symbol.upper()

    # 1. Full analyst payload in .raw/ (silent — tier3 fallback may succeed)
    data = _try_load_raw(reports_dir, "analyst_data.json")
    if data:
        recs = data.get("recommendations", {})
        if sym_upper in recs:
            rec = _filter_fields(recs[sym_upper], fields)
            print(
                json.dumps(
                    {"symbol": sym_upper, "source": "analyst_data", **rec}, indent=2, default=str
                )
            )
            return 0

    # 2. Tier3 enriched (has synthesis + consultation block)
    t3 = _try_load_raw(reports_dir, "analyst_recommendations_tier3_enriched.json")
    if t3:
        enriched = t3.get("enriched_recommendations", {})
        if sym_upper in enriched:
            rec = _filter_fields(enriched[sym_upper], fields)
            print(
                json.dumps(
                    {"symbol": sym_upper, "source": "tier3_enriched", **rec}, indent=2, default=str
                )
            )
            return 0

    # 3. Compact summary in main dir (always present after /portfolio analyst)
    summary_path = reports_dir / "analyst_recommendations_summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)
        recs = summary.get("recommendations", {})
        if sym_upper in recs:
            rec = _filter_fields(recs[sym_upper], fields)
            print(
                json.dumps(
                    {"symbol": sym_upper, "source": "analyst_summary", **rec}, indent=2, default=str
                )
            )
            return 0

    print(json.dumps({"error": f"Symbol '{symbol}' not found. Run '/ic-analyst' first."}))
    return 1


def query_performance_top(reports_dir: Path, top_n: int, fields: list[str] | None) -> int:
    """Return top N positions by return_pct from .raw/performance.json."""
    data = _load_raw(reports_dir, "performance.json")
    if data is None:
        return 1

    positions = data.get("positions", data.get("holdings", []))
    if isinstance(positions, dict):
        positions = list(positions.values())

    # Sort by return_pct descending
    try:
        positions.sort(key=lambda p: float(p.get("return_pct", 0)), reverse=True)
    except (TypeError, ValueError):
        pass

    top = [_filter_fields(p, fields) for p in positions[:top_n]]
    print(json.dumps({"top_n": top_n, "by": "return_pct", "positions": top}, indent=2, default=str))
    return 0


def query_bonds_symbol(reports_dir: Path, symbol: str, fields: list[str] | None) -> int:
    """Extract a single CUSIP/symbol from .raw/bond_analysis.json."""
    data = _load_raw(reports_dir, "bond_analysis.json")
    if data is None:
        return 1

    bonds = data.get("bonds", data.get("positions", []))
    if isinstance(bonds, dict):
        bonds = list(bonds.values())

    sym_upper = symbol.upper()
    matches = [
        b
        for b in bonds
        if b.get("cusip", "").upper() == sym_upper
        or b.get("symbol", "").upper() == sym_upper
        or b.get("ticker", "").upper() == sym_upper
    ]

    if not matches:
        print(json.dumps({"error": f"Bond '{symbol}' not found in bond_analysis."}))
        return 1

    result = {"symbol": sym_upper, "bonds": [_filter_fields(m, fields) for m in matches]}
    print(json.dumps(result, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    import contextlib
    import io
    import os

    parser = argparse.ArgumentParser(
        description="Targeted lookup from portfolio_reports/.raw/ data files."
    )
    # Allow symbol as first positional arg for common case: /portfolio lookup NVDA
    parser.add_argument(
        "symbol", nargs="?", default=None, help="Ticker or CUSIP to look up (positional)"
    )
    # Optional reports_dir (fallback to env var or default)
    parser.add_argument(
        "--reports-dir",
        "--dir",
        dest="reports_dir",
        default=None,
        help="Path to portfolio_reports/ directory (default: ~/portfolio_reports)",
    )
    parser.add_argument(
        "--symbol", dest="symbol_flag", default=None, help="Ticker or CUSIP (flag variant)"
    )
    parser.add_argument(
        "--file",
        default="holdings",
        choices=["holdings", "analyst", "performance", "bonds"],
        help="Which raw file to query (default: holdings)",
    )
    parser.add_argument(
        "--top", type=int, default=None, help="Return top N records (performance only)"
    )
    parser.add_argument(
        "--accounts", action="store_true", help="Return account summary from holdings"
    )
    parser.add_argument(
        "--fields", default=None, help="Comma-separated field names to include in output"
    )
    # Artifact / stonkmode pass-through (from router)
    parser.add_argument("--artifact", default=None, help="Write HTML artifact to this path")
    parser.add_argument(
        "--stonkmode", action="store_true", help="Include stonkmode narrative in artifact"
    )
    args = parser.parse_args()

    # Resolve symbol from positional or --symbol flag
    symbol = args.symbol or args.symbol_flag

    # Guard: reject natural-language question strings. Agents occasionally
    # pass the user's full NL question ("What is yield to maturity?") as
    # the lookup symbol, which produces an unhelpful "symbol not found"
    # error and consumes an ic_result exit=1. Detect obvious NL shapes and
    # redirect to the P7 (concept) / P8 (market-wide) decline template so
    # the agent sees a clear "this is not a lookup, route elsewhere" signal.
    def _looks_like_nl_question(s: str) -> bool:
        if not s:
            return False
        s_stripped = s.strip()
        if len(s_stripped) > 40:
            return True
        if "?" in s_stripped:
            return True
        if " " in s_stripped:
            return True
        lowered = s_stripped.lower()
        nl_prefixes = (
            "what",
            "how",
            "why",
            "when",
            "where",
            "who",
            "is ",
            "are ",
            "do ",
            "does ",
            "can ",
            "should ",
            "explain",
            "define",
            "describe",
            "tell ",
        )
        if lowered.startswith(nl_prefixes):
            return True
        return False

    if symbol and _looks_like_nl_question(symbol):
        msg = (
            "InvestorClaw is scoped to your actual holdings and does not "
            "run a glossary, concept-explanation, or market-wide lookup "
            "layer. `investorclaw lookup` takes a single ticker or CUSIP, "
            "not a natural-language question. For concept questions use a "
            "general-purpose knowledge source; for your own holdings try "
            "/portfolio holdings, /portfolio performance, /portfolio "
            "bonds, etc."
        )
        print(
            json.dumps(
                {
                    "disclaimer": "EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
                    "is_investment_advice": False,
                    "error": "natural_language_question_not_a_symbol",
                    "received": symbol,
                    "guidance": msg,
                }
            )
        )
        # Emit ic_result with exit 0 since the decline is the *correct*
        # outcome — not a script failure. The agent's contract treats
        # exit=0 + non-null error field as "skill declined deliberately."
        print(json.dumps({"ic_result": {"script": "lookup.py", "exit_code": 0, "duration_ms": 0}}))
        sys.exit(0)

    # Resolve reports_dir from arg, env var, or default
    if args.reports_dir:
        reports_dir = Path(args.reports_dir)
    else:
        reports_dir = Path(os.environ.get("INVESTORCLAW_REPORTS_DIR", "~/portfolio_reports"))

    reports_dir = reports_dir.expanduser().resolve()
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else None

    def _dispatch() -> int:
        if args.file == "holdings":
            if args.accounts:
                return query_holdings_accounts(reports_dir)
            if symbol:
                return query_holdings_symbol(reports_dir, symbol, fields)
            print(json.dumps({"error": "Specify SYMBOL or --accounts for holdings lookup."}))
            return 1
        if args.file == "analyst":
            if symbol:
                return query_analyst_symbol(reports_dir, symbol, fields)
            print(json.dumps({"error": "Specify SYMBOL for analyst lookup."}))
            return 1
        if args.file == "performance":
            n = args.top or 10
            return query_performance_top(reports_dir, n, fields)
        if args.file == "bonds":
            if symbol:
                return query_bonds_symbol(reports_dir, symbol, fields)
            print(json.dumps({"error": "Specify SYMBOL (CUSIP or ticker) for bond lookup."}))
            return 1
        print(json.dumps({"error": f"Unknown --file: {args.file}"}))
        return 1

    # When an artifact is requested, capture stdout so we can parse the result
    # JSON and feed it to the artifact builder, then re-emit it for the agent.
    if args.artifact:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = _dispatch()
        captured = buf.getvalue()
        sys.stdout.write(captured)
        sys.stdout.flush()

        if exit_code == 0:
            try:
                result = json.loads(captured)
                _project_root = str(Path(__file__).resolve().parent.parent)
                if _project_root not in sys.path:
                    sys.path.insert(0, _project_root)
                from ic_engine.commands._artifact_helpers import (
                    build_lookup_artifact,
                    pop_artifact_flags,
                )

                # Respect state-file stonkmode toggle too
                _, state_stonk = pop_artifact_flags(["--artifact", args.artifact])
                stonk = bool(args.stonkmode or state_stonk)
                out = build_lookup_artifact(result, args.artifact, stonkmode=stonk)
                print(f"Artifact: {out}")
            except Exception as e:
                print(f"Artifact generation failed: {e}", file=sys.stderr)
        return exit_code

    return _dispatch()


if __name__ == "__main__":
    sys.exit(main())
