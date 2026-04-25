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
Command argument synthesis for InvestorClaw skill routing.
Auto-detects input files and synthesizes output paths based on command type.
"""

import os
from pathlib import Path
from typing import Optional

_ERR_NO_HOLDINGS = "No holdings.json found. Run '/ic-holdings' first."
_ERR_NO_BONDS = "No bond_analysis.json found. Run '/ic-bonds' first."


def _find_summary_file(reports_dir: Path) -> Optional[str]:
    """Return holdings_summary.json for dashboard; falls back to CDM holdings.json."""
    summary = reports_dir / "holdings_summary.json"
    if summary.exists():
        return str(summary)
    return _find_holdings_file(reports_dir)


def _find_holdings_file(reports_dir: Path) -> Optional[str]:
    """Return the path to holdings.json, checking .raw/ first (new CDM location)."""
    raw_path = reports_dir / ".raw" / "holdings.json"
    if raw_path.exists():
        return str(raw_path)
    legacy_path = reports_dir / "holdings.json"
    if legacy_path.exists():
        return str(legacy_path)
    return None


_HOLDINGS_CONSUMER_COMMANDS = frozenset(
    {
        "bonds",
        "bond-analysis",
        "analyze-bonds",
        "fixed-income",
        "fixed-income-analysis",
        "bond-strategy",
        "news",
        "sentiment",
        "news-plan",
        "fetch-plan",
        "analyst",
        "analysts",
        "ratings",
        "analysis",
        "portfolio-analysis",
        "synthesize",
        "synthesize-opportunities",
        "multi-factor",
        "analyze-multi",
        "recommend",
        "recommendations",
        "analyze",
        "performance",
        "returns",
        "report",
        "export",
        "csv",
        "excel",
        "scenario",
        "stress-test",
        "macro-scenario",
        "rebalance",
        "rebalance-tax",
        "tax-rebalance",
        "tax-lots",
        "optimize",
        "efficient-frontier",
        "peer",
        "peer-analysis",
        "factor-exposure",
        "whatchanged",
        "attribution",
        "why-changed",
        "cashflow",
        "dividends",
        "coupon-calendar",
        "run",
        "pipeline",
        "portfolio",
    }
)


def _strip_csv_first_arg(command: str, script_args: list) -> list:
    """Strip a leading CSV/XLS path when passed to a holdings-consumer command.

    Agents often re-pass the portfolio CSV to every /portfolio subcommand
    even though only `holdings` accepts it. Downstream scripts then hand
    the CSV to json.load() and die with `JSONDecodeError: Expecting value:
    line 1 column 1 (char 0)`. Silently drop that leading arg so the
    consumer falls through to synthesis on the auto-bootstrapped
    holdings.json.
    """
    if not script_args or command not in _HOLDINGS_CONSUMER_COMMANDS:
        return script_args
    head = str(script_args[0]).lower()
    if head.endswith((".csv", ".xls", ".xlsx", ".pdf", ".tsv")):
        return list(script_args[1:])
    return script_args


def synthesize_command_args(
    command: str,
    script_args: list,
    reports_dir: Path,
) -> tuple[list, int]:
    """
    Synthesize script arguments for a given command based on available data files.

    Args:
        command: Command name (from COMMANDS dict)
        script_args: User-provided script arguments
        reports_dir: Directory where reports are written

    Returns:
        Tuple of (synthesized_args: list, error_code: int)
        error_code is 0 on success, 1 if required files missing
    """
    script_args = _strip_csv_first_arg(command, script_args)

    # If user provided args, return them unchanged
    if script_args:
        return script_args, 0

    # For holdings command, auto-detect portfolio file if not specified
    if command == "holdings":
        # Note: find_portfolio_file() is called before this, so we just construct output
        # This is a fallback; the router typically provides args already
        output_file = str(reports_dir / "holdings.json")
        return [], 0  # Router handles this separately

    # For bonds command, use holdings.json
    if command == "bonds":
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "bond_analysis.json")
            return [holdings_file, output_file], 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For news/sentiment command, auto-detect holdings.json
    if command in ["news", "sentiment"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "portfolio_news.json")
            cache_file = str(reports_dir / "portfolio_news_cache.json")
            model_id = os.environ.get("OPENCLAW_MODEL", "").strip()
            model_args = ["--model", model_id] if model_id else []
            return [holdings_file, output_file, "--cache", cache_file] + model_args, 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For run/pipeline command, auto-detect holdings.json
    if command == "run":
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            return [holdings_file], 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For news-plan/fetch-plan, show the adaptive fetch plan
    if command in ["news-plan", "fetch-plan"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            model_id = os.environ.get("OPENCLAW_MODEL", "").strip()
            args = [holdings_file]
            if model_id:
                args += ["--model", model_id]
            return args, 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For analyst/ratings command, auto-detect holdings.json
    if command in ["analyst", "analysts", "ratings"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "analyst_data.json")
            args = [holdings_file, output_file]
            # --tier3 injection is handled by the router after arg synthesis;
            # do not duplicate it here. consultation_policy is the authority.
            return args, 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For portfolio analysis command
    if command in [
        "analysis",
        "portfolio-analysis",
        "synthesize",
        "synthesize-opportunities",
        "multi-factor",
        "analyze-multi",
        "recommend",
        "recommendations",
    ]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "portfolio_analysis.json")
            return [holdings_file, output_file], 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For performance analysis command
    if command in ["analyze", "performance", "returns"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "performance.json")
            return [holdings_file, "ytd", "today", output_file], 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For report/export command, auto-detect holdings.json
    if command in ["report", "export", "csv", "excel"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            performance_file = str(reports_dir / "performance.json")
            output_prefix = str(reports_dir / "portfolio_report")
            export_format = (
                "csv" if command == "csv" else ("excel" if command == "excel" else "both")
            )
            return [holdings_file, performance_file, export_format, output_prefix], 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For fixed-income command, use bond_analysis.json (preferred) or fallback to holdings.json
    if command in ["fixed-income", "fixed-income-analysis", "bond-strategy"]:
        bond_analysis_file = str(reports_dir / "bond_analysis.json")
        if Path(bond_analysis_file).exists():
            output_file = str(reports_dir / "fixed_income_analysis.json")
            return [bond_analysis_file, output_file], 0
        else:
            # Fallback: use holdings.json if bond_analysis.json doesn't exist
            holdings_file = _find_holdings_file(reports_dir)
            if holdings_file:
                output_file = str(reports_dir / "fixed_income_analysis.json")
                return [holdings_file, output_file], 0
            else:
                print(f"❌ {_ERR_NO_HOLDINGS}")
                return [], 1

    # For session command, pass reports_dir
    # If INVESTORCLAW_AUTO_SESSION=true (agentic/CI), pass a default profile
    # so session_init.py doesn't block waiting for interactive user input.
    if command in ["session", "session-init", "risk-profile", "calibrate"]:
        reports_dir_str = str(reports_dir)
        if os.environ.get("INVESTORCLAW_AUTO_SESSION", "").lower() == "true":
            # heat=3 (Balanced/moderate), stance=neutral, concerns="" (none)
            return [reports_dir_str, "--profile", "3", "neutral", ""], 0
        return [reports_dir_str], 0

    # For optimize command, auto-detect holdings.json
    if command in ["optimize", "rebalance", "allocation", "efficient-frontier"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "optimize.json")
            return [holdings_file, output_file], 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For peer-analysis command, auto-detect holdings.json and optional performance.json
    if command in ["peer", "peer-analysis", "factor-exposure", "style-drift"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "peer_analysis.json")
            perf_file = str(reports_dir / "performance.json")
            args = [holdings_file]
            if Path(perf_file).exists():
                args.append(perf_file)
            args.append(output_file)
            return args, 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For cashflow command, auto-detect holdings.json and optional bond_analysis.json
    if command in ["cashflow", "dividends", "coupon-calendar", "income"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "cashflow.json")
            bond_file = str(reports_dir / "bond_analysis.json")
            args = [holdings_file]
            if Path(bond_file).exists():
                args.append(bond_file)
            args.extend(["--months", "12", output_file])
            return args, 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For rebalance-tax command, auto-detect holdings.json
    if command in ["rebalance-tax", "tax-rebalance", "tax-lots"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "rebalance_tax.json")
            return [holdings_file, output_file], 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For whatchanged command, auto-detect holdings.json and performance.json
    if command in ["whatchanged", "attribution", "why-changed"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            perf_file = str(reports_dir / "performance.json")
            if not Path(perf_file).exists():
                print(
                    f"❌ {_ERR_NO_BONDS.replace('bond_analysis.json', 'performance.json')} Run '/ic-performance' first."
                )
                return [], 1
            output_file = str(reports_dir / "whatchanged.json")
            return [holdings_file, perf_file, output_file], 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For scenario command, auto-detect holdings.json and optional performance.json
    if command in ["scenario", "stress-test", "macro-scenario"]:
        holdings_file = _find_holdings_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "scenario.json")
            perf_file = str(reports_dir / "performance.json")
            args = [holdings_file]
            if Path(perf_file).exists():
                args.append(perf_file)
            args.append(output_file)
            return args, 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # For dashboard command, auto-wire holdings_summary.json + sibling artifacts
    if command in ["dashboard", "interactive-dashboard", "artifact"]:
        holdings_file = _find_summary_file(reports_dir)
        if holdings_file:
            output_file = str(reports_dir / "dashboard.html")
            perf_file = str(reports_dir / "performance.json")
            bonds_file = str(reports_dir / "bond_analysis.json")
            summary_file = str(reports_dir / "dashboard_summary.json")
            args = [
                holdings_file,
                output_file,
                "--format",
                "interactive",
                "--summary-out",
                summary_file,
            ]
            if Path(perf_file).exists():
                args.extend(["--performance", perf_file])
            if Path(bonds_file).exists():
                args.extend(["--bonds", bonds_file])
            return args, 0
        else:
            print(f"❌ {_ERR_NO_HOLDINGS}")
            return [], 1

    # Default: return empty args (user will provide them)
    return [], 0
