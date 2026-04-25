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
Path resolution utilities for InvestorClaw.

Finds portfolio files and report directories based on environment and conventions.
"""

import os
from datetime import date
from pathlib import Path
from typing import Optional


def get_portfolio_dir(skill_dir: Path) -> Path:
    """
    Get the portfolio directory (configurable via env var or auto-discovered).

    Discovery order (first match wins):
      1. ``INVESTOR_CLAW_PORTFOLIO_DIR`` env var (canonical)
      2. ``INVESTORCLAW_PORTFOLIO_DIR`` env var (legacy alias used by installer)
      3. ``~/portfolios/`` (if it exists) — recommended user-scoped location
      4. ``<skill_dir>/portfolios/`` — fallback for source-checkout use

    Returns Path to portfolio directory.
    """
    # Accept both canonical and legacy env var names for portability
    for env_name in ("INVESTOR_CLAW_PORTFOLIO_DIR", "INVESTORCLAW_PORTFOLIO_DIR"):
        val = os.environ.get(env_name, "").strip()
        if val:
            return Path(val).expanduser()

    # Prefer ~/portfolios/ when it exists — matches installer default ($HOME/portfolios)
    home_dir = Path.home() / "portfolios"
    if home_dir.exists() and home_dir.is_dir():
        return home_dir

    return skill_dir / "portfolios"


def get_reports_dir() -> Path:
    """
    Get the reports output directory (configurable via env var or default).

    By default, outputs are written to a dated subdirectory:
        {base}/YYYY-MM-DD/

    This keeps each day's run isolated and prevents files from prior runs being
    overwritten silently.  Override with:
        INVESTOR_CLAW_DATED_REPORTS=false   — write directly to base dir
        INVESTOR_CLAW_RUN_DATE=YYYY-MM-DD   — force a specific date (e.g. for re-runs)

    Returns Path to reports directory (creates if doesn't exist).
    """
    _reports_env = os.environ.get("INVESTOR_CLAW_REPORTS_DIR", "").strip()
    base_dir = (
        Path(_reports_env).expanduser() if _reports_env else Path.home() / "portfolio_reports"
    )

    dated_env = os.environ.get("INVESTOR_CLAW_DATED_REPORTS", "true").strip().lower()
    if dated_env not in ("false", "0", "no", "off"):
        run_date = os.environ.get("INVESTOR_CLAW_RUN_DATE", "").strip()
        reports_dir = base_dir / (run_date if run_date else date.today().isoformat())
    else:
        reports_dir = base_dir

    reports_dir.mkdir(parents=True, exist_ok=True)
    # Set directory permissions to 0700 (owner only) to protect sensitive financial data
    reports_dir.chmod(0o700)
    return reports_dir


def find_portfolio_file(skill_dir: Path) -> Optional[str]:
    """
    Find the best portfolio file to use.

    Priority:
    1. master_portfolio.csv (consolidation output)
    2. Most recently modified *_extracted.csv
    3. Most recently modified *.csv file

    Returns path string, or None if no file found.
    """
    portfolio_dir = get_portfolio_dir(skill_dir)

    # First choice: master_portfolio.csv (consolidation output)
    master = portfolio_dir / "master_portfolio.csv"
    if master.exists():
        return str(master)

    # Second choice: any *_extracted.csv file (but not bonds)
    extracted_files = list(portfolio_dir.glob("*_extracted.csv"))
    extracted_files = [f for f in extracted_files if "_bonds" not in f.name]
    if extracted_files:
        latest = max(extracted_files, key=lambda p: p.stat().st_mtime)
        return str(latest)

    # Third choice: any raw *.csv file (e.g., directly placed broker exports)
    raw_csv_files = [
        f
        for f in portfolio_dir.glob("*.csv")
        if not f.name.startswith(".") and "_bonds" not in f.name
    ]
    if raw_csv_files:
        latest = max(raw_csv_files, key=lambda p: p.stat().st_mtime)
        return str(latest)

    # Fallback: return None
    return None


def secure_file_permissions(file_path: Path) -> None:
    """Set sensitive output files to owner-only (0600) permissions."""
    if file_path.exists():
        file_path.chmod(0o600)
