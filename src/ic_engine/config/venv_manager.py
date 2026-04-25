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
Virtual Environment Manager - Transparently manages Python venv with uv.

Automatically:
- Detects if running in virtual environment
- Creates venv if missing
- Runs `uv sync` to install dependencies
- Activates venv for subprocesses
- All transparent to the user

Usage:
  from config.venv_manager import ensure_venv, get_venv_python

  # Auto-setup venv (transparent)
  ensure_venv()

  # Get path to venv Python (for subprocess calls)
  python_path = get_venv_python()
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def get_project_root() -> Path:
    """Get InvestorClaw project root directory."""
    return Path(__file__).parent.parent


def get_venv_path() -> Path:
    """Get path to virtual environment (.venv in project root)."""
    return get_project_root() / ".venv"


def get_venv_python() -> Path:
    """Get path to Python executable in venv."""
    venv_path = get_venv_path()
    if sys.platform == "win32":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def get_venv_activate_script() -> Path:
    """Get path to venv activation script."""
    venv_path = get_venv_path()
    if sys.platform == "win32":
        return venv_path / "Scripts" / "activate.bat"
    return venv_path / "bin" / "activate"


def is_venv_active() -> bool:
    """Check if currently running in a virtual environment."""
    return hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )


def is_venv_initialized() -> bool:
    """Check if venv exists and is properly initialized."""
    venv_path = get_venv_path()
    python_exe = get_venv_python()

    return venv_path.exists() and python_exe.exists()


def check_uv_installed() -> bool:
    """Check if uv is installed in system."""
    try:
        result = subprocess.run(
            ["uv", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def install_uv() -> bool:
    """Refuse implicit uv installation; require an explicit/manual install step."""
    if sys.platform == "win32":
        logger.warning("uv installation on Windows requires manual installation")
        logger.warning("Install from: https://github.com/astral-sh/uv#installation")
        return False

    logger.warning("uv not found; automatic installation is disabled for safety")
    logger.warning("Install uv manually from: https://github.com/astral-sh/uv#installation")
    return False


def create_venv() -> bool:
    """Create virtual environment using uv."""
    try:
        logger.info("Creating virtual environment...")
        project_root = get_project_root()

        result = subprocess.run(
            ["uv", "venv", str(get_venv_path())],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            logger.info("✓ Virtual environment created")
            return True
        else:
            logger.error(f"Failed to create venv: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"Failed to create venv: {e}")
        return False


def sync_dependencies() -> bool:
    """Sync dependencies with uv sync."""
    try:
        logger.info("Syncing dependencies (this may take a minute)...")
        project_root = get_project_root()

        result = subprocess.run(
            ["uv", "sync"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        if result.returncode == 0:
            logger.info("✓ Dependencies synced")
            return True
        else:
            logger.error(f"Failed to sync dependencies: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        logger.error("Dependency sync timed out (>5 minutes)")
        return False
    except Exception as e:
        logger.error(f"Failed to sync dependencies: {e}")
        return False


def ensure_venv() -> bool:
    """
    Ensure virtual environment is set up and dependencies synced.

    Handles the full setup transparently:
    1. Check if uv is installed (install if missing)
    2. Create venv if missing
    3. Sync dependencies if needed

    Returns:
        True if setup successful or already initialized, False otherwise.
    """
    # If already in venv, nothing to do
    if is_venv_active():
        logger.debug("Already running in virtual environment")
        return True

    # If venv is initialized, just verify dependencies
    if is_venv_initialized():
        logger.debug("Virtual environment already exists")
        # Optionally sync to pick up any new dependencies
        # (disabled by default to not slow down startup)
        return True

    # Full setup needed
    logger.info("Setting up InvestorClaw virtual environment...")

    # Step 1: Check/install uv
    if not check_uv_installed():
        logger.info("uv not found, attempting to install...")
        if not install_uv():
            logger.error("❌ Could not install or find uv")
            logger.error("Please install uv manually: https://github.com/astral-sh/uv#installation")
            return False

    # Step 2: Create venv
    if not create_venv():
        logger.error("❌ Failed to create virtual environment")
        return False

    # Step 3: Sync dependencies
    if not sync_dependencies():
        logger.error("❌ Failed to sync dependencies")
        return False

    logger.info("✓ Virtual environment ready")
    return True


def get_activation_command() -> str:
    """
    Get shell command to activate venv.

    Returns shell-appropriate activation command for the user's shell.
    """
    venv_path = get_venv_path()

    if sys.platform == "win32":
        return f"{venv_path / 'Scripts' / 'activate.bat'}"

    shell_name = os.path.basename(os.environ.get("SHELL", "bash"))
    if shell_name == "zsh":
        return f"source {venv_path / 'bin' / 'activate'}"
    elif shell_name == "fish":
        return f"source {venv_path / 'bin' / 'activate.fish'}"
    else:
        return f"source {venv_path / 'bin' / 'activate'}"


def run_in_venv(command: list, cwd: Optional[Path] = None) -> int:
    """
    Run a command in the virtual environment.

    Args:
        command: Command and arguments as list
        cwd: Working directory (defaults to project root)

    Returns:
        Exit code from the command
    """
    ensure_venv()

    # Use venv's Python
    python_path = get_venv_python()

    # If first arg is "python", replace with venv python
    if command and command[0] in ("python", "python3"):
        command = [str(python_path)] + command[1:]

    cwd = cwd or get_project_root()

    try:
        result = subprocess.run(command, cwd=str(cwd))
        return result.returncode
    except Exception as e:
        logger.error(f"Failed to run command: {e}")
        return 1
