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
InvestorClaw command router.

Contains the COMMANDS registry, argument synthesis, and tier-3 injection.
This is the single place that maps user commands to scripts and builds
the argument list that gets passed to each script subprocess.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command → script mapping
# All paths are relative to SCRIPTS_DIR (investorclaw/commands/).
# "../pipeline.py" resolves to the InvestorClaw parent directory.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# v2.2 SECTION_DISPATCH layer (RFC §3.3, r2.3)
#
# Consolidated wrappers map (wrapper, section) -> underlying script.
# Wrappers themselves appear in COMMANDS as "__dispatch__" sentinels.
# Legacy CLI aliases ("holdings", "performance", etc.) below remain
# permanent per RFC §8 resolved entry 3.
# ---------------------------------------------------------------------------

SECTION_DISPATCH: dict = {
    "view": {
        "holdings": "fetch_holdings.py",
        "performance": "analyze_performance_polars.py",
        "analyst": "fetch_analyst_recommendations_parallel.py",
        "news": "fetch_portfolio_news.py",
        "dashboard": "dashboard_deferred.py",
    },
    "compute": {
        "synthesize": "portfolio_analyzer.py",
        "optimize-sharpe": "optimize.py",
        "optimize-minvol": "optimize.py",
        "optimize-blacklitterman": "optimize.py",
    },
    "target": {
        "allocation": "session_init.py",
        "drift": "session_init.py",
    },
    "scenario": {
        "rebalance": "scenario.py",
        "stress": "scenario.py",
        "tax-aware": "rebalance_tax.py",
    },
    "market": {
        "news": "fetch_market_news.py",  # NEW in step 3b
        "concept": "concept_decline.py",
        "market": "concept_decline.py",
    },
    "bonds": {
        "analysis": "bond_analyzer.py",
        "strategy": "fixed_income_analysis.py",
    },
}

DEFAULT_SECTIONS: dict = {
    "view": "holdings",
    "compute": "synthesize",
    "target": "allocation",
    "scenario": "rebalance",
    "market": "news",
    "bonds": "analysis",
}

# Sentinel for COMMANDS entries that are dispatch wrappers.
_DISPATCH_SENTINEL = "__dispatch__"

