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
Static contract gate for the InvestorClaw harness.

This is intentionally harsh: it fails fast on metadata drift, leaked local paths,
credential patterns, or command-surface mismatches before heavier CI/CD phases run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from harness.command_matrix import COMMAND_MATRIX

PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
PACKAGE_JSON_PATH = PROJECT_ROOT / "package.json"
PLUGIN_JSON_PATH = PROJECT_ROOT / "openclaw.plugin.json"
SKILL_TOML_PATH = PROJECT_ROOT / "SKILL.toml"
SKILL_MD_PATH = PROJECT_ROOT / "SKILL.md"
ENTRYPOINT_PATH = PROJECT_ROOT / "investorclaw.py"
CLAUDE_PLUGIN_MARKETPLACE = PROJECT_ROOT / ".claude-plugin" / "marketplace.json"

REQUIRED_PUBLIC_COMMANDS = {
    "setup",
    "holdings",
    "performance",
    "bonds",
    "analyst",
    "news",
    "news-plan",
    "synthesize",
    "fixed-income",
    "optimize",
    "report",
    "eod-report",
    "session",
    "fa-topics",
    "lookup",
    "guardrails",
    "update-identity",
    "run",
    "stonkmode",
    "check-updates",
    "ollama-setup",
    "help",
}

# v2.2 RFC §3.1 — SKILL.toml consolidated to a 9-tool surface dispatched by
# `--section`. Each tool name listed here is the bare command word the
# corresponding [[tools]] entry exposes (i.e. command="investorclaw <word>").
# This intentionally does NOT mirror REQUIRED_PUBLIC_COMMANDS (the 22-command
# CLI surface in command_matrix) — the skill surface is deliberately narrower
# than the CLI surface in v2.2.
REQUIRED_SKILL_TOOLS = {
    "view",
    "compute",
    "target",
    "scenario",
    "market",
    "bonds",
    "config",
    "eod-report",
    "lookup",
}

FORBIDDEN_SUBSTRINGS = {
    "~/Projects/InvestorClaw": "developer-local path leak",
    "/Users/jasonperlow/Projects/InvestorClaw": "developer-local absolute path leak",
    "python3 investorclaw.py": "direct python invocation instead of installed entrypoint",
}

CREDENTIAL_PATTERNS = {
    r"ghp_[A-Za-z0-9]{20,}": "GitHub personal access token",
    r"glpat-[A-Za-z0-9_\-]{20,}": "GitLab personal access token",
    r"Authorization:\s*Bearer\s+[A-Za-z0-9._\-]+": "embedded bearer token",
}

SCAN_SUFFIXES = {
    ".py",
    ".md",
    ".toml",
    ".json",
    ".yml",
    ".yaml",
    ".ts",
    ".js",
    ".txt",
}

SKIP_PATH_PARTS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".harness",
    "dist",
}

RELEASE_SURFACES = [
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "INSTALL.md",
    PROJECT_ROOT / "QUICKSTART.md",
    PROJECT_ROOT / "SKILL.md",
    PROJECT_ROOT / "SKILL.toml",
    PROJECT_ROOT / "openclaw.plugin.json",
    PROJECT_ROOT / "package.json",
    PROJECT_ROOT / "pyproject.toml",
]


@dataclass
class ContractFinding:
    code: str
    severity: str
    message: str
    path: str | None = None
    detail: str | None = None


