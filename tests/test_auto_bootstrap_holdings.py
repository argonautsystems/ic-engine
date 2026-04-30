# Copyright 2026 InvestorClaw Contributors
# Licensed under the Apache License, Version 2.0
"""Regression: ic-engine v2.6.3 cold-cache cascade fix.

Bug history: ask.py used to call get_or_run(holdings_path) where holdings_path
was a CSV broker export. Downstream HoldingsLoader.load() does json.load() on
whatever path it gets, which crashed with JSONDecodeError, cascading every
analysis section to "did not run". Fix lives in router.auto_bootstrap_holdings
(formerly _auto_bootstrap_holdings) plus the rebind in ask.py main().

These tests guard the helper's contract — that callers can rely on the return
value to plumb the materialized path forward.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ic_engine.runtime.router import auto_bootstrap_holdings, _auto_bootstrap_holdings


def test_returns_existing_raw_holdings_when_present(tmp_path: Path) -> None:
    """If .raw/holdings.json already exists, return its path (no subprocess)."""
    reports_dir = tmp_path / "reports"
    raw = reports_dir / ".raw" / "holdings.json"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"portfolio": {}}')

    result = auto_bootstrap_holdings("ask", tmp_path / "skill", reports_dir)
    assert result == raw


def test_returns_existing_legacy_holdings_when_present(tmp_path: Path) -> None:
    """If legacy reports_dir/holdings.json exists (no .raw/), return that path."""
    reports_dir = tmp_path / "reports"
    legacy = reports_dir / "holdings.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"portfolio": {}}')

    result = auto_bootstrap_holdings("ask", tmp_path / "skill", reports_dir)
    assert result == legacy


def test_returns_none_for_non_consumer_command(tmp_path: Path) -> None:
    """Commands like llm-config should be silent no-ops."""
    result = auto_bootstrap_holdings(
        "llm-config", tmp_path / "skill", tmp_path / "reports"
    )
    assert result is None


def test_returns_none_for_holdings_command(tmp_path: Path) -> None:
    """The 'holdings' command itself shouldn't recursively bootstrap."""
    result = auto_bootstrap_holdings(
        "holdings", tmp_path / "skill", tmp_path / "reports"
    )
    assert result is None


def test_explicit_portfolio_path_overrides_discovery(tmp_path: Path) -> None:
    """When portfolio_path is set, helper must NOT call find_portfolio_file."""
    reports_dir = tmp_path / "reports"
    skill_dir = tmp_path / "skill"
    portfolio_csv = tmp_path / "external" / "Q1.csv"
    portfolio_csv.parent.mkdir(parents=True)
    portfolio_csv.write_text("symbol,quantity\nAAPL,100\n")

    # Mock subprocess + fetch_script so we can verify the discovery path
    # isn't called and the explicit path IS passed through.
    # find_portfolio_file is imported inside the function body, so we patch
    # it at its source module rather than on router (where it isn't visible).
    with patch("ic_engine.config.path_resolver.find_portfolio_file") as mock_find, \
         patch("ic_engine.runtime.router.subprocess.run") as mock_run, \
         patch("ic_engine.cli.SCRIPTS_DIR", tmp_path / "scripts_dir"):
        # fetch_holdings.py needs to "exist" for the bootstrap to attempt to run.
        fetch = tmp_path / "scripts_dir" / "fetch_holdings.py"
        fetch.parent.mkdir(parents=True)
        fetch.write_text("# stub")

        # Stub subprocess: pretend it succeeded and wrote the file.
        def fake_run(cmd, **kwargs):
            # cmd[2] is portfolio_file (the CSV the helper passed in).
            # cmd[3] is the destination raw_holdings path.
            Path(cmd[3]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[3]).write_text('{"portfolio": {}}')

            class _R:
                returncode = 0
                stderr = b""

            return _R()

        mock_run.side_effect = fake_run

        result = auto_bootstrap_holdings(
            "ask", skill_dir, reports_dir, portfolio_path=portfolio_csv
        )

        # The discovery path must NOT be called when explicit path is provided.
        mock_find.assert_not_called()
        # The materialized JSON path must be returned.
        assert result == reports_dir / ".raw" / "holdings.json"
        assert result.exists()
        # The CSV path must have been passed to fetch_holdings.py.
        called_cmd = mock_run.call_args[0][0]
        assert str(portfolio_csv) in called_cmd


def test_subprocess_failure_returns_none_logs_warning(tmp_path: Path, caplog) -> None:
    """Bootstrap subprocess failures must surface a WARNING + return None."""
    import logging

    reports_dir = tmp_path / "reports"
    skill_dir = tmp_path / "skill"
    portfolio_csv = tmp_path / "Q1.csv"
    portfolio_csv.write_text("symbol,quantity\nAAPL,100\n")

    with patch("ic_engine.runtime.router.subprocess.run") as mock_run, \
         patch("ic_engine.cli.SCRIPTS_DIR", tmp_path / "scripts_dir"):
        fetch = tmp_path / "scripts_dir" / "fetch_holdings.py"
        fetch.parent.mkdir(parents=True)
        fetch.write_text("# stub")

        class _Failed:
            returncode = 1
            stderr = b"fetch_holdings.py: HTTP 503 from upstream API\n"

        mock_run.return_value = _Failed()

        with caplog.at_level(logging.WARNING):
            result = auto_bootstrap_holdings(
                "ask", skill_dir, reports_dir, portfolio_path=portfolio_csv
            )

        assert result is None
        # WARNING must mention the failure (not just a debug-level swallow).
        assert any(
            "auto-bootstrap" in rec.message.lower() and rec.levelno == logging.WARNING
            for rec in caplog.records
        ), f"expected WARNING-level bootstrap-failure log; got: {[r.message for r in caplog.records]}"


def test_back_compat_alias_still_works(tmp_path: Path) -> None:
    """The old _auto_bootstrap_holdings name must still be callable for any
    consumer that imported it from a prior ic-engine release."""
    assert _auto_bootstrap_holdings is auto_bootstrap_holdings