COMMANDS: dict = {
    # v2.2 consolidated wrapper sentinels — resolve via SECTION_DISPATCH.
    # `scenario`, `market`, `bonds` already have legacy CLI entries below;
    # those route as direct scripts when no --section is provided, and as
    # dispatch wrappers when --section is provided. resolve_script handles both.
    "view": _DISPATCH_SENTINEL,
    "compute": _DISPATCH_SENTINEL,
    "target": _DISPATCH_SENTINEL,
    "setup": "auto_setup.py",
    "auto-setup": "auto_setup.py",
    "init": "auto_setup.py",
    "initialize": "auto_setup.py",
    "bonds": "bond_analyzer.py",
    "bond-analysis": "bond_analyzer.py",
    "analyze-bonds": "bond_analyzer.py",
    "bond-exposure": "bond_analyzer.py",
    "bond-allocation": "bond_analyzer.py",
    "holdings": "fetch_holdings.py",
    "snapshot": "fetch_holdings.py",
    "prices": "fetch_holdings.py",
    "performance": "analyze_performance_polars.py",
    "analyze": "analyze_performance_polars.py",
    "returns": "analyze_performance_polars.py",
    "synthesize": "portfolio_analyzer.py",
    "synthesize-opportunities": "portfolio_analyzer.py",
    "analyze-multi": "portfolio_analyzer.py",
    "multi-factor": "portfolio_analyzer.py",
    "recommend": "portfolio_analyzer.py",
    "recommendations": "portfolio_analyzer.py",
    "report": "export_report.py",
    "export": "export_report.py",
    "csv": "export_report.py",
    "excel": "export_report.py",
    "news": "fetch_portfolio_news.py",
    "sentiment": "fetch_portfolio_news.py",
    "news-plan": "news_fetch_planner.py",
    "fetch-plan": "news_fetch_planner.py",
    "analyst": "fetch_analyst_recommendations_parallel.py",
    "analysts": "fetch_analyst_recommendations_parallel.py",
    "ratings": "fetch_analyst_recommendations_parallel.py",
    "analysis": "portfolio_analyzer.py",
    "portfolio-analysis": "portfolio_analyzer.py",
    "complete": "portfolio_complete.py",
    "fixed-income": "fixed_income_analysis.py",
    "fixed-income-analysis": "fixed_income_analysis.py",
    "bond-strategy": "fixed_income_analysis.py",
    "optimize": "optimize.py",
    "rebalance": "optimize.py",
    "allocation": "optimize.py",
    "efficient-frontier": "optimize.py",
    "rebalance-tax": "rebalance_tax.py",
    "tax-rebalance": "rebalance_tax.py",
    "tax-lots": "rebalance_tax.py",
    "session": "session_init.py",
    "session-init": "session_init.py",
    "risk-profile": "session_init.py",
    "calibrate": "session_init.py",
    "guardrails": "model_guardrails.py",
    "guardrail": "model_guardrails.py",
    "guardrails-prime": "model_guardrails.py",
    "guardrails-status": "model_guardrails.py",
    "lookup": "lookup.py",
    "query": "lookup.py",
    "detail": "lookup.py",
    "llm-config": "llm_config.py",
    "llm_config": "llm_config.py",
    "ollama-setup": "ollama_model_config.py",
    "model-setup": "ollama_model_config.py",
    "consult-setup": "ollama_model_config.py",
    "eod-report": "eod_report.py",
    "eod": "eod_report.py",
    "daily-report": "eod_report.py",
    "end-of-day": "eod_report.py",
    "fa-topics": "fa_discussion.py",
    "fa-discussion": "fa_discussion.py",
    "discussion-topics": "fa_discussion.py",
    "run": "../pipeline.py",
    "pipeline": "../pipeline.py",
    "stonkmode": "stonkmode_control.py",
    "stonk-mode": "stonkmode_control.py",
    "stonks": "stonkmode_control.py",
    "check-updates": "check_updates.py",
    "check_updates": "check_updates.py",
    "update-check": "check_updates.py",
    "update": "check_updates.py",
    "peer": "peer_analysis.py",
    "peer-analysis": "peer_analysis.py",
    "factor-exposure": "peer_analysis.py",
    "style-drift": "peer_analysis.py",
    "whatchanged": "whatchanged.py",
    "attribution": "whatchanged.py",
    "why-changed": "whatchanged.py",
    "scenario": "scenario.py",
    "stress-test": "scenario.py",
    "macro-scenario": "scenario.py",
    "cashflow": "cashflow.py",
    "dividends": "cashflow.py",
    "coupon-calendar": "cashflow.py",
    "income": "cashflow.py",
    "portfolio": "portfolio_switcher.py",
    "portfolio-list": "portfolio_switcher.py",
    "portfolio-create": "portfolio_switcher.py",
    "portfolio-switch": "portfolio_switcher.py",
    "portfolio-meta": "portfolio_switcher.py",
    "portfolios": "portfolio_switcher.py",
    # Dashboard is deferred per v2.1.0; stub emits the canonical deferral
    # message with ic_result exit 0 so agents get a clean quotable reply
    # rather than "Unknown command: dashboard".
    "dashboard": "dashboard_deferred.py",
    # Concept / market-wide question deflection (Patterns 7 + 8). Gives the
    # agent an ic_result-verified target to route to instead of answering
    # from training data.
    "concept": "concept_decline.py",
    "define": "concept_decline.py",
    "explain": "concept_decline.py",
    "glossary": "concept_decline.py",
    "market": "concept_decline.py",
    "macro": "concept_decline.py",
    "market-wide": "concept_decline.py",
    # v2.2 market-news entry (portfolio_market --section=news --topic=X)
    "market-news": "fetch_market_news.py",
    "ask": "ask.py",
    "refresh": "ask.py",
}

