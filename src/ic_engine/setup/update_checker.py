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
Update checker for InvestorClaw.

Detects runtime context (Claude Code plugin / OpenClaw skill / ZeroClaw
skill / Hermes skill / standalone git clone) and emits the appropriate
update instruction for that runtime. Always backs up the ``.env`` file
to a timestamped copy under ``~/.investorclaw-backups/`` before the user
runs the update command, so API keys survive any
delete-and-reinstall flow.

Non-interactive by design: no prompts, no auto-git-pull, no pip-install.
Detect → compare → back up → print → exit. The user (or their agent)
runs the printed command themselves.

Version source is the running module's VERSION constant; latest is
fetched from the canonical plugin manifest at
``gitlab.com/perlowja/InvestorClaw/-/raw/main/.claude-plugin/plugin.json``.
GitLab is the primary anonymous-read surface — its free tier does not
throttle anonymous raw fetches the way GitHub's
``raw.githubusercontent.com`` does on heavy-traffic IPs, which makes it
the right surface for a hands-off auto-update probe that runs at
arbitrary times from arbitrary networks.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Module-local logger — plain prints, no logging framework dependency so
# this module works before the app's own logger is configured.
def _say(msg: str) -> None:
    print(msg)


PLUGIN_JSON_URL = (
    "https://gitlab.com/perlowja/InvestorClaw/-/raw/main/.claude-plugin/plugin.json"
)

CHANGELOG_ANCHOR = "https://gitlab.com/perlowja/InvestorClaw/-/blob/main/RELEASE_NOTES.md"
INSTALLER_BASE = "https://gitlab.com/perlowja/InvestorClaw/-/raw/main"

# Per-runtime installer sub-paths. These must match the actual paths in
# the upstream repo. Keep in sync with the install.sh files the docs
# point at.
_INSTALLER_PATHS = {
    "openclaw": "/openclaw/install.sh",
    "zeroclaw": "/zeroclaw/install.sh",
    "hermes": "/hermes/install.sh",
}

BACKUP_DIR = Path.home() / ".investorclaw-backups"


@dataclass
class RuntimeContext:
    """Everything compose_message() needs to emit the right instructions."""

    cluster: str  # "claude-code" | "reinstall" | "git" | "unknown"
    runtime_name: str  # "Claude Code plugin" / "OpenClaw skill" / ...
    update_command: Optional[str]  # slash command for cluster A, None otherwise
    installer_url: Optional[str]  # canonical installer for cluster B, None otherwise
    install_path: Path  # where the install currently lives


# ─────────────────────────────────────────────────────────────────────────
# Version source
# ─────────────────────────────────────────────────────────────────────────


def get_current_version() -> str:
    """Read the canonical VERSION constant from investorclaw.py.

    Imports the module rather than parsing the file; the module already
    resolves the env override for us.
    """
    try:
        # Ensure the repo root is importable. This file is setup/update_checker.py;
        # parent.parent is the repo root where investorclaw.py lives.
        repo_root = Path(__file__).resolve().parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        import investorclaw as ic  # type: ignore

        return getattr(ic, "__version__", getattr(ic, "VERSION", "0.0.0"))
    except Exception:
        return "0.0.0"


def fetch_latest_version(timeout: float = 5.0) -> Optional[str]:
    """Fetch the canonical upstream version from the plugin.json manifest.

    Returns None on any fetch/parse failure (network, timeout, missing key).
    We hit GitLab's raw-file URL because it serves the manifest
    unconditionally to anonymous readers, with no rate-limit ceiling that
    fires on shared IPs the way GitHub's anonymous API does.
    """
    try:
        import urllib.request

        with urllib.request.urlopen(PLUGIN_JSON_URL, timeout=timeout) as resp:
            payload = resp.read()
        data = json.loads(payload)
        return data.get("version")
    except Exception:
        return None


