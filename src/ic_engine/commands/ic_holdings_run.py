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


"""Holdings wrapper: auto-detects portfolio CSV, creates output dirs, delegates to fetch_holdings."""

import os
import sys
from pathlib import Path

IC_HOME = Path(__file__).parent.parent

# Determine reports dir (env override or default)
reports_dir = Path(
    os.environ.get("INVESTOR_CLAW_REPORTS_DIR", str(Path.home() / "portfolio_reports"))
)
raw_dir = reports_dir / ".raw"
raw_dir.mkdir(parents=True, exist_ok=True)

# Determine portfolio dir (env override or defaults)
portfolio_dir_env = os.environ.get("INVESTOR_CLAW_PORTFOLIO_DIR", "")
search_dirs = [
    Path(portfolio_dir_env) if portfolio_dir_env else None,
    IC_HOME / "portfolios",
    Path.home() / "portfolios",
]

csv_file = None
for d in search_dirs:
    if d and d.exists():
        csvs = sorted(d.glob("*.csv"))
        if csvs:
            csv_file = str(csvs[0])
            break

if not csv_file:
    print(
        "ERROR: No portfolio CSV found. Place CSV in ~/portfolios/ or set INVESTOR_CLAW_PORTFOLIO_DIR."
    )
    sys.exit(1)

print(f"Using CSV: {csv_file}")
print(f"Output: {raw_dir / 'holdings.json'}")

env = dict(os.environ, PYTHONPATH=str(IC_HOME))
import subprocess

sys.exit(
    subprocess.call(
        [
            sys.executable,
            str(IC_HOME / "commands" / "fetch_holdings.py"),
            csv_file,
            str(raw_dir / "holdings.json"),
        ],
        env=env,
    )
)