# Commands that should NOT trigger guardrail auto-priming (saves ~80 tokens/call)
NON_ANALYSIS_COMMANDS: frozenset = frozenset(
    {
        "guardrails",
        "guardrail",
        "guardrails-prime",
        "guardrails-status",
        "setup",
        "auto-setup",
        "init",
        "initialize",
        "session",
        "session-init",
        "risk-profile",
        "calibrate",
        "report",
        "export",
        "csv",
        "excel",
        "lookup",
        "query",
        "detail",
        "llm-config",
        "llm_config",
        "ollama-setup",
        "model-setup",
        "consult-setup",
        "eod-report",
        "eod",
        "daily-report",
        "end-of-day",
        "fa-topics",
        "fa-discussion",
        "discussion-topics",
        "check-updates",
        "check_updates",
        "update-check",
        "update",
        "help",
        "update-identity",
        "update_identity",
        "identity",
        "portfolio",
        "portfolio-list",
        "portfolio-create",
        "portfolio-switch",
        "portfolio-meta",
        "portfolios",
        # Deferral / deflection stubs — no analysis occurs.
        "dashboard",
        "concept",
        "define",
        "explain",
        "glossary",
        "market",
        "macro",
        "market-wide",
        "ask",
        "refresh",
    }
)

# Commands where synthesize_command_args should be called if no user args given
_AUTO_SYNTHESIZE: frozenset = frozenset(
    {
        "bonds",
        "news",
        "sentiment",
        "run",
        "pipeline",
        "news-plan",
        "fetch-plan",
        "analyst",
        "analysts",
        "ratings",
        "analysis",
        "portfolio-analysis",
        "analyze",
        "performance",
        "returns",
        "report",
        "export",
        "csv",
        "excel",
        "fixed-income",
        "fixed-income-analysis",
        "bond-strategy",
        "optimize",
        "rebalance",
        "allocation",
        "efficient-frontier",
        "rebalance-tax",
        "tax-rebalance",
        "tax-lots",
        "session",
        "session-init",
        "risk-profile",
        "calibrate",
        "synthesize",
        "synthesize-opportunities",
        "analyze-multi",
        "multi-factor",
        "recommend",
        "recommendations",
        "lookup",
        "query",
        "detail",
        "peer",
        "peer-analysis",
        "factor-exposure",
        "style-drift",
        "cashflow",
        "dividends",
        "coupon-calendar",
        "income",
        "whatchanged",
        "attribution",
        "why-changed",
        "scenario",
        "stress-test",
        "macro-scenario",
    }
)

# Commands with special CLI signatures (no auto-synthesis of positional args)
_NO_AUTO_SYNTHESIZE: frozenset = frozenset({})

# Commands whose scripts explicitly understand --verbose. Harnesses can opt
# into richer diagnostics without changing compact production defaults.
_VERBOSE_AWARE_COMMANDS: frozenset = frozenset(
    {
        "analyst",
        "analysts",
        "ratings",
        "news",
        "sentiment",
        "analysis",
        "portfolio-analysis",
        "synthesize",
        "synthesize-opportunities",
        "analyze-multi",
        "multi-factor",
        "recommend",
        "recommendations",
        "holdings",
        "snapshot",
        "prices",
        "performance",
        "analyze",
        "returns",
        "bonds",
        "bond-analysis",
        "analyze-bonds",
        "optimize",
        "rebalance",
        "allocation",
        "efficient-frontier",
        "rebalance-tax",
        "tax-rebalance",
        "tax-lots",
        "scenario",
        "stress-test",
        "macro-scenario",
        "cashflow",
        "dividends",
        "coupon-calendar",
        "income",
        "whatchanged",
        "attribution",
        "why-changed",
        "peer",
        "peer-analysis",
        "peer_analysis",
        "factor-exposure",
        "style-drift",
    }
)


