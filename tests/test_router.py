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
Unit tests for runtime/router.py.

Tests command resolution and guardrail-priming decisions without needing
actual script files or a live portfolio.
"""

import sys
from pathlib import Path

_skill_root = Path(__file__).parent.parent
if str(_skill_root) not in sys.path:
    sys.path.insert(0, str(_skill_root))

import pytest

from runtime.router import (
    _DISPATCH_SENTINEL,
    COMMANDS,
    DEFAULT_SECTIONS,
    NON_ANALYSIS_COMMANDS,
    SECTION_DISPATCH,
    resolve_script,
    should_prime_guardrails,
    synthesize_args,
)

# ---------------------------------------------------------------------------
# COMMANDS registry sanity checks
# ---------------------------------------------------------------------------


def test_commands_dict_not_empty():
    assert len(COMMANDS) > 10


def test_all_command_values_are_py_files():
    """All COMMANDS entries are either a .py script or the v2.2 dispatch sentinel."""
    for cmd, script in COMMANDS.items():
        assert script.endswith(".py") or script == _DISPATCH_SENTINEL, (
            f"Command '{cmd}' maps to non-.py: {script}"
        )


def test_known_commands_present():
    for cmd in ("holdings", "bonds", "news", "analyst", "performance", "setup"):
        assert cmd in COMMANDS, f"Expected command '{cmd}' missing from COMMANDS"


# ---------------------------------------------------------------------------
# resolve_script
# ---------------------------------------------------------------------------


def test_resolve_script_unknown_command(tmp_path, capsys):
    result = resolve_script("not-a-real-command", tmp_path)
    assert result is None
    captured = capsys.readouterr()
    assert "Unknown command" in captured.err


def test_resolve_script_missing_script_file(tmp_path, capsys):
    # holdings → fetch_holdings.py, but we use an empty tmp dir
    result = resolve_script("holdings", tmp_path)
    assert result is None
    captured = capsys.readouterr()
    assert "Script not found" in captured.err


def test_resolve_script_returns_path_when_file_exists(tmp_path):
    # Create a stub script file so exists() returns True
    (tmp_path / "fetch_holdings.py").touch()
    result = resolve_script("holdings", tmp_path)
    assert result is not None
    assert result == tmp_path / "fetch_holdings.py"


def test_resolve_script_alias(tmp_path):
    """'snapshot' is an alias for 'holdings' → fetch_holdings.py."""
    (tmp_path / "fetch_holdings.py").touch()
    result = resolve_script("snapshot", tmp_path)
    assert result is not None
    assert result.name == "fetch_holdings.py"


# ---------------------------------------------------------------------------
# should_prime_guardrails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "holdings",
        "bonds",
        "news",
        "analyst",
        "performance",
        "synthesize",
        "analysis",
    ],
)
def test_analysis_commands_should_prime(command):
    assert should_prime_guardrails(command) is True


@pytest.mark.parametrize("command", list(NON_ANALYSIS_COMMANDS))
def test_non_analysis_commands_skip_priming(command):
    assert should_prime_guardrails(command) is False


# ---------------------------------------------------------------------------
# NON_ANALYSIS_COMMANDS coverage
# ---------------------------------------------------------------------------


def test_setup_in_non_analysis():
    assert "setup" in NON_ANALYSIS_COMMANDS


def test_report_in_non_analysis():
    assert "report" in NON_ANALYSIS_COMMANDS


def test_help_in_non_analysis():
    assert "help" in NON_ANALYSIS_COMMANDS


# ---------------------------------------------------------------------------
# v2.2 SECTION_DISPATCH coverage (RFC §3.3, r2.3)
# ---------------------------------------------------------------------------


def test_section_dispatch_has_all_v2_2_wrappers():
    """The 6 wrappers from RFC §3.1 must be in SECTION_DISPATCH."""
    expected = {"view", "compute", "target", "scenario", "market", "bonds"}
    assert set(SECTION_DISPATCH.keys()) == expected


def test_default_sections_cover_every_wrapper():
    """Every dispatch wrapper has a default section."""
    for wrapper in SECTION_DISPATCH:
        assert wrapper in DEFAULT_SECTIONS, f"No default section for '{wrapper}'"
        default = DEFAULT_SECTIONS[wrapper]
        assert default in SECTION_DISPATCH[wrapper], (
            f"Default section '{default}' for '{wrapper}' not in dispatch table"
        )


def test_v2_2_sentinel_wrappers_in_commands():
    """Sentinel-only wrappers are addressable through COMMANDS."""
    for wrapper in ("view", "compute", "target"):
        assert wrapper in COMMANDS
        assert COMMANDS[wrapper] == _DISPATCH_SENTINEL


def test_v2_2_legacy_aliases_preserved():
    """Legacy CLI aliases must remain after v2.2 (RFC §8 resolved entry 3)."""
    legacy = [
        "holdings",
        "performance",
        "bonds",
        "analyst",
        "news",
        "synthesize",
        "optimize",
        "rebalance-tax",
        "session",
        "scenario",
        "concept",
        "market",
        "fixed-income",
        "analysis",
    ]
    for cmd in legacy:
        assert cmd in COMMANDS, f"Legacy CLI alias '{cmd}' missing from COMMANDS"


@pytest.mark.parametrize(
    "wrapper,section,expected_script",
    [
        ("view", "holdings", "fetch_holdings.py"),
        ("view", "performance", "analyze_performance_polars.py"),
        ("view", "analyst", "fetch_analyst_recommendations_parallel.py"),
        ("view", "news", "fetch_portfolio_news.py"),
        ("view", "dashboard", "dashboard_deferred.py"),
        ("compute", "synthesize", "portfolio_analyzer.py"),
        ("compute", "optimize-sharpe", "optimize.py"),
        ("compute", "optimize-minvol", "optimize.py"),
        ("compute", "optimize-blacklitterman", "optimize.py"),
        ("target", "allocation", "session_init.py"),
        ("target", "drift", "session_init.py"),
        ("scenario", "rebalance", "scenario.py"),
        ("scenario", "stress", "scenario.py"),
        ("scenario", "tax-aware", "rebalance_tax.py"),
        ("market", "concept", "concept_decline.py"),
        ("market", "market", "concept_decline.py"),
        ("bonds", "analysis", "bond_analyzer.py"),
        ("bonds", "strategy", "fixed_income_analysis.py"),
    ],
)
def test_resolve_script_section_dispatch(tmp_path, wrapper, section, expected_script):
    """Every (wrapper, section) pair resolves to its declared script."""
    (tmp_path / expected_script).touch()
    result = resolve_script(wrapper, tmp_path, section=section)
    assert result is not None, f"resolve_script({wrapper}, {section}) returned None"
    assert result.name == expected_script


@pytest.mark.parametrize(
    "wrapper,default_script",
    [
        ("view", "fetch_holdings.py"),
        ("compute", "portfolio_analyzer.py"),
        ("target", "session_init.py"),
        ("scenario", "scenario.py"),
        ("bonds", "bond_analyzer.py"),
    ],
)
def test_resolve_script_default_section(tmp_path, wrapper, default_script):
    """resolve_script(wrapper, section=None) uses DEFAULT_SECTIONS."""
    (tmp_path / default_script).touch()
    result = resolve_script(wrapper, tmp_path)
    assert result is not None
    assert result.name == default_script


def test_resolve_script_invalid_section_emits_envelope(tmp_path, capsys):
    """Invalid section returns None and prints an ic_result error envelope."""
    result = resolve_script("view", tmp_path, section="not-a-real-section")
    assert result is None
    captured = capsys.readouterr()
    assert '"error": "Invalid section"' in captured.out
    assert '"section_provided": "not-a-real-section"' in captured.out
    assert '"allowed_sections":' in captured.out
    assert "exit_code" in captured.out


def test_resolve_script_legacy_alias_ignores_section(tmp_path):
    """Legacy CLI aliases (not in SECTION_DISPATCH) ignore the section param."""
    (tmp_path / "fetch_holdings.py").touch()
    # `snapshot` is a legacy alias for fetch_holdings.py
    result = resolve_script("snapshot", tmp_path, section="anything")
    assert result is not None
    assert result.name == "fetch_holdings.py"


def test_resolve_script_legacy_holdings_still_works(tmp_path):
    """`investorclaw holdings` (legacy alias) resolves without --section."""
    (tmp_path / "fetch_holdings.py").touch()
    result = resolve_script("holdings", tmp_path)
    assert result is not None
    assert result.name == "fetch_holdings.py"


def test_resolve_script_market_section_news_pending(tmp_path):
    """market --section=news routes to fetch_market_news.py (script lands in step 3b)."""
    (tmp_path / "fetch_market_news.py").touch()
    result = resolve_script("market", tmp_path, section="news")
    assert result is not None
    assert result.name == "fetch_market_news.py"


# ---------------------------------------------------------------------------
# v2.2 synthesize_args wrapper handling (CSV-strip + section pass-through)
# ---------------------------------------------------------------------------


def test_synthesize_args_view_holdings_csv_strip_preserved(tmp_path):
    """`view --section=holdings` should accept a positional CSV like the legacy
    `holdings` command does. CSV-strip behavior must NOT fire here."""
    csv_file = tmp_path / "portfolio.csv"
    csv_file.write_text("symbol,quantity\nAAPL,100\n")
    args, code = synthesize_args("view", [str(csv_file)], tmp_path, section="holdings")
    assert code == 0
    # First positional arg should still be the CSV (mapped to fetch_holdings.py
    # contract: portfolio_file, output_file, ...)
    assert any(str(csv_file) in str(a) for a in args)


def test_synthesize_args_view_performance_csv_dropped(tmp_path):
    """`view --section=performance` should drop a leading CSV positional, since
    analyze_performance_polars.py does NOT take a CSV positional.

    Downstream synthesis may still fail (missing holdings.json), but the CSV
    must NOT appear in the returned args regardless. We only assert CSV-strip,
    not the synthesis success path.
    """
    csv_file = tmp_path / "portfolio.csv"
    csv_file.write_text("symbol,quantity\nAAPL,100\n")
    args, code = synthesize_args("view", [str(csv_file)], tmp_path, section="performance")
    # The CSV must have been stripped before downstream synthesis ran,
    # whether or not synthesis itself succeeded.
    assert not any(str(csv_file) == a for a in args), (
        f"CSV {csv_file} leaked through to performance args: {args}"
    )