def _is_newer(latest: str, current: str) -> bool:
    """True if *latest* is semantically greater than *current*.

    Uses packaging.version if available, falls back to string compare.
    packaging is a pyproject dep so this should succeed in installed
    environments; the fallback is for raw-source uses.
    """
    try:
        from packaging import version as pkg_version

        return pkg_version.parse(latest) > pkg_version.parse(current)
    except Exception:
        return latest != current and latest > current


# ─────────────────────────────────────────────────────────────────────────
# Runtime detection
# ─────────────────────────────────────────────────────────────────────────


def detect_runtime(install_path: Path) -> RuntimeContext:
    """Classify the current install into a cluster + runtime.

    Args:
        install_path: the skill's install directory (where SKILL.md lives).

    Returns:
        RuntimeContext with cluster / runtime_name / update command or
        installer URL ready to surface in the user-facing message.
    """
    path_str = str(install_path).replace("\\", "/")

    # Cluster A — Claude Code marketplace install. Path lives under
    # ~/.claude/plugins/<marketplace>/<plugin>/.
    if "/.claude/plugins/" in path_str:
        return RuntimeContext(
            cluster="claude-code",
            runtime_name="Claude Code plugin",
            update_command="/plugin update investorclaw@investorclaw",
            installer_url=None,
            install_path=install_path,
        )

    # Cluster B — reinstall-based runtimes. Order matters: match the most
    # specific path markers first.
    for marker, runtime in (
        ("/.openclaw/workspace/skills/", "openclaw"),
        ("/.zeroclaw/workspace/skills/", "zeroclaw"),
        ("/.hermes/skills/", "hermes"),
    ):
        if marker in path_str:
            return RuntimeContext(
                cluster="reinstall",
                runtime_name=f"{runtime.title()} skill",
                update_command=None,
                installer_url=INSTALLER_BASE + _INSTALLER_PATHS[runtime],
                install_path=install_path,
            )

    # Hermes secondary marker: some Hermes skill layouts put the skill
    # directly under ~/.hermes/investorclaw or ~/investorclaw alongside
    # a ~/.hermes/config.yaml. Conservative: if ~/.hermes/ exists and the
    # install is under it, treat as hermes.
    hermes_home = Path.home() / ".hermes"
    if hermes_home.exists():
        try:
            if install_path.is_relative_to(hermes_home):
                return RuntimeContext(
                    cluster="reinstall",
                    runtime_name="Hermes skill",
                    update_command=None,
                    installer_url=INSTALLER_BASE + _INSTALLER_PATHS["hermes"],
                    install_path=install_path,
                )
        except (ValueError, AttributeError):
            pass

    # Standalone git clone — has a .git directory at the install root.
    if (install_path / ".git").exists():
        return RuntimeContext(
            cluster="git",
            runtime_name="standalone git clone",
            update_command=None,
            installer_url=None,
            install_path=install_path,
        )

    # Fallback — unknown runtime.
    return RuntimeContext(
        cluster="unknown",
        runtime_name="unknown runtime",
        update_command=None,
        installer_url=None,
        install_path=install_path,
    )


# ─────────────────────────────────────────────────────────────────────────
# .env backup
# ─────────────────────────────────────────────────────────────────────────