def resolve_script(
    command: str,
    scripts_dir: Path,
    section: Optional[str] = None,
) -> Optional[Path]:
    """
    Return the absolute script Path for *command*, or None on failure.

    Prints actionable error messages to stderr so the caller can simply
    return 1 without additional diagnostics.

    When *command* is a v2.2 consolidated wrapper (in SECTION_DISPATCH):
      - section=None  → uses DEFAULT_SECTIONS[command]
      - section=valid → resolves to SECTION_DISPATCH[command][section]
      - section=invalid → emits ic_result error envelope to stdout,
                          returns None
    When *command* is a legacy CLI alias and section is not None, the section
    arg is ignored (legacy aliases are direct script mappings).
    """
    if command not in COMMANDS:
        print(f"❌ Unknown command: {command}", file=sys.stderr)
        print(f"Available commands: {', '.join(sorted(COMMANDS.keys()))}", file=sys.stderr)
        print("Run 'python3 investorclaw.py help' for more information.", file=sys.stderr)
        return None

    # v2.2 dispatch resolution: command is a wrapper (sentinel or legacy alias
    # that also appears in SECTION_DISPATCH) AND a section is provided OR the
    # command is a sentinel-only entry.
    is_sentinel = COMMANDS[command] == _DISPATCH_SENTINEL
    if is_sentinel or (section is not None and command in SECTION_DISPATCH):
        wrapper_dispatch = SECTION_DISPATCH.get(command)
        if wrapper_dispatch is None:
            # Sentinel without a dispatch table — should not happen for built-in
            # commands, but guard anyway.
            print(f"❌ Wrapper '{command}' has no dispatch table", file=sys.stderr)
            return None

        if section is None:
            section = DEFAULT_SECTIONS.get(command)
            if section is None:
                print(f"❌ Wrapper '{command}' has no default section", file=sys.stderr)
                return None

        if section not in wrapper_dispatch:
            import json as _json

            envelope = {
                "ic_result": {"script": command, "exit_code": 1, "duration_ms": 0},
                "error": "Invalid section",
                "command": command,
                "section_provided": section,
                "allowed_sections": sorted(wrapper_dispatch.keys()),
            }
            print(_json.dumps(envelope))
            print(
                f"❌ Invalid section '{section}' for '{command}'. "
                f"Allowed: {sorted(wrapper_dispatch.keys())}",
                file=sys.stderr,
            )
            return None

        script_name = wrapper_dispatch[section]
    else:
        script_name = COMMANDS[command]

    script_path = scripts_dir / script_name
    if not script_path.exists():
        print(f"❌ Script not found: {script_path}", file=sys.stderr)
        return None

    return script_path


#: Commands that legitimately need pre-materialized holdings data. Anything
#: not in this set is a no-op for the auto-bootstrap — otherwise running
#: `llm-config` / `setup` / `help` / `update-identity` / etc. on a fresh
#: checkout would silently spin up fetch_holdings.py and hit the network.
_HOLDINGS_CONSUMERS = frozenset(
    {
        # Performance / analysis
        "performance",
        "analyze",
        "returns",
        "analysis",
        "portfolio-analysis",
        "synthesize",
        "multi-factor",
        "recommend",
        "recommendations",
        "whatchanged",
        "attribution",
        "peer",
        "peer-analysis",
        "factor-exposure",
        "scenario",
        "stress-test",
        # Bonds / fixed income
        "bonds",
        "bond-analysis",
        "analyze-bonds",
        "fixed-income",
        "fixed-income-analysis",
        "bond-strategy",
        # Market data / external fetchers that join on holdings
        "analyst",
        "analysts",
        "ratings",
        "news",
        "sentiment",
        # Optimization + tax
        "optimize",
        "rebalance",
        "rebalance-tax",
        "tax-lots",
        "tax-rebalance",
        # Reporting
        "report",
        "export",
        "csv",
        "excel",
        "eod-report",
        "dashboard",
        "cashflow",
        "dividends",
        "coupon-calendar",
        # Lookups (consume holdings summary)
        "lookup",
        "query",
        "detail",
        # Aggregates
        "portfolio-overview",
        "optimization-plan",
        "intelligence",
        "complete",
        "run",
        "pipeline",
        # Top-level entry points that drive the envelope-cache pipeline,
        # which transitively reads holdings via HoldingsLoader. These need
        # the .raw/holdings.json bootstrap on first run against a CSV/XLS.
        "ask",
        "refresh",
    }
)


