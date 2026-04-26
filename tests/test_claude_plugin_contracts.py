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
Enforce Anthropic store submission contracts for Claude Code plugin.

Ensures that:
- No direct Python path assumptions (~/Projects/InvestorClaw, python3 investorclaw.py)
- All slash command references are namespaced (/investorclaw:ic-*, /investorclaw:investorclaw-*)
- No overly broad Bash tool permissions
- Hooks are properly registered or removed
"""

import re
from pathlib import Path

CLAUDE_DIR = Path(__file__).parent.parent / "claude"
FORBIDDEN_PATTERNS = [
    (
        r"python3\s+.*investorclaw\.py",
        "Direct python3 investorclaw.py — use installed console script",
    ),
    (r"~/Projects/InvestorClaw", "Hard-coded path ~/Projects/InvestorClaw — not portable"),
    (r"~/investorclaw/investorclaw\.py", "Hard-coded path ~/investorclaw — use installed script"),
]
BARE_SLASH_PATTERN = re.compile(r"/(?:ic-|investorclaw-)[\w-]+(?!:)")
NAMESPACED_SLASH_PATTERN = re.compile(r"/investorclaw:(?:ic-|investorclaw-)[\w-]+")


def test_no_direct_python_paths():
    """Verify no direct `python3 investorclaw.py` references in documentation."""
    for pattern_regex, reason in FORBIDDEN_PATTERNS:
        pattern = re.compile(pattern_regex)
        for md_file in CLAUDE_DIR.glob("**/*.md"):
            content = md_file.read_text(encoding="utf-8")
            matches = pattern.findall(content)
            assert not matches, (
                f"{md_file.name}: Found forbidden pattern '{pattern_regex}'\n"
                f"Reason: {reason}\n"
                f"Matches: {matches}"
            )


def test_slash_commands_are_namespaced():
    """Verify all slash commands in Claude docs are namespaced with /investorclaw:."""
    for md_file in CLAUDE_DIR.glob("**/*.md"):
        content = md_file.read_text(encoding="utf-8")

        # Find all bare slash commands
        bare_matches = BARE_SLASH_PATTERN.findall(content)

        # Allowed contexts for bare slash commands (e.g., explaining what commands are)
        # but actual slash command references should be namespaced
        for match in bare_matches:
            # Skip if this is part of a namespaced command already
            if f"/investorclaw:{match}" in content:
                continue
            # Check if it's in a code block explaining the command name itself
            # (not a user-facing reference)
            lines = content.split("\n")
            for i, line in enumerate(lines):
                if match in line:
                    match_index = line.find(match)
                    # Relative Markdown links such as ../investorclaw-setup/SKILL.md
                    # are not slash-command invocations.
                    if match_index >= 2 and line[match_index - 2 : match_index] == "..":
                        continue
                    # If it's in a code example or command table explaining the command
                    # (not "run this command"), it might be OK
                    if "Command" in line or "command" in line or "Example" in line:
                        continue
                    raise AssertionError(
                        f"{md_file.name}:{i + 1}: Found bare slash command '{match}'\n"
                        f"Store submission requires: /investorclaw:{match}\n"
                        f"Line: {line.strip()}"
                    )


def test_no_overly_broad_bash_permissions():
    """Verify allowed-tools don't have overly broad Bash permissions."""
    for skill_file in CLAUDE_DIR.glob("skills/*/SKILL.md"):
        content = skill_file.read_text(encoding="utf-8")

        # Check for overly broad patterns
        forbidden_tools = [
            "Bash(python3 -m pip install --user *)",
            "Bash(pip install *)",
            "Bash(git clone *)",
        ]

        for forbidden in forbidden_tools:
            assert forbidden not in content, (
                f"{skill_file.name}: Found overly broad Bash permission: {forbidden}\n"
                f"Use narrow, specific commands instead"
            )

        assert "Bash(python3 *)" not in content, (
            f"{skill_file.name}: Found overly broad Bash permission: Bash(python3 *)"
        )


def test_commands_do_not_execute_raw_arguments():
    """Verify command markdown never shell-executes raw slash command arguments."""
    for command_file in CLAUDE_DIR.glob("commands/*.md"):
        content = command_file.read_text(encoding="utf-8")
        assert "```!" not in content, (
            f"{command_file.name}: executable shell blocks are not allowed"
        )
        assert "$ARGUMENTS" not in content, (
            f"{command_file.name}: raw $ARGUMENTS is not allowed; validate argv first"
        )


