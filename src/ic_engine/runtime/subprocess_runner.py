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
Script subprocess executor for InvestorClaw.

Selects the appropriate Python interpreter (skill venv if present, otherwise
the current executable) and runs the target script with a controlled cwd.
Emits an ic_result verification envelope after each invocation.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)


# Commands that trigger auto-dashboard generation after a successful run.
_DASHBOARD_TRIGGER_COMMANDS = frozenset(
    {"holdings", "performance", "bonds", "synthesize", "analyst", "news"}
)


def _maybe_auto_dashboard(command: str, skill_dir: Path) -> None:
    """Auto-generate dashboard after a data-producing command.

    Resolves the engine-bundled dashboard implementation
    (commands/dashboard_deferred.py — the only dashboard module actually
    shipped) and runs it as a subprocess after data-producing commands.
    Falls back to legacy commands/dashboard.py for older layouts that
    still ship that name. If neither exists the function silently
    no-ops — the dashboard is regenerable on demand via the deferred
    flow + dashboard-launch.sh, so missing auto-generation is not a
    pipeline failure.
    """
    try:
        # Prefer the actual shipped name (dashboard_deferred.py — the
        # 'deferred' suffix reflects that artifact generation is opt-in
        # and operator-launched via dashboard-launch.sh under normal
        # operation). Fall back to commands/dashboard.py only for
        # legacy out-of-tree layouts that still carry that name.
        dashboard_script = skill_dir / "commands" / "dashboard_deferred.py"
        if not dashboard_script.exists():
            legacy_script = skill_dir / "commands" / "dashboard.py"
            if legacy_script.exists():
                dashboard_script = legacy_script
            else:
                logger.debug(
                    "Auto-dashboard skipped — no dashboard module "
                    "shipped at %s (or %s). Operator regenerates via "
                    "dashboard-launch.sh on demand.",
                    dashboard_script, legacy_script,
                )
                return

        # Build args via the same synthesizer used by investorclaw.py
        # Use the actual reports_dir, not skill_dir
        from ic_engine.config.command_builders import synthesize_command_args
        from ic_engine.config.path_resolver import get_reports_dir

        reports_dir = get_reports_dir()
        dash_args, error_code = synthesize_command_args("dashboard", [], reports_dir)

        if error_code != 0:
            logger.error(f"Dashboard arg synthesis failed (code {error_code})")
            return

        if not dash_args:
            logger.warning(
                "Dashboard synthesizer returned empty args (holdings_summary.json missing?)"
            )
            return

        logger.debug(f"Running dashboard with args: {dash_args}")

        # Run the dashboard subprocess from the skill_dir (InvestorClaw root)
        result = subprocess.run(
            [sys.executable, str(dashboard_script)] + dash_args,
            capture_output=True,
            text=True,
            cwd=str(skill_dir),
        )

        # Check for subprocess errors
        if result.returncode != 0:
            logger.error(f"Dashboard generation failed with exit code {result.returncode}")
            if result.stderr:
                logger.error(f"Dashboard stderr: {result.stderr}")
            return

        # Validate dashboard produced output
        if not result.stdout.strip():
            logger.error("Dashboard subprocess produced no output (empty stdout)")
            return

        # Parse artifact_path from dashboard JSON output
        try:
            data = json.loads(result.stdout.strip())
            artifact_path = data.get("artifact_path", "")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error(f"Failed to parse dashboard JSON output: {exc}")
            logger.debug(f"Dashboard stdout was: {result.stdout[:200]}")
            return

        # Verify artifact_path is non-empty and the file exists
        if not artifact_path:
            logger.error("Dashboard JSON returned empty artifact_path")
            return

        artifact_file = Path(artifact_path)
        if not artifact_file.exists():
            logger.error(f"Dashboard artifact file not created: {artifact_path}")
            return

        if artifact_file.stat().st_size == 0:
            logger.error(f"Dashboard artifact is empty (zero bytes): {artifact_path}")
            return

        # Success: emit reminder + artifact path for Claude to display
        logger.info(f"Dashboard generation successful: {artifact_path}")
        print(
            json.dumps(
                {
                    "type": "dashboard_ready",
                    "message": f"Dashboard updated after {command}.",
                    "artifact_path": artifact_path,
                }
            )
        )
    except Exception as exc:
        # Never let dashboard failure break the main command pipeline
        logger.exception(f"Unexpected error during dashboard generation: {exc}")


def run_script(
    script_path: Path,
    args: List[str],
    env: Dict[str, str],
    cwd: Path,
    command: str = "",
) -> int:
    """
    Execute *script_path* as a subprocess and return its exit code.

    Args:
        script_path: Absolute path to the Python script to run.
        args:        Argument list (without the interpreter or script name).
        env:         Full environment dict for the subprocess.
        cwd:         Working directory for the subprocess (the skill directory).
        command:     Logical command name (e.g. "holdings") used to trigger
                     stonkmode narration after a successful run.
    """
    # Cross-platform venv python detection.
    # POSIX layout:   <venv>/bin/python3 (or python)
    # Windows layout: <venv>\Scripts\python.exe
    venv_candidates = [
        cwd / "venv" / "bin" / "python3",
        cwd / "venv" / "bin" / "python",
        cwd / "venv" / "Scripts" / "python.exe",
        cwd / "venv" / "Scripts" / "python3.exe",
        cwd / ".venv" / "bin" / "python3",
        cwd / ".venv" / "bin" / "python",
        cwd / ".venv" / "Scripts" / "python.exe",
    ]
    python_exe = sys.executable
    for cand in venv_candidates:
        if cand.exists():
            python_exe = str(cand)
            break

    started = time.perf_counter()
    original_cwd = os.getcwd()
    try:
        os.chdir(cwd)
        result = subprocess.run(
            [python_exe, str(script_path)] + list(args),
            check=False,
            env=env,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)

        # Stonkmode narration — fires after a successful command run when active.
        # Runs in-process so narration lands in stdout before ic_result.
        if result.returncode == 0 and command:
            try:
                import sys as _sys

                _skill_root = str(cwd)
                if _skill_root not in _sys.path:
                    _sys.path.insert(0, _skill_root)
                from ic_engine.rendering.stonkmode import maybe_narrate

                maybe_narrate(command, cwd)
            except Exception:
                pass  # Narration failure must never break the command pipeline

        # Auto-dashboard generation — fires after data-producing commands.
        if result.returncode == 0 and command in _DASHBOARD_TRIGGER_COMMANDS:
            _maybe_auto_dashboard(command, cwd)

        print(
            json.dumps(
                {
                    "ic_result": {
                        "script": script_path.name,
                        "exit_code": result.returncode,
                        "duration_ms": duration_ms,
                    }
                }
            )
        )
        return result.returncode
    finally:
        os.chdir(original_cwd)