def auto_bootstrap_holdings(
    command: str,
    skill_dir: Path,
    reports_dir: Path,
    portfolio_path: Optional[Path] = None,
) -> Optional[Path]:
    """Auto-materialize holdings.json from a portfolio CSV when a downstream
    command needs it but it is absent.

    Returns the materialized holdings.json path on success, None when no-op
    or failure (logged at WARNING so it's visible in operator output).

    Silent no-op (returns None) when:
      - command is not in _HOLDINGS_CONSUMERS (e.g. llm-config, setup, help)
      - holdings.json already exists (returns existing path so callers can rebind)
      - no portfolio CSV is available
      - the command is 'holdings' itself (let the explicit invocation handle it)
      - fetch_holdings.py is missing

    `portfolio_path` argument: when set, use it directly instead of running
    `find_portfolio_file(skill_dir)`. Required for `ask --portfolio /path.csv`
    where the user picked a CSV outside the skill's portfolios/ directory.
    """
    from ic_engine.config.path_resolver import find_portfolio_file

    if command in {"holdings", "snapshot", "prices"}:
        return None

    if command not in _HOLDINGS_CONSUMERS:
        return None

    raw_holdings = reports_dir / ".raw" / "holdings.json"
    legacy_holdings = reports_dir / "holdings.json"
    if raw_holdings.exists():
        return raw_holdings
    if legacy_holdings.exists():
        return legacy_holdings

    portfolio_file: Optional[str]
    if portfolio_path is not None:
        # Caller passed an explicit --portfolio CSV/XLS — use it directly.
        # If it's already a JSON envelope, the caller shouldn't have invoked us;
        # but tolerate the shape and let HoldingsLoader handle it downstream.
        portfolio_file = str(portfolio_path)
    else:
        portfolio_file = find_portfolio_file(skill_dir)
    if not portfolio_file:
        return None

    # fetch_holdings.py lives in the engine package, not in the adapter's
    # user-data root. Resolve via SCRIPTS_DIR (cli.py's single source of
    # truth) so adapter installs find the engine-bundled script even when
    # skill_dir is overridden to the adapter checkout.
    from ic_engine.cli import SCRIPTS_DIR

    fetch_script = SCRIPTS_DIR / "fetch_holdings.py"
    if not fetch_script.exists():
        logger.warning(
            "Holdings auto-bootstrap skipped: fetch_holdings.py missing at %s",
            fetch_script,
        )
        return None

    raw_holdings.parent.mkdir(parents=True, exist_ok=True)
    try:
        from ic_engine.runtime.environment import build_env

        # Prefer the project's venv Python (has deps like `ratelimit`); fall back to current interpreter.
        venv_python = sys.executable
        for candidate in (
            skill_dir / ".venv" / "bin" / "python3",
            skill_dir / ".venv" / "bin" / "python",
            skill_dir / "venv" / "bin" / "python3",
            skill_dir / "venv" / "bin" / "python",
            skill_dir / ".venv" / "Scripts" / "python.exe",
            skill_dir / "venv" / "Scripts" / "python.exe",
        ):
            if candidate.exists():
                venv_python = str(candidate)
                break

        proc = subprocess.run(
            [venv_python, str(fetch_script), portfolio_file, str(raw_holdings)],
            capture_output=True,
            check=False,
            cwd=str(skill_dir),
            timeout=60,
            env=build_env(skill_dir),
        )
        if proc.returncode != 0:
            logger.warning(
                "Holdings auto-bootstrap failed (exit=%d): %s",
                proc.returncode,
                (proc.stderr or b"").decode("utf-8", errors="replace")[:500],
            )
            return None
        if not raw_holdings.exists():
            logger.warning(
                "Holdings auto-bootstrap returned 0 but %s was not written",
                raw_holdings,
            )
            return None
        return raw_holdings
    except Exception as exc:
        logger.warning("Holdings auto-bootstrap raised: %s", exc)
        return None


# Back-compat alias for any callers still using the underscore-private name.
_auto_bootstrap_holdings = auto_bootstrap_holdings


