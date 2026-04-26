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
InvestorClaw entry point for OpenClaw skill invocation.
Thin router: bootstraps config, resolves command, runs script.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent

# Keep import bootstrap minimal and local to the entrypoint. Subprocesses get
# their import paths from runtime/environment.py, so other modules should not
# need to mutate sys.path.
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# ---------------------------------------------------------------------------
# Version management — single source of truth (previously investorclaw/version.py)
# ---------------------------------------------------------------------------
from ic_engine import __version__ as VERSION
from ic_engine.config.help_text import show_help
from ic_engine.runtime.bootstrap import run_bootstrap
from ic_engine.runtime.environment import build_env
from ic_engine.runtime.router import (
    emit_critical_content_floor,
    resolve_script,
    should_prime_guardrails,
    synthesize_args,
)
from ic_engine.runtime.subprocess_runner import run_script
from ic_engine.setup.identity_updater import update_identity

VERSION_OVERRIDE = os.environ.get("INVESTORCLAW_VERSION")
__version__ = VERSION_OVERRIDE or VERSION


def get_version() -> str:
    """Get active version string (respects INVESTORCLAW_VERSION env var)."""
    return __version__


def get_canonical_version() -> str:
    """Get canonical version (ignores env var)."""
    return VERSION


def is_development() -> bool:
    """Check if running development version (has -dev suffix)."""
    return "dev" in __version__.lower() or "-dev" in __version__


# ---------------------------------------------------------------------------
# Guardrail priming (inlined from guardrail_primer.py — single caller)
# ---------------------------------------------------------------------------

# Models that require automatic session priming before the first query.
_PRIME_REQUIRED = {
    "xai/grok-4-1-fast-reasoning",
    "grok-4-1-fast-reasoning",
    "grok-4-1-fast",
    "xai/grok-4-1-fast",
}


def _auto_prime_guardrails(scripts_dir: Path) -> None:
    """Prime the guardrails session if the active model requires it.

    Called transparently before skill command execution.  A session marker
    prevents re-priming within the same OS session.  Non-fatal on failure.
    """
    guardrails_script = scripts_dir / "model_guardrails.py"
    if not guardrails_script.exists():
        return

    active_model = os.environ.get("OPENCLAW_MODEL", "").strip()
    if not active_model:
        try:
            cfg_path = Path.home() / ".openclaw" / "openclaw.json"
            with open(cfg_path) as fh:
                cfg = json.load(fh)
            active_model = cfg["agents"]["defaults"]["model"]["primary"]
        except Exception:
            return

    if active_model not in _PRIME_REQUIRED:
        return

    # SHA256 for consistent cryptographic practices (used only as a filename marker, not for security)
    model_hash = hashlib.sha256(active_model.encode()).hexdigest()[:8]
    marker_dir = Path.home() / ".investorclaw"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f".investorclaw_primed_{model_hash}"
    if marker.exists():
        return

    try:
        result = subprocess.run(
            [sys.executable, str(guardrails_script), "--prime", "--model", active_model],
            check=False,
            capture_output=True,
            timeout=90,
        )
        if result.returncode == 0:
            marker.touch()
    except Exception:
        pass  # Non-fatal


# SKILL_DIR is the user-data root: where `.env`, `portfolios/`, and
# adapter scaffolding live. SCRIPTS_DIR is the engine-code root: where the
# command Python scripts live. Pre-Phase-2-split these were the same dir;
# after the split they diverge and must be tracked separately:
#
#   * Self-hosted ic-engine usage (no adapter): both equal ROOT_DIR.
#   * Adapter (InvestorClaw post-v2.3.0) usage: SKILL_DIR is the adapter
#     checkout (set via INVESTORCLAW_SKILL_DIR by the shim), SCRIPTS_DIR
#     stays at ROOT_DIR/commands inside the engine package.
_SKILL_DIR_OVERRIDE = os.environ.get("INVESTORCLAW_SKILL_DIR", "").strip()
SKILL_DIR = Path(_SKILL_DIR_OVERRIDE).resolve() if _SKILL_DIR_OVERRIDE else ROOT_DIR
SCRIPTS_DIR = ROOT_DIR / "commands"

