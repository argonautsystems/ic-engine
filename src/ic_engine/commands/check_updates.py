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
Manual update-check command for InvestorClaw.

Usage:
  investorclaw check-updates       # Detects runtime + prints update
                                   # instruction; always backs up ~/.env.

The command is intentionally non-interactive. It does NOT attempt to
install the update itself — printed instructions are for the user (or
their agent) to execute. See setup/update_checker.py for detection
logic.

Exit codes:
  0 — on current, or update check failed (treated as non-error so hooks
      don't false-positive on transient network errors).
  2 — update available. Callers can detect this distinctly.
"""

import sys
from pathlib import Path

# Add parent to path for imports (this file runs as a subprocess).
sys.path.insert(0, str(Path(__file__).parent.parent))

from ic_engine.setup.update_checker import check_and_emit


def main() -> int:
    # Install path = repo root (this file's parent is commands/, its
    # parent is the repo root where SKILL.md + investorclaw.py live).
    install_path = Path(__file__).resolve().parent.parent
    return check_and_emit(install_path)


if __name__ == "__main__":
    sys.exit(main())