def backup_env(install_path: Path) -> Optional[Path]:
    """Copy the install's ``.env`` (if present) to a timestamped backup.

    Returns the backup file path, or None if no ``.env`` was found.
    Intentionally always runs regardless of cluster — even Claude Code's
    /plugin update may replace the workspace, and the cost of an
    unnecessary backup is negligible.
    """
    env_file = install_path / ".env"
    if not env_file.is_file():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f".env.{timestamp}"
    shutil.copy2(env_file, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


# ─────────────────────────────────────────────────────────────────────────
# Message composition
# ─────────────────────────────────────────────────────────────────────────


def compose_message(
    current: str,
    latest: str,
    runtime: RuntimeContext,
    backup_path: Optional[Path],
) -> str:
    """Build the full user-facing update-available message.

    Always includes the changelog link. Always includes an env-restore
    line when a backup was taken. Cluster-specific instructions in the
    middle.
    """
    lines = []
    lines.append("")
    lines.append(f"📦 InvestorClaw {latest} available (you have {current})")
    lines.append(f"   Runtime: {runtime.runtime_name}")
    lines.append("")

    if backup_path is not None:
        lines.append(f"   🔐 API keys backed up to: {backup_path}")
        lines.append("")

    if runtime.cluster == "claude-code":
        lines.append("   To update:")
        lines.append(f"     {runtime.update_command}")
        if backup_path is not None:
            lines.append("")
            lines.append("   If the update wipes the workspace .env, restore with:")
            lines.append(f"     cp {backup_path} {runtime.install_path}/.env")

    elif runtime.cluster == "reinstall":
        lines.append("   Updating requires a delete + reinstall. Instruct your agent to:")
        lines.append("     1. Delete InvestorClaw from the workspace")
        lines.append("     2. Reinstall from upstream:")
        lines.append(f"        curl -sSL {runtime.installer_url} | bash")
        if backup_path is not None:
            lines.append("")
            lines.append("   After reinstall, restore your API keys:")
            lines.append(f"     cp {backup_path} {runtime.install_path}/.env")

    elif runtime.cluster == "git":
        lines.append("   To update in place (preserves local .env):")
        lines.append(f"     cd {runtime.install_path} && git pull origin main && uv sync")

    else:  # unknown
        lines.append("   Detection couldn't classify your install path.")
        lines.append("   Pick the installer matching your runtime:")
        for name, path in _INSTALLER_PATHS.items():
            lines.append(f"     {name:10}  curl -sSL {INSTALLER_BASE + path} | bash")
        lines.append("     git clone  cd <repo> && git pull origin main && uv sync")
        if backup_path is not None:
            lines.append("")
            lines.append("   After reinstall, restore your API keys:")
            lines.append(f"     cp {backup_path} <install-path>/.env")

    lines.append("")
    lines.append(f"   Changelog: {CHANGELOG_ANCHOR}#v{latest.replace('.', '')}")
    lines.append("")
    return "\n".join(lines)


def compose_up_to_date(current: str) -> str:
    """Message when no update is needed."""
    return f"✅ InvestorClaw {current} is current — no update available.\n"


def compose_check_failed(
    current: str, reason: str = "network or upstream manifest unreachable"
) -> str:
    """Message when the version check itself failed."""
    return (
        f"⚠️ InvestorClaw {current} installed; update check skipped ({reason}).\n"
        f"   Changelog: {CHANGELOG_ANCHOR}\n"
    )


# ─────────────────────────────────────────────────────────────────────────
# Orchestrators
# ─────────────────────────────────────────────────────────────────────────


def check_for_updates(install_path: Optional[Path] = None) -> str:
    """High-level entry point — returns the user-facing message.

    Args:
        install_path: where the installed skill lives. Defaults to the
            repo root discovered relative to this file.

    Returns:
        Multi-line message ready to print. Calling code decides where to
        route it (stdout, log, agent surface).
    """
    if install_path is None:
        install_path = Path(__file__).resolve().parent.parent

    current = get_current_version()
    latest = fetch_latest_version()
    if latest is None:
        return compose_check_failed(current)

    if not _is_newer(latest, current):
        return compose_up_to_date(current)

    runtime = detect_runtime(install_path)
    backup = backup_env(install_path)
    return compose_message(current, latest, runtime, backup)


def check_and_emit(install_path: Optional[Path] = None) -> int:
    """CLI-style entry — prints the message and returns an exit code.

    Exit codes:
        0 — on current or check failed (non-error)
        2 — update available (distinct so hooks/callers can detect)
    """
    current = get_current_version()
    latest = fetch_latest_version()
    if latest is None:
        _say(compose_check_failed(current))
        return 0
    if not _is_newer(latest, current):
        _say(compose_up_to_date(current))
        return 0

    if install_path is None:
        install_path = Path(__file__).resolve().parent.parent
    runtime = detect_runtime(install_path)
    backup = backup_env(install_path)
    _say(compose_message(current, latest, runtime, backup))
    return 2


if __name__ == "__main__":
    sys.exit(check_and_emit())