@dataclass
class ContractReport:
    schema_version: str = "INVESTORCLAW_CONTRACT_GATE_V13"
    status: str = "pass"
    versions: dict[str, str] = field(default_factory=dict)
    findings: list[ContractFinding] = field(default_factory=list)
    checked_files: int = 0

    def add(
        self,
        code: str,
        message: str,
        *,
        severity: str = "critical",
        path: Path | None = None,
        detail: str | None = None,
    ) -> None:
        # Only critical/error severities flip the report status to fail.
        # Informational severities are recorded but don't gate the
        # pipeline. Used by `_missing_required_files` for the
        # "harness ran against a non-InvestorClaw repo" case
        # (ic-engine, clio, etc. legitimately lack package.json /
        # openclaw.plugin.json) — the InvestorClaw-specific checks
        # are skipped rather than failed.
        if severity not in {"info", "informational"}:
            self.status = "fail"
        self.findings.append(
            ContractFinding(
                code=code,
                severity=severity,
                message=message,
                path=str(path.relative_to(PROJECT_ROOT)) if path else None,
                detail=detail,
            )
        )


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _missing_required_files(report: ContractReport, paths: list[Path]) -> bool:
    """Return True if any required file is absent, after recording one finding
    per missing path. Distinguishes two cases:

    * Repo has NONE of the InvestorClaw release surfaces (i.e., it's a
      different project type — ic-engine, clio, etc.). Treat as
      informational — the InvestorClaw-specific checks don't apply.
    * Repo HAS an InvestorClaw context (at least one of the canonical
      manifests is present) but a required file has been deleted by
      mistake. That's a real regression — fail the gate.

    The presence-vector is the union of all paths in `paths`: if every
    path is missing, the repo lacks the context entirely. If some are
    present and some missing, the missing ones are real regressions.
    """
    missing = [p for p in paths if not p.exists()]
    if not missing:
        return False

    # No-context case: nothing in this list exists. Skip with informational
    # finding so harnesses against non-InvestorClaw repos (ic-engine, clio)
    # don't crash.
    if len(missing) == len(paths):
        for path in missing:
            report.add(
                "CONTEXT_MISSING",
                f"{path.name} not present; skipping InvestorClaw-specific check",
                severity="info",
                path=path,
            )
        return True

    # Partial-context case: this looks like an InvestorClaw repo with a
    # missing required surface. Real regression — fail the gate.
    for path in missing:
        report.add(
            "REQUIRED_FILE_MISSING",
            f"{path.name} required but not present in InvestorClaw-shaped repo",
            severity="error",
            path=path,
        )
    return True


def _read_version_from_entrypoint(path: Path) -> str:
    """Extract canonical VERSION literal from the entrypoint module.

    investorclaw.py declares ``VERSION = "X.Y.Z"`` as the single source
    of truth and then derives ``__version__`` via env-var override, so
    we match the literal assignment rather than ``__version__``.
    """
    pattern = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.MULTILINE)
    match = pattern.search(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"Could not locate VERSION literal in {path}")
    return match.group(1)


def _iter_scan_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in RELEASE_SURFACES:
        if path.exists():
            files.append(path)
    claude_root = root / "claude"
    if claude_root.exists():
        for path in claude_root.rglob("*.md"):
            if any(part in SKIP_PATH_PARTS for part in path.parts):
                continue
            files.append(path)
    docs_root = root / "docs"
    for name in ("PLATFORM_COMPARISON.md", "UV_TRANSPARENT_MANAGEMENT.md"):
        path = docs_root / name
        if path.exists():
            files.append(path)
    return sorted(set(files))


def _check_version_consistency(report: ContractReport) -> None:
    if _missing_required_files(
        report,
        [PYPROJECT_PATH, PACKAGE_JSON_PATH, PLUGIN_JSON_PATH, SKILL_TOML_PATH, ENTRYPOINT_PATH],
    ):
        return
    pyproject = _load_toml(PYPROJECT_PATH)
    package_json = _load_json(PACKAGE_JSON_PATH)
    plugin_json = _load_json(PLUGIN_JSON_PATH)
    skill_toml = _load_toml(SKILL_TOML_PATH)

    versions = {
        "pyproject": str(pyproject["project"]["version"]),
        "package.json": str(package_json["version"]),
        "openclaw.plugin.json": str(plugin_json["version"]),
        "SKILL.toml": str(skill_toml["skill"]["version"]),
        "investorclaw.py": _read_version_from_entrypoint(ENTRYPOINT_PATH),
    }
    report.versions = versions

    if len(set(versions.values())) != 1:
        report.add(
            "VERSION_DRIFT",
            "Version metadata is inconsistent across release surfaces",
            detail=json.dumps(versions, sort_keys=True),
        )


