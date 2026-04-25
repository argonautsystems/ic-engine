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
InvestorClaw Harness V13 Enterprise CI/CD

Rewritten in the stricter V6/V7/V8 style:
- mandatory pre-flight cleanup of sessions and artifacts
- static contract gate before heavier execution
- watchdog-enforced CI/CD phases
- aggressive repo-local validation meant to break logic drift early
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from harness.command_matrix import COMMAND_MATRIX, get_command
from harness.contract_check import run_contract_check

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("investorclaw.harness.v13")

SESSION_HARNESS = "ic-harness-v13"
SESSION_REVIEWER = "ic-reviewer-v13"
REPORTS_DIR = PROJECT_ROOT / ".harness" / "reports"


class AlertLevel(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class BlockerType(str, Enum):
    NONE = "none"
    CONTRACT_DRIFT_BLOCKER = "contract_drift_blocker"
    ENVIRONMENTAL = "environmental"
    SKILL_CODE_DEFECT = "skill_code_defect"
    INSTALL_BLOCKER = "install_blocker"
    PROVIDER_DEGRADATION_FAILURE = "provider_degradation_failure"
    SESSION_IDENTITY_VIOLATION = "session_identity_violation"
    WATCHDOG_TIMEOUT = "watchdog_timeout"
    UNKNOWN = "unknown"


@dataclass
class WatchdogEvent:
    phase_id: str
    rule: str
    alert_level: AlertLevel
    detail: str
    matched_text: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class CleanupAction:
    target: str
    action: str
    outcome: str
    detail: str | None = None


@dataclass
class PhaseResult:
    phase_id: str
    phase_name: str
    command: str
    status: ExecutionStatus
    exit_code: int | None
    elapsed_seconds: float
    alert_level: AlertLevel
    blocker_type: BlockerType = BlockerType.NONE
    stdout_tail: str = ""
    stderr_tail: str = ""
    watchdog_events: list[WatchdogEvent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class HarnessReport:
    schema_version: str
    harness_version: str
    run_id: str
    started_at: str
    finished_at: str
    repository_root: str
    session_ids: dict[str, str]
    overall_status: ExecutionStatus
    alert_level: AlertLevel
    cleanup_actions: list[CleanupAction]
    contract_gate: dict[str, Any]
    phases: list[PhaseResult]
    watchdog_summary: list[WatchdogEvent]


@dataclass(frozen=True)
class PhaseSpec:
    phase_id: str
    phase_name: str
    command: list[str]
    timeout_seconds: int
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    allow_failure: bool = False
    blocker_type: BlockerType = BlockerType.SKILL_CODE_DEFECT


@dataclass(frozen=True)
class BarrageInvocation:
    label: str
    command: list[str]
    timeout_seconds: int
    expect_success: bool
    expect_ic_result: bool
    expected_exit_codes: tuple[int, ...] = (0,)


@dataclass
class BarrageRun:
    label: str
    command: str
    exit_code: int | None
    elapsed_seconds: float
    ic_result_seen: bool
    ic_result_exit_code: int | None
    status: ExecutionStatus
    notes: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    transcript_path: str = ""


WATCHDOG_RULES = {
    "Traceback (most recent call last)": (
        "TRACEBACK",
        AlertLevel.FAIL,
        BlockerType.SKILL_CODE_DEFECT,
    ),
    "AssertionError": ("ASSERTION", AlertLevel.FAIL, BlockerType.SKILL_CODE_DEFECT),
    "name 'context' is not defined": (
        "PHASE4B_CONTEXT_REGRESSION",
        AlertLevel.FAIL,
        BlockerType.SKILL_CODE_DEFECT,
    ),
    "ENOLOCK": ("NPM_LOCKFILE_BLOCKER", AlertLevel.FAIL, BlockerType.INSTALL_BLOCKER),
    "No module named": ("IMPORT_BREAKAGE", AlertLevel.FAIL, BlockerType.SKILL_CODE_DEFECT),
    "429": ("RATE_LIMIT_SIGNAL", AlertLevel.WARN, BlockerType.PROVIDER_DEGRADATION_FAILURE),
    "Permission denied": ("PERMISSION_ENVIRONMENT", AlertLevel.WARN, BlockerType.ENVIRONMENTAL),
}


def _tail(text: str, *, lines: int = 30) -> str:
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dict__"):
        return asdict(value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


class InvestorClawEnterpriseHarness:
    def __init__(self, *, fast: bool = False, json_out: Path | None = None) -> None:
        self.fast = fast
        self.json_out = json_out
        self.run_id = f"IC-RUN-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self.cleanup_actions: list[CleanupAction] = []
        self.phase_results: list[PhaseResult] = []
        self.watchdog_events: list[WatchdogEvent] = []
        self.started_at = datetime.now(timezone.utc)
        self.harness_workspace = PROJECT_ROOT / ".harness" / self.run_id
        self.barrage_portfolio_dir = self.harness_workspace / "portfolios"
        self.barrage_reports_dir = self.harness_workspace / "reports"
        self.transcript_dir = self.harness_workspace / "transcripts"

    def run(self) -> int:
        LOGGER.info("INVESTORCLAW HARNESS V13 ENTERPRISE | run_id=%s", self.run_id)
        self._ensure_dirs()
        self._preflight_cleanup()
        contract_report = run_contract_check()
        if contract_report.status != "pass":
            LOGGER.error("Static contract gate failed; blocking downstream phases")
            self.phase_results.append(
                PhaseResult(
                    phase_id="T0",
                    phase_name="Static Contract Gate",
                    command="python3 harness/contract_check.py --json",
                    status=ExecutionStatus.BLOCKED,
                    exit_code=1,
                    elapsed_seconds=0.0,
                    alert_level=AlertLevel.FAIL,
                    blocker_type=BlockerType.CONTRACT_DRIFT_BLOCKER,
                    stdout_tail=_tail(json.dumps(asdict(contract_report), indent=2)),
                    notes=["CONTRACT_DRIFT_BLOCKER blocks all other tiers"],
                )
            )
            return self._finalize(contract_report=asdict(contract_report))

        self.phase_results.append(
            PhaseResult(
                phase_id="T0",
                phase_name="Static Contract Gate",
                command="python3 harness/contract_check.py --json",
                status=ExecutionStatus.SUCCESS,
                exit_code=0,
                elapsed_seconds=0.0,
                alert_level=AlertLevel.PASS,
                stdout_tail=_tail(json.dumps(asdict(contract_report), indent=2)),
                notes=["Contract preservation intact; proceeding to destructive CI/CD phases"],
            )
        )

        for spec in self._phase_specs():
            result = self._run_phase(spec)
            self.phase_results.append(result)
            if result.alert_level == AlertLevel.FAIL and not spec.allow_failure:
                LOGGER.error("Stopping after hard failure in %s", spec.phase_id)
                break

        if not any(phase.alert_level == AlertLevel.FAIL for phase in self.phase_results):
            for phase in self._run_barrage_phases():
                self.phase_results.append(phase)
                if phase.alert_level == AlertLevel.FAIL:
                    LOGGER.error("Stopping after barrage failure in %s", phase.phase_id)
                    break

        return self._finalize(contract_report=asdict(contract_report))

    def _ensure_dirs(self) -> None:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    def _record_cleanup(
        self, target: str, action: str, outcome: str, detail: str | None = None
    ) -> None:
        self.cleanup_actions.append(
            CleanupAction(target=target, action=action, outcome=outcome, detail=detail)
        )

    def _rm_path(self, path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            self._record_cleanup(str(path), "rm -rf", "removed")
        elif path.exists():
            path.unlink(missing_ok=True)
            self._record_cleanup(str(path), "unlink", "removed")
        else:
            self._record_cleanup(str(path), "cleanup", "not_present")

    def _run_optional_shell(self, target: str, command: list[str]) -> None:
        if shutil.which(command[0]) is None:
            self._record_cleanup(target, " ".join(command), "skipped", "binary unavailable")
            return
        try:
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            outcome = "ok" if completed.returncode == 0 else "nonzero"
            detail = _tail(f"{completed.stdout}\n{completed.stderr}")
            self._record_cleanup(target, " ".join(command), outcome, detail)
        except subprocess.TimeoutExpired:
            self._record_cleanup(target, " ".join(command), "timeout")

    def _preflight_cleanup(self) -> None:
        LOGGER.info("W0 PRE-FLIGHT CLEANUP | clearing harness artifacts and stale session state")
        self._rm_path(self.harness_workspace)
        self._rm_path(PROJECT_ROOT / ".harness" / "recordings")
        self._rm_path(PROJECT_ROOT / ".pytest_cache")
        for pattern in ("harness_v13_*", "investorclaw-v13-*", "ic-harness-v13*"):
            for path in Path("/tmp").glob(pattern):
                self._rm_path(path)
        zeroclaw_sessions = Path.home() / ".zeroclaw" / "sessions"
        for path in (
            zeroclaw_sessions.glob("ic-harness-v13*.json") if zeroclaw_sessions.exists() else []
        ):
            self._rm_path(path)

        self._run_optional_shell(
            "openclaw sessions cleanup",
            ["openclaw", "sessions", "cleanup", "--enforce"],
        )
        self._run_optional_shell(
            "openclaw session reset harness",
            ["openclaw", "agent", "--session-id", SESSION_HARNESS, "-m", "/new"],
        )
        self._run_optional_shell(
            "openclaw session reset reviewer",
            ["openclaw", "agent", "--session-id", SESSION_REVIEWER, "-m", "/new"],
        )

    def _phase_specs(self) -> list[PhaseSpec]:
        shared_env = {
            "PYTHONPYCACHEPREFIX": f"/tmp/{self.run_id}-pycache",
        }
        specs = [
            PhaseSpec(
                phase_id="T1",
                phase_name="Release Contract Batteries",
                command=[
                    "python3",
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_claude_plugin_contracts.py",
                    "tests/test_command_contracts.py",
                    "tests/test_data_quality.py",
                    "tests/test_syntax.py",
                ],
                timeout_seconds=300 if self.fast else 900,
                cwd=PROJECT_ROOT,
                env=shared_env,
            ),
            PhaseSpec(
                phase_id="T2",
                phase_name="Full Pytest Suite",
                command=["python3", "-m", "pytest", "-q"],
                timeout_seconds=420 if self.fast else 1800,
                cwd=PROJECT_ROOT,
                env=shared_env,
            ),
            PhaseSpec(
                phase_id="T3",
                phase_name="Pipeline Integration Kill Shot",
                command=["python3", "test_pipeline_integration.py"],
                timeout_seconds=300 if self.fast else 1200,
                cwd=PROJECT_ROOT,
                env=shared_env,
                blocker_type=BlockerType.SKILL_CODE_DEFECT,
            ),
            PhaseSpec(
                phase_id="T4",
                phase_name="Harness-Orchestrator Regression",
                # The three harness/test_*.py files are CLI drivers
                # (argparse + __main__) named test_* for historical
                # reasons, not pytest tests — their functions take
                # driver args, not pytest fixtures. Running pytest on
                # them collects the async drivers and fails for lack
                # of pytest-asyncio. The intent of this phase is to
                # catch regressions in the harness orchestrator
                # modules; an import-smoke check validates syntax,
                # import-graph integrity, and module-level state
                # without executing the drivers.
                command=[
                    sys.executable,
                    "-c",
                    "import harness.test_tier_execution; "
                    "import harness.test_device_matrix; "
                    "import harness.test_zeroclaw_orchestrator; "
                    "print('harness driver modules import clean')",
                ],
                timeout_seconds=60 if self.fast else 180,
                cwd=PROJECT_ROOT,
                env=shared_env,
            ),
        ]

        if shutil.which("npm"):
            specs.extend(
                [
                    PhaseSpec(
                        phase_id="T5",
                        phase_name="Node Reproducibility Gate",
                        command=["npm", "ci", "--ignore-scripts"],
                        timeout_seconds=300 if self.fast else 900,
                        cwd=PROJECT_ROOT,
                        blocker_type=BlockerType.INSTALL_BLOCKER,
                    ),
                    PhaseSpec(
                        phase_id="T6",
                        phase_name="TypeScript Build Gate",
                        command=["npm", "run", "build"],
                        timeout_seconds=180 if self.fast else 600,
                        cwd=PROJECT_ROOT,
                    ),
                    PhaseSpec(
                        phase_id="T7",
                        phase_name="TypeScript Typecheck Gate",
                        command=["npm", "run", "typecheck"],
                        timeout_seconds=180 if self.fast else 600,
                        cwd=PROJECT_ROOT,
                    ),
                    PhaseSpec(
                        phase_id="T8",
                        phase_name="Security Audit Gate",
                        command=["npm", "audit", "--audit-level=critical"],
                        timeout_seconds=180 if self.fast else 600,
                        cwd=PROJECT_ROOT,
                        blocker_type=BlockerType.INSTALL_BLOCKER,
                    ),
                ]
            )
        else:
            specs.append(
                PhaseSpec(
                    phase_id="T5",
                    phase_name="Node Reproducibility Gate",
                    command=["bash", "-lc", "echo 'npm unavailable in environment'"],
                    timeout_seconds=10,
                    cwd=PROJECT_ROOT,
                    allow_failure=True,
                    blocker_type=BlockerType.ENVIRONMENTAL,
                )
            )

        specs.extend(
            [
                PhaseSpec(
                    phase_id="T9",
                    phase_name="Git Diff Hygiene",
                    command=["git", "diff", "--check"],
                    timeout_seconds=60,
                    cwd=PROJECT_ROOT,
                    blocker_type=BlockerType.SKILL_CODE_DEFECT,
                ),
                PhaseSpec(
                    phase_id="T10",
                    phase_name="Dist Sync Gate",
                    command=["git", "diff", "--exit-code", "dist/"],
                    timeout_seconds=60,
                    cwd=PROJECT_ROOT,
                    blocker_type=BlockerType.SKILL_CODE_DEFECT,
                    allow_failure=True,
                ),
            ]
        )
        return specs

    def _prepare_barrage_workspace(self) -> dict[str, str]:
        self.barrage_portfolio_dir.mkdir(parents=True, exist_ok=True)
        self.barrage_reports_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        portfolio_csv = self.barrage_portfolio_dir / "master_portfolio.csv"
        portfolio_csv.write_text(
            "\n".join(
                [
                    "SYMBOL,QUANTITY,PRICE,ASSET TYPE,PURCHASE PRICE",
                    "AAPL,100,189.25,equity,172.10",
                    "MSFT,80,413.55,equity,390.10",
                    "BND,250,71.02,etf,69.44",
                    "TLT,40,92.85,etf,95.11",
                    "JNJ,55,151.33,equity,148.02",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        env = {
            "INVESTOR_CLAW_PORTFOLIO_DIR": str(self.barrage_portfolio_dir),
            "INVESTORCLAW_PORTFOLIO_DIR": str(self.barrage_portfolio_dir),
            "INVESTOR_CLAW_REPORTS_DIR": str(self.barrage_reports_dir),
            "INVESTOR_CLAW_DATED_REPORTS": "false",
            "INVESTORCLAW_AUTO_SESSION": "true",
            # update-identity returns exit 2 in non-interactive sessions
            # without auto-consent; the harness must NOT mutate IDENTITY.md
            # from a CI run, so silence the skip with the explicit opt-out.
            "INVESTORCLAW_SKIP_IDENTITY_UPDATE": "1",
            "PYTHONPYCACHEPREFIX": f"/tmp/{self.run_id}-pycache-barrage",
        }
        return env

    def _run_barrage_phases(self) -> list[PhaseResult]:
        env = self._prepare_barrage_workspace()
        phases = [
            self._run_barrage_phase(
                phase_id="T11",
                phase_name="Command Barrage Canonical Surface",
                invocations=self._canonical_invocations(),
                env=env,
                default_blocker=BlockerType.SKILL_CODE_DEFECT,
            ),
            self._run_barrage_phase(
                phase_id="T12",
                phase_name="Command Barrage Repeat Pressure",
                invocations=self._repeat_pressure_invocations(),
                env=env,
                default_blocker=BlockerType.SKILL_CODE_DEFECT,
            ),
            self._run_barrage_phase(
                phase_id="T13",
                phase_name="Command Barrage Hostile Inputs",
                invocations=self._hostile_invocations(),
                env=env,
                default_blocker=BlockerType.SKILL_CODE_DEFECT,
            ),
            self._run_concurrent_barrage_phase(
                phase_id="T14",
                phase_name="Command Barrage Concurrent Overlap",
                invocations=self._concurrent_invocations(),
                env=env,
                default_blocker=BlockerType.SKILL_CODE_DEFECT,
            ),
            self._run_poison_barrage_phase(
                phase_id="T15",
                phase_name="Command Barrage Stale Artifact Poison",
                env=env,
                default_blocker=BlockerType.SKILL_CODE_DEFECT,
            ),
        ]
        return phases

    def _canonical_invocations(self) -> list[BarrageInvocation]:
        python = sys.executable
        commands = []
        for command_name in sorted(COMMAND_MATRIX):
            cfg = get_command(command_name)
            commands.append(
                BarrageInvocation(
                    label=f"canonical:{command_name}",
                    command=[python, "investorclaw.py", command_name],
                    timeout_seconds=max(20, cfg.timeout_seconds * (2 if self.fast else 4)),
                    expect_success=True,
                    expect_ic_result=True,
                    expected_exit_codes=(0,),
                )
            )
        return commands

    def _repeat_pressure_invocations(self) -> list[BarrageInvocation]:
        python = sys.executable
        critical = [
            "setup",
            "holdings",
            "session",
            "performance",
            "bonds",
            "analyst",
            "news",
            "synthesize",
            "optimize",
            "run",
            "report",
        ]
        invocations: list[BarrageInvocation] = []
        for wave in range(1, 3 if self.fast else 4):
            for command_name in critical:
                cfg = get_command(command_name)
                invocations.append(
                    BarrageInvocation(
                        label=f"repeat{wave}:{command_name}",
                        command=[python, "investorclaw.py", command_name],
                        timeout_seconds=max(20, cfg.timeout_seconds * 3),
                        expect_success=True,
                        expect_ic_result=True,
                        expected_exit_codes=(0,),
                    )
                )
        return invocations

    def _hostile_invocations(self) -> list[BarrageInvocation]:
        python = sys.executable
        return [
            BarrageInvocation(
                label="hostile:unknown-command",
                command=[python, "investorclaw.py", "totally-invalid-command"],
                timeout_seconds=15,
                expect_success=False,
                expect_ic_result=False,
                expected_exit_codes=(1,),
            ),
            BarrageInvocation(
                label="hostile:lookup-bad-file",
                command=[
                    python,
                    "investorclaw.py",
                    "lookup",
                    "--file",
                    "definitely_missing",
                    "--symbol",
                    "AAPL",
                ],
                timeout_seconds=20,
                expect_success=False,
                expect_ic_result=True,
                expected_exit_codes=(1, 2),
            ),
            BarrageInvocation(
                label="hostile:optimize-bad-method",
                command=[python, "investorclaw.py", "optimize", "definitely-not-a-method"],
                timeout_seconds=20,
                expect_success=False,
                expect_ic_result=True,
                expected_exit_codes=(1, 2),
            ),
            BarrageInvocation(
                label="hostile:portfolio-switch-missing",
                command=[python, "investorclaw.py", "portfolio-switch", "nonexistent-portfolio"],
                timeout_seconds=20,
                expect_success=False,
                expect_ic_result=True,
                expected_exit_codes=(1, 2),
            ),
            BarrageInvocation(
                label="hostile:scenario-bad-arg",
                command=[python, "investorclaw.py", "scenario", "--shock", "NaN"],
                timeout_seconds=20,
                expect_success=False,
                expect_ic_result=True,
                expected_exit_codes=(1, 2),
            ),
        ]

    def _concurrent_invocations(self) -> list[BarrageInvocation]:
        python = sys.executable
        labels = ["holdings", "performance", "news", "analyst", "synthesize", "report"]
        invocations: list[BarrageInvocation] = []
        for command_name in labels:
            cfg = get_command(command_name)
            invocations.append(
                BarrageInvocation(
                    label=f"concurrent:{command_name}",
                    command=[python, "investorclaw.py", command_name],
                    timeout_seconds=max(25, cfg.timeout_seconds * 4),
                    expect_success=True,
                    expect_ic_result=True,
                    expected_exit_codes=(0,),
                )
            )
        return invocations

    def _sanitize_label(self, label: str) -> str:
        return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label)

    def _write_transcript(
        self,
        *,
        label: str,
        command: list[str],
        stdout: str,
        stderr: str,
        exit_code: int | None,
        elapsed_seconds: float,
        notes: list[str],
    ) -> str:
        payload = {
            "label": label,
            "command": command,
            "exit_code": exit_code,
            "elapsed_seconds": elapsed_seconds,
            "notes": notes,
            "stdout": stdout,
            "stderr": stderr,
        }
        path = self.transcript_dir / f"{self._sanitize_label(label)}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return str(path)

    def _hash_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _snapshot_artifacts(self) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for artifact in sorted(self.barrage_reports_dir.rglob("*")):
            if artifact.is_file():
                rel = str(artifact.relative_to(self.barrage_reports_dir))
                snapshot[rel] = {
                    "size": artifact.stat().st_size,
                    "sha256": self._hash_file(artifact),
                }
        return snapshot

    def _fabrication_events(
        self,
        *,
        phase_id: str,
        label: str,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        ic_result: dict[str, Any] | None,
    ) -> list[WatchdogEvent]:
        text = f"{stdout}\n{stderr}".lower()
        phrases = {
            "i'm sorry": "APOLOGY_SIGNAL",
            "i apologize": "APOLOGY_SIGNAL",
            "unable to complete": "UNVERIFIED_FAILURE_NARRATION",
            "example output": "FAKE_EXAMPLE_OUTPUT",
            "simulated": "SIMULATION_SIGNAL",
            "placeholder": "PLACEHOLDER_SIGNAL",
            "mock response": "MOCK_SIGNAL",
        }
        events: list[WatchdogEvent] = []
        for needle, rule in phrases.items():
            if needle in text:
                severity = (
                    AlertLevel.FAIL if exit_code == 0 or ic_result is None else AlertLevel.WARN
                )
                events.append(
                    WatchdogEvent(
                        phase_id=phase_id,
                        rule=rule,
                        alert_level=severity,
                        detail=f"{label} emitted suspicious phrase '{needle}'",
                        matched_text=needle,
                    )
                )
        return events

    def _extract_ic_result(self, stdout: str) -> dict[str, Any] | None:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "ic_result" in payload:
                return payload["ic_result"]
        return None

    def _artifact_events(self, phase_id: str) -> list[WatchdogEvent]:
        events: list[WatchdogEvent] = []
        for artifact in self.barrage_reports_dir.rglob("*"):
            if artifact.is_file() and artifact.stat().st_size == 0:
                events.append(
                    WatchdogEvent(
                        phase_id=phase_id,
                        rule="ZERO_BYTE_ARTIFACT",
                        alert_level=AlertLevel.FAIL,
                        detail=f"Artifact is zero bytes: {artifact.relative_to(self.barrage_reports_dir)}",
                    )
                )
        return events

    def _run_barrage_phase(
        self,
        *,
        phase_id: str,
        phase_name: str,
        invocations: list[BarrageInvocation],
        env: dict[str, str],
        default_blocker: BlockerType,
    ) -> PhaseResult:
        LOGGER.info("%s %s | %d invocations", phase_id, phase_name, len(invocations))
        started = time.monotonic()
        runs: list[BarrageRun] = []
        phase_events: list[WatchdogEvent] = []
        alert = AlertLevel.PASS
        blocker = BlockerType.NONE
        baseline_snapshot = self._snapshot_artifacts()

        for invocation in invocations:
            run_started = time.monotonic()
            try:
                proc = subprocess.run(
                    invocation.command,
                    cwd=PROJECT_ROOT,
                    env={**os.environ, **env},
                    capture_output=True,
                    text=True,
                    timeout=invocation.timeout_seconds,
                    check=False,
                )
                elapsed = time.monotonic() - run_started
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
                exit_code = proc.returncode
            except subprocess.TimeoutExpired as exc:
                elapsed = time.monotonic() - run_started
                stdout = exc.stdout or ""
                stderr = exc.stderr or ""
                exit_code = None
                proc = None
                phase_events.append(
                    WatchdogEvent(
                        phase_id=phase_id,
                        rule="WATCHDOG_TIMEOUT",
                        alert_level=AlertLevel.FAIL,
                        detail=f"{invocation.label} exceeded {invocation.timeout_seconds}s",
                    )
                )

            ic_result = self._extract_ic_result(stdout)
            notes: list[str] = []
            status = ExecutionStatus.SUCCESS

            if proc is None:
                notes.append("Invocation timed out under watchdog")
                status = ExecutionStatus.FAILURE

            fabrication_events = self._fabrication_events(
                phase_id=phase_id,
                label=invocation.label,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                ic_result=ic_result,
            )
            phase_events.extend(fabrication_events)
            if any(event.alert_level == AlertLevel.FAIL for event in fabrication_events):
                notes.append("Suspicious fake-success or apology language detected")
                status = ExecutionStatus.FAILURE

            if invocation.expect_ic_result and not ic_result:
                notes.append("Missing ic_result envelope")
                status = ExecutionStatus.FAILURE
                phase_events.append(
                    WatchdogEvent(
                        phase_id=phase_id,
                        rule="IC_RESULT_MISSING",
                        alert_level=AlertLevel.FAIL,
                        detail=f"{invocation.label} completed without ic_result envelope",
                    )
                )

            if ic_result and exit_code != ic_result.get("exit_code"):
                notes.append("Process exit code diverged from ic_result.exit_code")
                status = ExecutionStatus.FAILURE
                phase_events.append(
                    WatchdogEvent(
                        phase_id=phase_id,
                        rule="IC_RESULT_EXIT_MISMATCH",
                        alert_level=AlertLevel.FAIL,
                        detail=f"{invocation.label} returncode={exit_code} ic_result={ic_result.get('exit_code')}",
                    )
                )

            if invocation.expect_success:
                if exit_code not in invocation.expected_exit_codes:
                    notes.append(f"Unexpected non-zero exit: {exit_code}")
                    status = ExecutionStatus.FAILURE
            else:
                if exit_code in invocation.expected_exit_codes:
                    status = ExecutionStatus.SUCCESS
                else:
                    notes.append(f"Hostile case escaped expected failure contract: {exit_code}")
                    status = ExecutionStatus.FAILURE

            watchdog_events, mapped_blocker, mapped_alert = self._evaluate_watchdogs(
                phase_id,
                stdout,
                stderr,
                exit_code,
                default_blocker,
            )
            phase_events.extend(watchdog_events)
            if status == ExecutionStatus.FAILURE or mapped_alert == AlertLevel.FAIL:
                alert = AlertLevel.FAIL
                blocker = mapped_blocker if mapped_blocker != BlockerType.NONE else default_blocker
            elif mapped_alert == AlertLevel.WARN and alert != AlertLevel.FAIL:
                alert = AlertLevel.WARN
                if blocker == BlockerType.NONE:
                    blocker = mapped_blocker

            runs.append(
                BarrageRun(
                    label=invocation.label,
                    command=" ".join(invocation.command),
                    exit_code=exit_code,
                    elapsed_seconds=elapsed,
                    ic_result_seen=ic_result is not None,
                    ic_result_exit_code=ic_result.get("exit_code") if ic_result else None,
                    status=status,
                    notes=notes,
                    stdout_tail=_tail(stdout, lines=12),
                    stderr_tail=_tail(stderr, lines=12),
                    transcript_path=self._write_transcript(
                        label=invocation.label,
                        command=invocation.command,
                        stdout=stdout,
                        stderr=stderr,
                        exit_code=exit_code,
                        elapsed_seconds=elapsed,
                        notes=notes,
                    ),
                )
            )

        artifact_events = self._artifact_events(phase_id)
        phase_events.extend(artifact_events)
        if artifact_events:
            alert = AlertLevel.FAIL
            blocker = default_blocker

        self.watchdog_events.extend(phase_events)
        final_snapshot = self._snapshot_artifacts()
        failures = [run for run in runs if run.status != ExecutionStatus.SUCCESS]
        status = (
            ExecutionStatus.SUCCESS
            if not failures and alert == AlertLevel.PASS
            else (ExecutionStatus.PARTIAL if alert == AlertLevel.WARN else ExecutionStatus.FAILURE)
        )
        summary_lines = [
            f"invocations={len(runs)} failures={len(failures)}",
            f"artifacts_before={len(baseline_snapshot)} artifacts_after={len(final_snapshot)}",
            *[
                f"{run.label} exit={run.exit_code} ic_result={run.ic_result_seen} notes={' | '.join(run.notes) or 'none'}"
                for run in failures[:12]
            ],
        ]
        return PhaseResult(
            phase_id=phase_id,
            phase_name=phase_name,
            command=f"{len(invocations)} barrage invocations",
            status=status,
            exit_code=0 if not failures and alert != AlertLevel.FAIL else 1,
            elapsed_seconds=time.monotonic() - started,
            alert_level=alert,
            blocker_type=blocker,
            stdout_tail="\n".join(summary_lines),
            stderr_tail="\n".join(
                line for run in failures[:8] for line in [run.stderr_tail] if line
            ),
            watchdog_events=phase_events,
            notes=[
                f"workspace={self.harness_workspace}",
                f"portfolio_dir={self.barrage_portfolio_dir}",
                f"reports_dir={self.barrage_reports_dir}",
                f"transcripts={self.transcript_dir}",
            ],
        )

    def _run_concurrent_barrage_phase(
        self,
        *,
        phase_id: str,
        phase_name: str,
        invocations: list[BarrageInvocation],
        env: dict[str, str],
        default_blocker: BlockerType,
    ) -> PhaseResult:
        LOGGER.info("%s %s | %d concurrent invocations", phase_id, phase_name, len(invocations))
        started = time.monotonic()
        phase_events: list[WatchdogEvent] = []
        runs: list[BarrageRun] = []
        procs: list[tuple[BarrageInvocation, subprocess.Popen[str], float]] = []

        for invocation in invocations:
            proc = subprocess.Popen(
                invocation.command,
                cwd=PROJECT_ROOT,
                env={**os.environ, **env},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            procs.append((invocation, proc, time.monotonic()))

        alert = AlertLevel.PASS
        blocker = BlockerType.NONE
        for invocation, proc, proc_started in procs:
            try:
                stdout, stderr = proc.communicate(timeout=invocation.timeout_seconds)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                exit_code = None
                phase_events.append(
                    WatchdogEvent(
                        phase_id=phase_id,
                        rule="WATCHDOG_TIMEOUT",
                        alert_level=AlertLevel.FAIL,
                        detail=f"{invocation.label} exceeded {invocation.timeout_seconds}s under concurrent load",
                    )
                )
            elapsed = time.monotonic() - proc_started
            ic_result = self._extract_ic_result(stdout)
            notes: list[str] = []
            status = ExecutionStatus.SUCCESS
            if exit_code not in invocation.expected_exit_codes:
                status = ExecutionStatus.FAILURE
                notes.append(f"Unexpected concurrent exit: {exit_code}")
            if invocation.expect_ic_result and not ic_result:
                status = ExecutionStatus.FAILURE
                notes.append("Missing ic_result envelope under concurrent load")
            fabrication_events = self._fabrication_events(
                phase_id=phase_id,
                label=invocation.label,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                ic_result=ic_result,
            )
            phase_events.extend(fabrication_events)
            watchdog_events, mapped_blocker, mapped_alert = self._evaluate_watchdogs(
                phase_id,
                stdout,
                stderr,
                exit_code,
                default_blocker,
            )
            phase_events.extend(watchdog_events)
            if (
                status == ExecutionStatus.FAILURE
                or mapped_alert == AlertLevel.FAIL
                or any(e.alert_level == AlertLevel.FAIL for e in fabrication_events)
            ):
                alert = AlertLevel.FAIL
                blocker = mapped_blocker if mapped_blocker != BlockerType.NONE else default_blocker
            runs.append(
                BarrageRun(
                    label=invocation.label,
                    command=" ".join(invocation.command),
                    exit_code=exit_code,
                    elapsed_seconds=elapsed,
                    ic_result_seen=ic_result is not None,
                    ic_result_exit_code=ic_result.get("exit_code") if ic_result else None,
                    status=status,
                    notes=notes,
                    stdout_tail=_tail(stdout, lines=12),
                    stderr_tail=_tail(stderr, lines=12),
                    transcript_path=self._write_transcript(
                        label=invocation.label,
                        command=invocation.command,
                        stdout=stdout,
                        stderr=stderr,
                        exit_code=exit_code,
                        elapsed_seconds=elapsed,
                        notes=notes,
                    ),
                )
            )

        self.watchdog_events.extend(phase_events)
        failures = [run for run in runs if run.status != ExecutionStatus.SUCCESS]
        return PhaseResult(
            phase_id=phase_id,
            phase_name=phase_name,
            command=f"{len(invocations)} concurrent barrage invocations",
            status=ExecutionStatus.SUCCESS
            if not failures and alert == AlertLevel.PASS
            else ExecutionStatus.FAILURE,
            exit_code=0 if not failures and alert == AlertLevel.PASS else 1,
            elapsed_seconds=time.monotonic() - started,
            alert_level=alert,
            blocker_type=blocker,
            stdout_tail="\n".join(
                [f"invocations={len(runs)} failures={len(failures)}"]
                + [
                    f"{run.label} exit={run.exit_code} ic_result={run.ic_result_seen}"
                    for run in failures[:12]
                ]
            ),
            stderr_tail="\n".join(run.stderr_tail for run in failures[:8] if run.stderr_tail),
            watchdog_events=phase_events,
            notes=[
                f"transcripts={self.transcript_dir}",
                "parallel overlap intended to trigger race conditions",
            ],
        )

    def _run_poison_barrage_phase(
        self,
        *,
        phase_id: str,
        phase_name: str,
        env: dict[str, str],
        default_blocker: BlockerType,
    ) -> PhaseResult:
        poison_target = self.barrage_reports_dir / "holdings_summary.json"
        poison_target.write_text("POISON_STALE_ARTIFACT_DO_NOT_TRUST\n", encoding="utf-8")
        poison_hash = self._hash_file(poison_target)
        result = self._run_barrage_phase(
            phase_id=phase_id,
            phase_name=phase_name,
            invocations=[
                BarrageInvocation(
                    label="poison:holdings-refresh",
                    command=[sys.executable, "investorclaw.py", "holdings"],
                    timeout_seconds=30,
                    expect_success=True,
                    expect_ic_result=True,
                    expected_exit_codes=(0,),
                ),
                BarrageInvocation(
                    label="poison:performance-refresh",
                    command=[sys.executable, "investorclaw.py", "performance"],
                    timeout_seconds=30,
                    expect_success=True,
                    expect_ic_result=True,
                    expected_exit_codes=(0,),
                ),
            ],
            env=env,
            default_blocker=default_blocker,
        )
        if poison_target.exists() and self._hash_file(poison_target) == poison_hash:
            event = WatchdogEvent(
                phase_id=phase_id,
                rule="STALE_ARTIFACT_REUSED",
                alert_level=AlertLevel.FAIL,
                detail="Poisoned holdings_summary.json survived refresh unchanged",
            )
            result.watchdog_events.append(event)
            result.alert_level = AlertLevel.FAIL
            result.status = ExecutionStatus.FAILURE
            result.exit_code = 1
            result.blocker_type = default_blocker
            result.stdout_tail = f"{result.stdout_tail}\npoison_artifact=unchanged"
            self.watchdog_events.append(event)
        return result

    def _run_phase(self, spec: PhaseSpec) -> PhaseResult:
        LOGGER.info("%s %s | %s", spec.phase_id, spec.phase_name, " ".join(spec.command))
        env = os.environ.copy()
        env.update(spec.env)
        started = time.monotonic()

        try:
            completed = subprocess.run(
                spec.command,
                cwd=spec.cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
                check=False,
            )
            elapsed = time.monotonic() - started
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            watchdog_events, blocker_type, alert_level = self._evaluate_watchdogs(
                spec.phase_id,
                stdout,
                stderr,
                completed.returncode,
                spec.blocker_type,
            )
            status = (
                ExecutionStatus.SUCCESS if completed.returncode == 0 else ExecutionStatus.FAILURE
            )
            if completed.returncode != 0 and spec.allow_failure and alert_level != AlertLevel.FAIL:
                status = ExecutionStatus.PARTIAL
                alert_level = AlertLevel.WARN
            self.watchdog_events.extend(watchdog_events)
            return PhaseResult(
                phase_id=spec.phase_id,
                phase_name=spec.phase_name,
                command=" ".join(spec.command),
                status=status,
                exit_code=completed.returncode,
                elapsed_seconds=elapsed,
                alert_level=alert_level,
                blocker_type=blocker_type
                if completed.returncode != 0 or watchdog_events
                else BlockerType.NONE,
                stdout_tail=_tail(stdout),
                stderr_tail=_tail(stderr),
                watchdog_events=watchdog_events,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            event = WatchdogEvent(
                phase_id=spec.phase_id,
                rule="WATCHDOG_TIMEOUT",
                alert_level=AlertLevel.FAIL,
                detail=f"Phase exceeded watchdog budget of {spec.timeout_seconds}s",
            )
            self.watchdog_events.append(event)
            if exc.stdout or exc.stderr:
                matched = self._evaluate_watchdogs(
                    spec.phase_id,
                    exc.stdout or "",
                    exc.stderr or "",
                    None,
                    BlockerType.WATCHDOG_TIMEOUT,
                )[0]
            else:
                matched = []
            return PhaseResult(
                phase_id=spec.phase_id,
                phase_name=spec.phase_name,
                command=" ".join(spec.command),
                status=ExecutionStatus.BLOCKED,
                exit_code=None,
                elapsed_seconds=elapsed,
                alert_level=AlertLevel.FAIL,
                blocker_type=BlockerType.WATCHDOG_TIMEOUT,
                stdout_tail=_tail(exc.stdout or ""),
                stderr_tail=_tail(exc.stderr or ""),
                watchdog_events=[event, *matched],
                notes=["Process killed by watchdog"],
            )

    def _evaluate_watchdogs(
        self,
        phase_id: str,
        stdout: str,
        stderr: str,
        returncode: int | None,
        default_blocker: BlockerType,
    ) -> tuple[list[WatchdogEvent], BlockerType, AlertLevel]:
        # Alert level is derived from actual watchdog sentinels — returncode
        # is evaluated separately by the caller (via invocation.expected_exit_codes
        # and ExecutionStatus), so hostile invocations that correctly fail
        # with exit!=0 must not be double-penalized here.
        text = f"{stdout}\n{stderr}"
        events: list[WatchdogEvent] = []
        alert = AlertLevel.PASS
        blocker = BlockerType.NONE

        for needle, (rule, level, mapped_blocker) in WATCHDOG_RULES.items():
            if needle in text:
                event = WatchdogEvent(
                    phase_id=phase_id,
                    rule=rule,
                    alert_level=level,
                    detail=f"Matched watchdog sentinel: {needle}",
                    matched_text=needle,
                )
                events.append(event)
                if level == AlertLevel.FAIL:
                    alert = AlertLevel.FAIL
                    blocker = mapped_blocker
                elif alert != AlertLevel.FAIL:
                    alert = AlertLevel.WARN
                    if blocker == BlockerType.NONE:
                        blocker = mapped_blocker

        # Process-level failure without any matched sentinel: fall back to
        # the caller's default blocker so the provenance is preserved.
        if returncode not in (0, None) and not events:
            blocker = default_blocker or BlockerType.UNKNOWN
        return events, blocker, alert

    def _finalize(self, *, contract_report: dict[str, Any]) -> int:
        finished_at = datetime.now(timezone.utc)
        overall_status = ExecutionStatus.SUCCESS
        alert_level = AlertLevel.PASS

        for phase in self.phase_results:
            if phase.alert_level == AlertLevel.FAIL:
                overall_status = ExecutionStatus.FAILURE
                alert_level = AlertLevel.FAIL
                break
            if phase.alert_level == AlertLevel.WARN and alert_level != AlertLevel.FAIL:
                overall_status = ExecutionStatus.PARTIAL
                alert_level = AlertLevel.WARN

        report = HarnessReport(
            schema_version="INVESTORCLAW_JSON_V13_ENTERPRISE",
            harness_version="v13",
            run_id=self.run_id,
            started_at=self.started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            repository_root=str(PROJECT_ROOT),
            session_ids={"harness": SESSION_HARNESS, "reviewer": SESSION_REVIEWER},
            overall_status=overall_status,
            alert_level=alert_level,
            cleanup_actions=self.cleanup_actions,
            contract_gate=contract_report,
            phases=self.phase_results,
            watchdog_summary=self.watchdog_events,
        )

        report_path = self.json_out or REPORTS_DIR / f"{self.run_id}.json"
        report_path.write_text(
            json.dumps(asdict(report), indent=2, default=_json_default),
            encoding="utf-8",
        )

        self._print_summary(report, report_path)
        return 0 if overall_status == ExecutionStatus.SUCCESS else 1

    def _print_summary(self, report: HarnessReport, report_path: Path) -> None:
        banner = "═" * 108
        print(
            f"INVESTORCLAW TEST HARNESS V13 ENTERPRISE | {self.run_id} | Session: {SESSION_HARNESS}"
        )
        print(banner)
        print("CAPABILITIES")
        print("CAP1 ORCHESTRATION: repo-local CI/CD execution with hard watchdog timeouts")
        print("CAP2 CONTRACTS: static metadata, command-surface, and credential leak gate")
        print("CAP3 CLEANUP: pre-flight session reset and artifact purge before every run")
        print(
            "CAP4 WATCHDOG: blocker classification on tracebacks, install drift, and pipeline regressions"
        )
        print(
            "CAP5 BARRAGE: canonical command storm, repeat-pressure waves, and hostile malformed inputs"
        )
        print(banner)
        print("SUMMARY")
        print(
            f"overall_status={report.overall_status.value} alert_level={report.alert_level.value}"
        )
        print(
            f"contract_gate={report.contract_gate.get('status')} cleanup_actions={len(report.cleanup_actions)}"
        )
        for phase in report.phases:
            print(
                f"{phase.phase_id} {phase.phase_name}: {phase.status.value} "
                f"alert={phase.alert_level.value} exit={phase.exit_code}"
            )
            if phase.blocker_type != BlockerType.NONE:
                print(f"  blocker={phase.blocker_type.value}")
            for event in phase.watchdog_events:
                print(
                    f"  watchdog={event.rule} level={event.alert_level.value} detail={event.detail}"
                )
        print(f"json_report={report_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="InvestorClaw Harness V13 Enterprise CI/CD runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python3 harness/v13_typhon_enterprise_cicd.py
              python3 harness/v13_typhon_enterprise_cicd.py --fast
              python3 harness/v13_typhon_enterprise_cicd.py --json-out /tmp/ic-v13.json
            """
        ),
    )
    parser.add_argument(
        "--fast", action="store_true", help="shorten watchdog budgets for faster iteration"
    )
    parser.add_argument("--json-out", type=Path, help="override JSON report path")
    args = parser.parse_args()

    harness = InvestorClawEnterpriseHarness(fast=args.fast, json_out=args.json_out)
    return harness.run()


if __name__ == "__main__":
    raise SystemExit(main())