# Commands that must never trigger stonkmode narration (would recurse or be
# meaningless — stonkmode narrating itself, setup, or guardrails).
STONKMODE_EXCLUDED_COMMANDS: frozenset = frozenset(
    {
        "stonkmode",
        "stonk-mode",
        "stonks",
        "setup",
        "auto-setup",
        "init",
        "initialize",
        "guardrails",
        "guardrail",
        "guardrails-prime",
        "guardrails-status",
        "update-identity",
        "update_identity",
        "identity",
    }
)


def _emit_ic_result(script: str, exit_code: int, started: float) -> None:
    """Emit ic_result envelope for router-level outcomes (early returns, help/version, synthesis failures)."""
    duration_ms = int((time.monotonic() - started) * 1000)
    print(
        json.dumps(
            {"ic_result": {"script": script, "exit_code": exit_code, "duration_ms": duration_ms}}
        )
    )


def _extract_section_flag(argv_tail: list) -> tuple:
    """Extract the v2.2 --section flag (and value) from argv_tail.

    Accepts both `--section X` and `--section=X` forms. Returns
    (section_value_or_None, argv_tail_with_flag_removed). The flag
    is stripped before downstream synthesize_args sees the args, so
    legacy script CLI signatures stay unchanged.
    """
    section = None
    out: list = []
    skip_next = False
    for i, tok in enumerate(argv_tail):
        if skip_next:
            skip_next = False
            continue
        if tok == "--section":
            if i + 1 < len(argv_tail):
                section = argv_tail[i + 1]
                skip_next = True
            continue
        if tok.startswith("--section="):
            section = tok.split("=", 1)[1]
            continue
        out.append(tok)
    return section, out


def main() -> int:
    """Thin router: bootstrap → resolve → build args → run."""
    _t0 = time.monotonic()

    # Auto-manage virtual environment (transparent to user)
    try:
        from ic_engine.config.venv_manager import ensure_venv, is_venv_active

        if not is_venv_active():
            ensure_venv()
    except Exception:
        # If venv setup fails, log warning but continue
        # (might be running in pre-existing venv or other context)
        pass

    command = sys.argv[1].lower() if len(sys.argv) > 1 else "setup"

    if command in {"-h", "--help", "help"}:
        show_help()
        _emit_ic_result("help.py", 0, _t0)
        return 0

    if command in {"-v", "--version", "version"}:
        print(f"investorclaw {VERSION}")
        _emit_ic_result("version.py", 0, _t0)
        return 0

    if command in {"update-identity", "update_identity", "identity"}:
        exit_code = update_identity(SKILL_DIR)
        _emit_ic_result("update_identity.py", exit_code, _t0)
        return exit_code

    # Bootstrap config/env for all commands except setup/help (which run without config)
    if command not in {"setup", "help", "-h", "--help"}:
        run_bootstrap(SKILL_DIR)

    if should_prime_guardrails(command):
        _auto_prime_guardrails(SCRIPTS_DIR)

    # v2.2: extract --section flag before downstream arg synthesis sees it.
    section, user_args = _extract_section_flag(list(sys.argv[2:]))

    script_path = resolve_script(command, SCRIPTS_DIR, section=section)
    if script_path is None:
        # Unknown command OR invalid section: resolve_script already emitted
        # the appropriate envelope/stderr message.
        return 1

    args, error_code = synthesize_args(command, user_args, SKILL_DIR, section=section)
    if error_code != 0:
        # Arg synthesis failed (e.g. missing upstream data file); preserve envelope contract.
        _emit_ic_result(script_path.name, error_code, _t0)
        return error_code

    narration_command = command if command not in STONKMODE_EXCLUDED_COMMANDS else ""
    exit_code = run_script(script_path, args, build_env(SKILL_DIR), SKILL_DIR, narration_command)

    # CRITICAL_CONTENT floor — always surface educational disclaimer
    emit_critical_content_floor(command)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