def _check_plugin_contract(report: ContractReport) -> None:
    if _missing_required_files(report, [PACKAGE_JSON_PATH, PLUGIN_JSON_PATH, SKILL_TOML_PATH]):
        return
    package_json = _load_json(PACKAGE_JSON_PATH)
    plugin_json = _load_json(PLUGIN_JSON_PATH)
    skill_toml = _load_toml(SKILL_TOML_PATH)

    package_extensions = package_json.get("openclaw", {}).get("extensions", [])
    plugin_extensions = plugin_json.get("openclaw", {}).get("extensions", [])
    if package_extensions != plugin_extensions:
        report.add(
            "PLUGIN_EXTENSION_DRIFT",
            "package.json and openclaw.plugin.json disagree on extension entrypoints",
            path=PLUGIN_JSON_PATH,
            detail=f"package={package_extensions} plugin={plugin_extensions}",
        )

    if "./dist/index.js" not in plugin_extensions:
        report.add(
            "PLUGIN_EXTENSION_MISSING",
            "Plugin extension contract must point at ./dist/index.js",
            path=PLUGIN_JSON_PATH,
        )

    script_target = skill_toml.get("tools", [{}])[0].get("command", "")
    if "python3 investorclaw.py" in script_target:
        report.add(
            "SKILL_ENTRYPOINT_DRIFT",
            "SKILL.toml still points at python3 investorclaw.py",
            path=SKILL_TOML_PATH,
        )

    project_scripts = _load_toml(PYPROJECT_PATH).get("project", {}).get("scripts", {})
    if "investorclaw" not in project_scripts:
        report.add(
            "CONSOLE_SCRIPT_MISSING",
            "pyproject.toml must export an investorclaw console script",
            path=PYPROJECT_PATH,
        )

    if not CLAUDE_PLUGIN_MARKETPLACE.exists():
        report.add(
            "CLAUDE_MARKETPLACE_MISSING",
            "Claude plugin marketplace manifest must be committed for submission",
            path=CLAUDE_PLUGIN_MARKETPLACE,
        )


def _check_command_surface(report: ContractReport) -> None:
    if _missing_required_files(report, [SKILL_TOML_PATH]):
        return
    skill_toml = _load_toml(SKILL_TOML_PATH)
    skill_commands = set()
    for tool in skill_toml.get("tools", []):
        command = tool.get("command", "").strip()
        if tool.get("kind") != "shell" or not command.startswith("investorclaw "):
            continue
        shell_command = command.split(" ", 1)[1]
        skill_commands.add(shell_command)
        if shell_command == "eod-report":
            skill_commands.add("report")
        if shell_command == "consult-setup":
            skill_commands.add("ollama-setup")
    matrix_commands = set(COMMAND_MATRIX)
    missing_from_matrix = REQUIRED_PUBLIC_COMMANDS - matrix_commands
    missing_from_skill = REQUIRED_SKILL_TOOLS - skill_commands

    if missing_from_matrix:
        report.add(
            "COMMAND_MATRIX_DRIFT",
            "Required commands missing from harness.command_matrix",
            path=Path("harness/command_matrix.py"),
            detail=", ".join(sorted(missing_from_matrix)),
        )

    if missing_from_skill:
        report.add(
            "SKILL_COMMAND_DRIFT",
            "Required commands missing from SKILL.toml shell tool declarations",
            path=SKILL_TOML_PATH,
            detail=", ".join(sorted(missing_from_skill)),
        )


def _check_repository_scan(report: ContractReport) -> None:
    files = _iter_scan_files(PROJECT_ROOT)
    report.checked_files = len(files)
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for needle, reason in FORBIDDEN_SUBSTRINGS.items():
            if needle in content:
                report.add(
                    "FORBIDDEN_CONTENT",
                    f"Forbidden content detected: {reason}",
                    path=path,
                    detail=needle,
                )

        for pattern, reason in CREDENTIAL_PATTERNS.items():
            if re.search(pattern, content, flags=re.IGNORECASE):
                report.add(
                    "CREDENTIAL_LEAK_PATTERN",
                    f"Credential-like material detected: {reason}",
                    path=path,
                    detail=pattern,
                )


