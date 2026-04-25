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
Subprocess environment builder for InvestorClaw script dispatch.

Constructs the env dict passed to each script subprocess:
  - Inherits os.environ (which already reflects the bootstrap precedence chain)
  - Prepends skill_dir and its parent to PYTHONPATH
  - Applies skill/.env one more time via setdefault (belt-and-suspenders for
    keys only needed at subprocess level and not loaded during bootstrap)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

# env_loader is on PYTHONPATH once bootstrap has run; import deferred to
# build_env() so this module remains importable before sys.path is set up.


def build_env(skill_dir: Path) -> Dict[str, str]:
    """
    Return an environment dict suitable for passing to subprocess.run().

    PYTHONPATH is constructed so that both the InvestorClaw parent directory
    and the skill directory itself are importable inside scripts.
    """
    env = os.environ.copy()

    # Build PYTHONPATH so scripts can import without per-file sys.path surgery:
    # Include:
    #   <skill/>              — allows from config.*, from internal.*, from rendering.*, etc.
    #   <skill/commands/>     — allows direct execution of command scripts
    # Use os.pathsep so PYTHONPATH works on both POSIX (":") and Windows (";")
    path_entries = [
        skill_dir,  # <skill/> — root allows from internal.*, from config.*, etc.
        skill_dir / "commands",  # router-mapped command scripts (so they can run directly)
    ]
    pythonpath = os.pathsep.join(str(p.absolute()) for p in path_entries)
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = pythonpath + os.pathsep + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = pythonpath

    # Apply .env a second time for keys that scripts need but bootstrap didn't set
    from config.env_loader import load_env_file

    for k, v in load_env_file(skill_dir / ".env").items():
        env.setdefault(k, v)

    return env