def test_no_obsolete_hook_directory():
    """Verify hooks directory doesn't exist with outdated lifecycle hooks."""
    hooks_dir = CLAUDE_DIR / "hooks"
    assert not hooks_dir.exists(), (
        "hooks/ directory should not exist with unregistered lifecycle hooks.\n"
        "Either register with hooks.json or remove entirely."
    )


def test_install_investorclaw_helper_exists():
    """Verify the narrow install-investorclaw helper script exists."""
    helper = CLAUDE_DIR / "bin" / "install-investorclaw"
    assert helper.exists(), f"Missing helper script: {helper}"
    assert helper.stat().st_mode & 0o111, f"Helper not executable: {helper}"

    content = helper.read_text(encoding="utf-8")
    assert "git+https://gitlab.com/perlowja/InvestorClaw.git@main" in content, (
        "Helper should install from the canonical GitLab pip+git source, not arbitrary pip commands"
    )


def test_setup_skill_uses_narrow_tools():
    """Verify investorclaw-setup skill uses narrow Bash permissions."""
    setup_skill = CLAUDE_DIR / "skills" / "investorclaw-setup" / "SKILL.md"
    assert setup_skill.exists(), f"Missing skill: {setup_skill}"

    content = setup_skill.read_text(encoding="utf-8")
    # New orchestrator-based setup uses setup-orchestrator script
    assert "Bash(setup-orchestrator)" in content, (
        "Setup skill should allow Bash(setup-orchestrator) for automated setup"
    )
    assert "Bash(investorclaw *)" in content, (
        "Setup skill should allow Bash(investorclaw *) for command invocation"
    )
    # Verify it doesn't have the old broad pip permission
    assert "Bash(pip install" not in content and "Bash(python3 -m pip install" not in content, (
        "Setup skill should not have broad pip permissions"
    )


def test_claude_install_docs_use_marketplace_flow():
    """Verify Claude-facing install docs use the marketplace flow.

    Note: CLAUDE.md is intentionally omitted — it is a gitignored per-checkout
    internal dev aid, not a user-facing install document. User-facing install
    guidance lives in README.md / QUICKSTART.md / INSTALL.md / claude/.
    """
    docs = [
        Path(__file__).parent.parent / "README.md",
        Path(__file__).parent.parent / "QUICKSTART.md",
        Path(__file__).parent.parent / "INSTALL.md",
        CLAUDE_DIR / "README.md",
        CLAUDE_DIR / "INSTALL_FLOW.md",
        Path(__file__).parent.parent / "docs" / "PLATFORM_COMPARISON.md",
    ]

    for doc in docs:
        content = doc.read_text(encoding="utf-8")
        assert "/plugin marketplace add https://gitlab.com/perlowja/InvestorClaw.git" in content, (
            f"{doc.name}: Claude Code install docs must add the InvestorClaw marketplace"
        )
        assert "/plugin install investorclaw@investorclaw" in content, (
            f"{doc.name}: Claude Code install docs must install from the InvestorClaw marketplace"
        )
        assert 'Click "Install from URL"' not in content, (
            f"{doc.name}: use documented /plugin marketplace commands, not UI-only wording"
        )


def test_claude_docs_do_not_prescribe_manual_python_install():
    """Verify Claude-facing docs do not tell users to pip-install the plugin."""
    docs = [
        CLAUDE_DIR / "README.md",
        CLAUDE_DIR / "INSTALL_FLOW.md",
        CLAUDE_DIR / "skills" / "investorclaw-setup" / "SKILL.md",
    ]

    forbidden = [
        "pip install -r requirements.txt",
        "python3 -m pip install -r",
        "bash ~/InvestorClaw/claude/bin/setup-orchestrator",
        "source ~/InvestorClaw/.venv/bin/activate",
    ]
    for doc in docs:
        content = doc.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern not in content, (
                f"{doc.name}: Claude Code docs must not prescribe manual install path {pattern!r}"
            )


if __name__ == "__main__":
    test_no_direct_python_paths()
    test_slash_commands_are_namespaced()
    test_no_overly_broad_bash_permissions()
    test_no_obsolete_hook_directory()
    test_install_investorclaw_helper_exists()
    test_setup_skill_uses_narrow_tools()
    print("✓ All Claude plugin store submission contracts passed")