def synthesize_args(
    command: str,
    user_args: List[str],
    skill_dir: Path,
    section: Optional[str] = None,
) -> Tuple[List[str], int]:
    """
    Build the complete argument list for *command*.

    Returns (args, exit_code).  exit_code != 0 indicates a hard error
    (e.g. required input file missing); the caller should propagate it
    directly as the process exit code.

    Injection order:
      1. User-provided args (pass-through when present)
      2. Auto-synthesized args from command_builders (when user gave none)
      3. --tier3 / --tier3-limit appended by consultation_policy (authority)

    *section* is the v2.2 dispatch section (RFC §3.3). When provided, it
    influences CSV-strip + auto-bootstrap behavior so wrapper-resolved
    scripts get the same treatment their legacy CLI counterparts received.
    """
    from ic_engine.config.path_resolver import find_portfolio_file, get_reports_dir
    from ic_engine.services.consultation_policy import (
        get_consultation_limit,
        get_dynamic_consultation_limit,
        should_inject_tier3,
    )

    # When command is a v2.2 wrapper, derive the legacy command identity from
    # (command, section) so the existing CSV-strip / auto-bootstrap / verbose
    # logic continues to work unchanged. The wrapper layer is purely
    # presentation; the deterministic system below operates on the legacy
    # command identity.
    effective_command = command
    if command in SECTION_DISPATCH:
        sec = section if section is not None else DEFAULT_SECTIONS.get(command)
        # Map (wrapper, section) → legacy command name used by downstream gates
        # below (e.g. _HOLDINGS_CONSUMERS, _AUTO_SYNTHESIZE, _VERBOSE_AWARE_COMMANDS).
        wrapper_legacy_map = {
            ("view", "holdings"): "holdings",
            ("view", "performance"): "performance",
            ("view", "analyst"): "analyst",
            ("view", "news"): "news",
            ("view", "dashboard"): "dashboard",
            ("compute", "synthesize"): "synthesize",
            ("compute", "optimize-sharpe"): "optimize",
            ("compute", "optimize-minvol"): "optimize",
            ("compute", "optimize-blacklitterman"): "optimize",
            ("target", "allocation"): "session",
            ("target", "drift"): "session",
            ("scenario", "rebalance"): "scenario",
            ("scenario", "stress"): "stress-test",
            ("scenario", "tax-aware"): "rebalance-tax",
            # v2.2 fetch_market_news.py uses its own argparse (--topic /
            # --max-articles / --verbose) and rejects positional args, so it
            # MUST NOT collapse to legacy 'market' identity (which would later
            # inject ["market"] as a positional via the market-wide synthesis
            # block and crash argparse with "unrecognized arguments: market").
            # Keep it as a distinct effective identity that opts out of
            # market-wide arg injection below.
            ("market", "news"): "market-news",
            ("market", "concept"): "concept",
            ("market", "market"): "market",
            ("bonds", "analysis"): "bonds",
            ("bonds", "strategy"): "fixed-income",
        }
        effective_command = wrapper_legacy_map.get((command, sec), command)

    reports_dir = get_reports_dir()
    _auto_bootstrap_holdings(effective_command, skill_dir, reports_dir)
    args = list(user_args)

    # Agents frequently re-pass the portfolio CSV to every subcommand, but
    # only `holdings` takes the CSV as a positional arg. Downstream scripts
    # (performance/analyst/news/analysis) would then json.load() the CSV
    # and die with JSONDecodeError. Silently drop the CSV so synthesis
    # below can fill in the correct holdings.json path. See v2.1.2 notes.
    if effective_command in _HOLDINGS_CONSUMERS and effective_command != "holdings" and args:
        head = str(args[0]).lower()
        if head.endswith((".csv", ".xls", ".xlsx", ".pdf", ".tsv")):
            args = args[1:]

    # Holdings: special-case source-file detection
    if effective_command == "holdings":
        # Split any caller-supplied flags away from positional paths so
        # `--verbose`, `--artifact PATH`, etc. don't end up as positional
        # input/output arguments when the caller (agent) passed a portfolio
        # path explicitly.
        user_flags: List[str] = []
        user_positionals: List[str] = []
        _skip_next = False
        for i, a in enumerate(args):
            if _skip_next:
                user_flags.append(a)
                _skip_next = False
                continue
            if a.startswith("--"):
                user_flags.append(a)
                if a == "--artifact":
                    _skip_next = True
                continue
            user_positionals.append(a)

        portfolio_file = user_positionals[0] if user_positionals else find_portfolio_file(skill_dir)
        if portfolio_file:
            # Full CDM goes to .raw/holdings.json; compact summary saved to holdings_summary.json
            raw_dir = reports_dir / ".raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            output_file = (
                user_positionals[1] if len(user_positionals) > 1 else str(raw_dir / "holdings.json")
            )
            extra_positionals = user_positionals[2:]
            args = [portfolio_file, output_file] + extra_positionals + user_flags
        elif not args:
            import json as _json

            _notice = {
                "status": "no_portfolio",
                "_note": "No portfolio files found. Add a CSV, XLS, or PDF file to the portfolios/ directory, then run /ic-setup.",
                "instruction": "Run '/ic-setup' first to discover and register your holdings file.",
                "ic_result": {"script": "fetch_holdings.py", "exit_code": 0, "duration_ms": 0},
            }
            print(_json.dumps(_notice))
            return [], 0

    # Lookup/query: pass reports_dir as flag so symbol isn't consumed as arg.
    # When invoked with no user args, default to --accounts (portfolio summary),
    # since an argumentless `lookup` otherwise errors with "Specify SYMBOL".
    if effective_command in ("lookup", "query", "detail"):
        base = ["--reports-dir", str(reports_dir)]
        if not user_args:
            return base + ["--accounts"], 0
        return base + list(user_args), 0

    # Market-wide aliases route to concept_decline.py but the script
    # switches between concept-mode and market-mode based on argv[0].
    # Inject the mode selector so `investorclaw market` returns the
    # market envelope (reason=market_wide_question_out_of_scope) rather
    # than defaulting to concept mode.
    if effective_command in ("market", "macro", "market-wide") and not user_args:
        return ["market"], 0

    # General argument synthesis for all other auto-synthesize commands
    if effective_command in _AUTO_SYNTHESIZE and not args:
        from ic_engine.config.command_builders import synthesize_command_args

        args, error_code = synthesize_command_args(effective_command, args, reports_dir)
        if error_code != 0:
            return [], error_code

    # Tier-3 consultation injection — consultation_policy is the single authority
    if should_inject_tier3(effective_command) and "--tier3" not in args:
        args.append("--tier3")
        # Use dynamic limit scaled to portfolio size when holdings_summary is available
        limit = get_consultation_limit(effective_command)
        try:
            holdings_summary = reports_dir / "holdings_summary.json"
            if holdings_summary.exists():
                import json as _json

                with open(holdings_summary) as _f:
                    _hs = _json.load(_f)
                _pc = _hs.get("data", _hs).get("summary", {}).get("position_count", {})
                _equity = _pc.get("equity", 0) if isinstance(_pc, dict) else 0
                if _equity > 0:
                    limit = get_dynamic_consultation_limit(_equity)
        except Exception:
            pass
        if limit:
            args.extend(["--tier3-limit", str(limit)])

    # Add --verbose by default for learning, unless disabled by user/CI
    if (
        effective_command in _VERBOSE_AWARE_COMMANDS
        and "--verbose" not in args
        and os.environ.get("INVESTORCLAW_VERBOSE_DISABLED", "false").lower() != "true"
    ):
        args.append("--verbose")

    return args, 0


def should_prime_guardrails(command: str) -> bool:
    """Return True if the command should trigger auto-priming of guardrails."""
    return command not in NON_ANALYSIS_COMMANDS


def emit_critical_content_floor(command: str) -> None:
    """
    CRITICAL CONTENT: Always surface advisor recommendation + disclaimer,
    even if INVESTORCLAW_VERBOSE_DISABLED is set.

    Called after command execution to ensure the educational-only disclaimer
    is always visible on analysis commands.
    """
    if command not in ("setup", "help", "version", "--version", "-v"):
        print("\n" + "=" * 70, file=sys.stderr)
        print("💡 IMPORTANT: Discuss these results with your financial advisor.", file=sys.stderr)
        print("   InvestorClaw is educational only — not financial advice.", file=sys.stderr)
        print("=" * 70 + "\n", file=sys.stderr)
