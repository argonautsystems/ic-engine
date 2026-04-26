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
Command-contract tests for runtime/router.py.

Verifies that every COMMANDS entry resolves to a script file that actually
exists on disk.  These tests act as a release gate: any Phase 4 rename or
move that breaks a router mapping will fail here before reaching production.
"""

import sys
from pathlib import Path

_skill_root = Path(__file__).parent.parent
if str(_skill_root) not in sys.path:
    sys.path.insert(0, str(_skill_root))

import pytest

import ic_engine
from ic_engine.runtime.router import (
    _DISPATCH_SENTINEL,
    COMMANDS,
    SECTION_DISPATCH,
    should_prime_guardrails,
)

# Engine-bundled command scripts live under the ic_engine package, not at
# the repo root. After Phase 2.5+ of IC_DECOMPOSITION, COMMANDS_DIR points
# at the package's commands/ rather than the legacy <repo>/commands.
COMMANDS_DIR = Path(ic_engine.__file__).resolve().parent / "commands"

# v2.2 (RFC §3.7.6): SECTION_DISPATCH may reference scripts that have not
# landed yet. Allowlist them so the existing script-existence gate doesn't
# block contract-truth + router-dispatch commits while step 3b is still in
# flight. Every entry here has a tracked task in the v2.2 implementation
# checklist; remove from the allowlist when the script is added.
_PENDING_V2_2_SCRIPTS = frozenset()


# ---------------------------------------------------------------------------
# Every COMMANDS entry must point to an existing script
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command,script", COMMANDS.items())
def test_command_script_exists(command, script):
    """Resolve each COMMANDS entry against the real commands/ directory.

    v2.2 dispatch sentinels (`__dispatch__`) are skipped here; the
    SECTION_DISPATCH map (tested separately) is the source of truth for
    which scripts those wrappers actually call.
    """
    if script == _DISPATCH_SENTINEL:
        pytest.skip(f"Command '{command}' is a v2.2 dispatch wrapper")
    resolved = (COMMANDS_DIR / script).resolve()
    assert resolved.exists(), (
        f"Command '{command}' → '{script}' resolved to {resolved} which does not exist. "
        "Update COMMANDS in runtime/router.py or restore the missing file."
    )


@pytest.mark.parametrize(
    "wrapper,section,script",
    [(w, s, sc) for w, sections in SECTION_DISPATCH.items() for s, sc in sections.items()],
)
def test_section_dispatch_script_exists(wrapper, section, script):
    """Every (wrapper, section) → script tuple must resolve to a real file,
    except scripts explicitly allowlisted as still-pending v2.2 work."""
    if script in _PENDING_V2_2_SCRIPTS:
        pytest.skip(f"Script '{script}' is pending v2.2 implementation")
    resolved = (COMMANDS_DIR / script).resolve()
    assert resolved.exists(), (
        f"SECTION_DISPATCH['{wrapper}']['{section}'] → '{script}' resolved to "
        f"{resolved} which does not exist. Update SECTION_DISPATCH or add the script."
    )


# ---------------------------------------------------------------------------
# Canonical command names present (public API surface)
# ---------------------------------------------------------------------------

CANONICAL_COMMANDS = {
    "setup",
    "holdings",
    "performance",
    "analysis",
    "bonds",
    "news",
    "analyst",
    "report",
    "lookup",
    "session",
    "guardrails",
    "consult-setup",
}


@pytest.mark.parametrize("command", sorted(CANONICAL_COMMANDS))
def test_canonical_command_registered(command):
    """Each canonical public command must appear in COMMANDS."""
    assert command in COMMANDS, f"Canonical command '{command}' is missing from COMMANDS registry."


# ---------------------------------------------------------------------------
# Alias consistency: aliases that share a script must resolve to the same file
# ---------------------------------------------------------------------------


def test_aliases_agree_on_script():
    """Aliases that should share a script must map to the same filename."""
    alias_groups = [
        ("holdings", "snapshot", "prices"),
        ("performance", "analyze", "returns"),
        ("bonds", "bond-analysis", "analyze-bonds"),
        ("analyst", "analysts", "ratings"),
        ("report", "export", "csv", "excel"),
        ("news", "sentiment"),
        ("setup", "auto-setup", "init", "initialize"),
        ("run", "pipeline"),
    ]
    for group in alias_groups:
        scripts = {COMMANDS[cmd] for cmd in group if cmd in COMMANDS}
        assert len(scripts) == 1, f"Alias group {group} maps to multiple scripts: {scripts}"


# ---------------------------------------------------------------------------
# Non-analysis guard: setup/report/lookup commands must skip priming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "setup",
        "auto-setup",
        "report",
        "export",
        "lookup",
        "guardrails",
        "session",
        "ollama-setup",
    ],
)
def test_non_analysis_commands_do_not_prime(command):
    assert should_prime_guardrails(command) is False


@pytest.mark.parametrize("command", ["holdings", "bonds", "news", "analyst", "performance"])
def test_analysis_commands_do_prime(command):
    assert should_prime_guardrails(command) is True


# ---------------------------------------------------------------------------
# No orphan scripts: every .py in commands/ is reachable via COMMANDS
# ---------------------------------------------------------------------------


def test_no_orphan_command_scripts():
    """Every public command script in commands/ must be referenced by COMMANDS.

    Internal wrappers and deployment helpers are allowed to exist without a
    direct router mapping, but they should be called out explicitly here so
    they do not silently accumulate.
    """
    allowed_unregistered = {
        "__init__.py",
        "ic_holdings_run.py",  # deployment wrapper used by SKILL.toml / Pi installs
        "_artifact_helpers.py",  # internal utility for HTML artifact generation across commands
        "portfolio_complete.py",  # internal pipeline orchestrator; run via internal/pipeline.py
    }
    registered = set(COMMANDS.values())
    for script_path in COMMANDS_DIR.glob("*.py"):
        if script_path.name in allowed_unregistered:
            continue
        assert script_path.name in registered, (
            f"commands/{script_path.name} exists but is not registered in COMMANDS. "
            "Add an entry in runtime/router.py, document it as an internal helper, or move the file."
        )