def _check_license_headers(report: ContractReport) -> None:
    return


# v2.2 routing-rule sentinels. Both the OpenClaw-runtime SKILL.md (repo
# root) and the Claude Code plugin's SKILL.md
# (claude/skills/portfolio-analysis/SKILL.md) MUST contain these
# canonical phrases verbatim.  History: a contaminated NL-pilot run
# under the COBOL conformance test rig caught the Claude Code skill
# shipping the v2.1 rule set while the OpenClaw skill had been
# upgraded to the v2.2 FINANCE-OVERRIDE allowlist — silent divergence
# between two surfaces of the same skill.  This gate prevents that
# class of drift from shipping again.
_ROUTING_RULE_SENTINELS = (
    "FINANCE OVERRIDE (HARD RULE — ALLOWLIST)",
    "YOU MAY ONLY USE INVESTORCLAW'S DETERMINISTIC SANDBOX",
    "EXACTLY ONE acceptable answer path",
)
_ROUTING_RULE_FILES = (
    PROJECT_ROOT / "SKILL.md",
    PROJECT_ROOT / "claude" / "skills" / "portfolio-analysis" / "SKILL.md",
)


def _check_routing_rules_parity(report: ContractReport) -> None:
    """Assert the FINANCE-OVERRIDE rule appears in every SKILL.md surface.

    Both the agent-runtime SKILL.md (repo root, used by OpenClaw / Hermes
    / ZeroClaw) and the Claude Code plugin skill SKILL.md must enforce
    the same routing discipline.  Drift between the two is a shipped
    bug — a contaminated NL-pilot test caught one such drift in v2.2.1
    after the rule had landed only on the OpenClaw side.

    When NEITHER SKILL.md exists the harness is running on a repo that
    has no skill surface to police (e.g. ic-engine itself), so we record
    that as informational rather than as a contract failure. When at
    least one SKILL.md exists, real drift gating applies to all of them.
    """
    if not any(path.exists() for path in _ROUTING_RULE_FILES):
        report.add(
            "CONTEXT_MISSING",
            "no SKILL.md surfaces present; skipping InvestorClaw routing-rule check",
            severity="info",
        )
        return
    for path in _ROUTING_RULE_FILES:
        if not path.exists():
            report.add(
                "ROUTING_RULE_FILE_MISSING",
                f"Required SKILL.md surface missing: {path.relative_to(PROJECT_ROOT)}",
                path=path,
            )
            continue
        content = path.read_text(encoding="utf-8")
        missing = [s for s in _ROUTING_RULE_SENTINELS if s not in content]
        if missing:
            report.add(
                "ROUTING_RULE_DRIFT",
                "FINANCE-OVERRIDE rule missing or paraphrased — both SKILL.md "
                "surfaces must contain the canonical allowlist verbatim",
                path=path,
                detail=f"missing sentinels: {missing}",
            )


def run_contract_check() -> ContractReport:
    report = ContractReport()
    _check_version_consistency(report)
    _check_plugin_contract(report)
    _check_command_surface(report)
    _check_repository_scan(report)
    _check_license_headers(report)
    _check_routing_rules_parity(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="InvestorClaw static contract gate")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    report = run_contract_check()
    payload = {
        **asdict(report),
        "findings": [asdict(item) for item in report.findings],
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("INVESTORCLAW STATIC CONTRACT GATE V13")
        print(f"status={report.status} checked_files={report.checked_files}")
        if report.versions:
            versions = " ".join(f"{key}={value}" for key, value in sorted(report.versions.items()))
            print(f"versions {versions}")
        if report.findings:
            for finding in report.findings:
                location = f" [{finding.path}]" if finding.path else ""
                detail = f" :: {finding.detail}" if finding.detail else ""
                print(
                    f"- {finding.severity.upper()} {finding.code}{location}: {finding.message}{detail}"
                )
        else:
            print("No contract drift detected.")

    return 1 if report.status != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
