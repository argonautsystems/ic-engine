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
setup/eod_scheduler.py — End-of-Day Report Scheduler

Installs (or removes) an after-hours schedule for the InvestorClaw EOD
report so it runs automatically on weekdays after market close.

macOS  → creates a launchd plist at:
           ~/Library/LaunchAgents/com.investorclaw.eod.plist

Linux  → adds a crontab entry for the current user

Default schedule: Monday–Friday at 16:30 US/Eastern (4:30pm ET)
  — after US market close (4:00pm), before the evening news cycle.
  — yfinance and other free-tier providers have no rate-limit issues
    at this hour (no competing trading workload).

Usage:
  python3 setup/eod_scheduler.py --install
  python3 setup/eod_scheduler.py --install --time 17:00
  python3 setup/eod_scheduler.py --install --run    (pipeline + report)
  python3 setup/eod_scheduler.py --install --email-to me@example.com
  python3 setup/eod_scheduler.py --uninstall
  python3 setup/eod_scheduler.py --status

Environment notes:
  The generated plist/cron entry inherits the calling shell's PATH and
  virtualenv (if active).  If you use a venv, activate it before running
  --install so the scheduler captures the correct Python interpreter.
"""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import List

# Conservative RFC-like email pattern for scheduler arg validation.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _validate_email(addr: str) -> str:
    """Return addr if it is a plausible RFC 5322 address, else raise ValueError.

    Rejects whitespace / newlines / control characters / shell metachars so it
    is safe to embed in crontab lines and plist ProgramArguments.
    """
    s = (addr or "").strip()
    if not s:
        return s
    if any(ch in s for ch in "\r\n\t `$|;&<>\"'\\"):
        raise ValueError(f"Refusing email with control/shell metachars: {addr!r}")
    if not _EMAIL_RE.match(s):
        raise ValueError(f"Not a valid email address: {addr!r}")
    return s


_ROOT = Path(__file__).parent.parent.resolve()
_PLIST_LABEL = "com.investorclaw.eod"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"
_CRON_MARKER = "# InvestorClaw EOD report"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _python_exe() -> str:
    """Return the Python interpreter that should be used in the schedule."""
    venv = _ROOT / "venv" / "bin" / "python3"
    return str(venv) if venv.exists() else sys.executable


def _build_cmd(extra_flags: List[str]) -> List[str]:
    """Return the command list to run eod_report.py."""
    return [
        _python_exe(),
        str(_ROOT / "commands" / "eod_report.py"),
    ] + extra_flags


def _parse_time(t: str) -> tuple:
    """Return (hour, minute) from 'HH:MM' string."""
    parts = t.split(":")
    if len(parts) != 2:
        raise ValueError(f"Time must be HH:MM, got: {t!r}")
    return int(parts[0]), int(parts[1])


# ---------------------------------------------------------------------------
# macOS launchd
# ---------------------------------------------------------------------------

# Weekday numbers: 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri
_WEEKDAYS = [1, 2, 3, 4, 5]


def _build_plist_dict(cmd: List[str], hour: int, minute: int) -> dict:
    log_dir = Path.home() / ".investorclaw" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    cal_entries = [{"Weekday": wd, "Hour": hour, "Minute": minute} for wd in _WEEKDAYS]

    return {
        "Label": _PLIST_LABEL,
        "ProgramArguments": list(cmd),
        "StartCalendarInterval": cal_entries,
        "WorkingDirectory": str(_ROOT),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(Path.home()),
        },
        "StandardOutPath": str(log_dir / "eod_report.log"),
        "StandardErrorPath": str(log_dir / "eod_report_err.log"),
        "ThrottleInterval": 3600,
    }


def _install_macos(cmd: List[str], hour: int, minute: int) -> int:
    plist_dict = _build_plist_dict(cmd, hour, minute)
    _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PLIST_PATH, "wb") as fh:
        plistlib.dump(plist_dict, fh)
    print(f"Plist written: {_PLIST_PATH}")

    # Unload any existing version first (ignore errors if not loaded)
    subprocess.run(
        ["launchctl", "unload", str(_PLIST_PATH)],
        capture_output=True,
    )
    result = subprocess.run(
        ["launchctl", "load", str(_PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"WARNING: launchctl load failed: {result.stderr.strip()}", file=sys.stderr)
        print(
            "The plist is written — you may need to run 'launchctl load' manually.", file=sys.stderr
        )
        return 1

    print(f"Scheduled: InvestorClaw EOD report, Mon–Fri at {hour:02d}:{minute:02d} local time")
    print(f"Logs: {Path.home()}/.investorclaw/logs/eod_report.log")
    return 0


def _uninstall_macos() -> int:
    if not _PLIST_PATH.exists():
        print("No plist found — nothing to uninstall.")
        return 0
    subprocess.run(["launchctl", "unload", str(_PLIST_PATH)], capture_output=True)
    _PLIST_PATH.unlink()
    print(f"Removed: {_PLIST_PATH}")
    return 0


def _status_macos() -> int:
    if not _PLIST_PATH.exists():
        print("Status: NOT INSTALLED (no plist found)")
        return 1

    result = subprocess.run(
        ["launchctl", "list", _PLIST_LABEL],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Status: LOADED\n{result.stdout.strip()}")
    else:
        print(f"Status: plist exists but not loaded — run 'launchctl load {_PLIST_PATH}'")
    print(f"Plist: {_PLIST_PATH}")
    return 0


# ---------------------------------------------------------------------------
# Linux cron
# ---------------------------------------------------------------------------


def _cron_line(cmd: List[str], hour: int, minute: int) -> str:
    for c in cmd:
        if any(ch in c for ch in "\r\n"):
            raise ValueError(f"Refusing cron arg containing newline: {c!r}")
    cmd_str = " ".join(cmd)
    return f"{minute} {hour} * * 1-5 {cmd_str}  {_CRON_MARKER}"


def _install_linux(cmd: List[str], hour: int, minute: int) -> int:
    new_line = _cron_line(cmd, hour, minute)

    # Get current crontab (may be empty)
    result = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
    )
    existing = result.stdout if result.returncode == 0 else ""

    # Remove any existing investorclaw eod entries
    filtered = [line for line in existing.splitlines() if _CRON_MARKER not in line]
    filtered.append(new_line)
    new_crontab = "\n".join(filtered) + "\n"

    proc = subprocess.run(
        ["crontab", "-"],
        input=new_crontab,
        text=True,
    )
    if proc.returncode != 0:
        print("ERROR: Failed to install crontab entry.", file=sys.stderr)
        return 1

    print(f"Cron entry installed: {new_line}")
    print(f"Schedule: Mon–Fri at {hour:02d}:{minute:02d} (server local time)")
    return 0


def _uninstall_linux() -> int:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        print("No crontab found — nothing to uninstall.")
        return 0

    filtered = [line for line in result.stdout.splitlines() if _CRON_MARKER not in line]
    new_crontab = "\n".join(filtered) + "\n"
    subprocess.run(["crontab", "-"], input=new_crontab, text=True)
    print("InvestorClaw EOD cron entry removed.")
    return 0


def _status_linux() -> int:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        print("Status: No crontab found")
        return 1
    lines = [l for l in result.stdout.splitlines() if _CRON_MARKER in l]
    if lines:
        print(f"Status: INSTALLED\n{lines[0]}")
        return 0
    else:
        print("Status: NOT INSTALLED (no InvestorClaw EOD entry in crontab)")
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install/remove InvestorClaw EOD report scheduler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true", help="Install scheduler")
    group.add_argument("--uninstall", action="store_true", help="Remove scheduler")
    group.add_argument("--status", action="store_true", help="Check scheduler status")

    parser.add_argument(
        "--time",
        default="16:30",
        metavar="HH:MM",
        help="Local time to run (default: 16:30 — after US market close)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Pass --run to eod_report.py (execute full pipeline before report)",
    )
    parser.add_argument(
        "--email-to",
        metavar="ADDRESS",
        default=os.environ.get("EOD_EMAIL_TO", "").strip(),
        help="Recipient address to pass to eod_report.py",
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Pass --pdf to eod_report.py (generate PDF attachment)",
    )

    args = parser.parse_args()

    hour, minute = _parse_time(args.time)

    # Build the eod_report.py command flags
    extra_flags: List[str] = []
    if args.run:
        extra_flags.append("--run")
    if args.email_to:
        try:
            validated_email = _validate_email(args.email_to)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        if validated_email:
            extra_flags.extend(["--email-to", validated_email])
    if args.pdf:
        extra_flags.append("--pdf")

    cmd = _build_cmd(extra_flags)
    is_macos = platform.system() == "Darwin"

    if args.status:
        return _status_macos() if is_macos else _status_linux()

    if args.uninstall:
        return _uninstall_macos() if is_macos else _uninstall_linux()

    # --install
    print("Installing InvestorClaw EOD scheduler …")
    print(f"  Python:   {cmd[0]}")
    print(f"  Script:   {cmd[1]}")
    print(f"  Flags:    {' '.join(extra_flags) or '(none)'}")
    print(f"  Schedule: Mon–Fri at {hour:02d}:{minute:02d} local time")
    print()

    if is_macos:
        return _install_macos(cmd, hour, minute)
    else:
        return _install_linux(cmd, hour, minute)


if __name__ == "__main__":
    sys.exit(main())
