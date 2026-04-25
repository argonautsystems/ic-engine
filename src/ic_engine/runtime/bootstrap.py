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
InvestorClaw startup bootstrap — first-run checks and configuration loading.

Env/config precedence enforced here (highest → lowest):
  1. os.environ at process start  — explicit agent/shell overrides; never touched
  2. ~/.investorclaw/setup_config.json  — applied via os.environ.setdefault()
  3. skill/.env  — applied last via os.environ.setdefault(); fills remaining gaps
"""

from __future__ import annotations

import os
from pathlib import Path


def run_bootstrap(skill_dir: Path) -> None:
    """
    Run startup initialization for a normal InvestorClaw invocation.

    Should be called once per process for all commands except setup/help.
    Imports are deferred so this module is importable before sys.path is fully
    resolved (though in practice investorclaw.py sets sys.path first).
    """
    from config.config_loader import get_deployment_type
    from config.config_loader import initialize as initialize_config
    from config.env_loader import load_env_file
    from services.context_window_monitor import warn_if_low_context
    from setup.first_run_check import check_and_offer
    from setup.update_checker import check_for_updates as _fetch_update_message

    check_and_offer()
    initialize_config()  # loads setup_config.json; applies via setdefault

    # Load skill/.env last so it only fills remaining gaps in os.environ
    for k, v in load_env_file(skill_dir / ".env").items():
        os.environ.setdefault(k, v)

    # Passive update check: detect runtime + compose message, then print.
    # No interactive prompts, no auto-install. Skippable via env var.
    if os.environ.get("INVESTORCLAW_SKIP_UPDATE_CHECK", "").lower() not in ("1", "true", "yes"):
        try:
            msg = _fetch_update_message(skill_dir)
            # Only surface when something actionable is present — skip the
            # "no update" / "check failed" paths during bootstrap to keep
            # the startup banner clean.
            if "available" in msg and "no update available" not in msg:
                print(msg)
        except Exception:
            pass  # never fail bootstrap on an update-check glitch

    # Warn if operational model has insufficient context window
    if get_deployment_type() == "focused":
        warn_if_low_context()
