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
Identity file updater for InvestorClaw data-integrity rules.
Updates workspace IDENTITY.md with guardrails for portfolio data handling.
"""

import os
import sys
from pathlib import Path

# File locking for concurrent invocations (Unix only; graceful fallback on others)
try:
    import fcntl  # type: ignore[import]

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows path
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False


_IDENTITY_DATA_INTEGRITY_SECTION = """
---

## InvestorClaw Data Integrity Rules

When working with InvestorClaw output, ALWAYS treat the output files as the
authoritative source of truth. Do NOT use cached session-context values.

**Rule: File Authority**
- Before citing any portfolio value (total, equity, bond, cash), READ the
  actual file: `~/portfolio_reports/holdings.json`
- Extract the value from `data.summary.total_portfolio_value` (and related keys)
- NEVER paraphrase from memory what you think the portfolio is worth
- If the file is missing or unreadable, say so — do not invent values

**Why this matters**: Users make financial decisions based on these numbers.
A stale cached value from a previous session is potentially harmful.

**Canonical value locations**:
| Data | File | JSON path |
|------|------|-----------|
| Portfolio total | `~/portfolio_reports/holdings.json` | `data.summary.total_portfolio_value` |
| Equity value | `~/portfolio_reports/holdings.json` | `data.summary.equity_value` |
| Bond value | `~/portfolio_reports/holdings.json` | `data.summary.bond_value` |
| Cash | `~/portfolio_reports/holdings.json` | `data.summary.cash_value` |
| Performance | `~/portfolio_reports/performance.json` | `data.portfolio_summary` |
| Analysis | `~/portfolio_reports/analysis.json` | `data.portfolio_value` |

---

## Financial Advice Guardrail

**EVERY response that discusses portfolio positions, allocation, or potential actions MUST include an explicit disclaimer.**

**Rule: Educational Framing Only**
- NEVER say "Execute", "Buy", "Sell", "Rotate into", or imply an action the user should take immediately
- NEVER suggest specific dollar amounts to move between holdings as a directive
- ALWAYS frame analysis as: "Based on the data...", "For informational purposes...", "A financial advisor might consider..."
- ALWAYS end responses that contain allocation or rebalancing discussion with:
  > ⚠️ **This analysis is for educational purposes only and is not financial advice. Consult a qualified financial advisor before making investment decisions.**

**Why this matters**: Investors may act on agent responses without understanding the risk. A suggestion to "Rotate $100k into NVDA" could cause significant harm if acted upon without professional guidance, especially in volatile markets.

**Triggered by**: Any mention of rebalancing, buying, selling, rotating, trimming, adding to positions, or allocation targets.
"""


def update_identity(skill_dir: Path) -> int:
    """
    Write/update the InvestorClaw data-integrity section into workspace IDENTITY.md.

    Args:
        skill_dir: Path to skill directory

    Returns:
        0 on success, 1 on failure, 2 if user declined
    """
    # Allow persistent opt-out via environment variable
    if os.environ.get("INVESTORCLAW_SKIP_IDENTITY_UPDATE", "").lower() in ("1", "true", "yes"):
        return 0

    # Non-interactive auto-consent: callers that intentionally want the install
    # without a TTY prompt set this env var. Bypasses the isatty() guard below.
    auto_yes = os.environ.get("INVESTORCLAW_IDENTITY_YES", "").lower() in ("1", "true", "yes")

    # Non-interactive invocation (CI, agents, harness) without auto-consent.
    # Return 2 ("skipped, did NOT install") rather than 0 ("success") so that
    # harness/agent callers can distinguish a real install from a silent skip.
    # Set INVESTORCLAW_IDENTITY_YES=1 to force install in non-TTY contexts, or
    # INVESTORCLAW_SKIP_IDENTITY_UPDATE=1 to explicitly opt out with exit 0.
    if not sys.stdin.isatty() and not auto_yes:
        print(
            "Skipping identity update (non-interactive session; no consent prompt possible). "
            "Set INVESTORCLAW_IDENTITY_YES=1 to install without prompting, or "
            "INVESTORCLAW_SKIP_IDENTITY_UPDATE=1 to silence this."
        )
        return 2

    # Explicit consent — show the user exactly what will be appended
    print("\n🔐 InvestorClaw Data Integrity Rules")
    print("The following section will be appended to your OpenClaw IDENTITY.md:\n")
    print(_IDENTITY_DATA_INTEGRITY_SECTION)
    print("\nThis adds guardrails to your OpenClaw agent IDENTITY.")
    if auto_yes:
        print("[INVESTORCLAW_IDENTITY_YES=1 — auto-consenting for non-interactive run]")
        response = "y"
    else:
        try:
            response = input("Allow [y/n]? ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            response = "n"
    if response not in ("y", "yes"):
        print("Skipping identity update.")
        return 2

    # Candidate locations for IDENTITY.md — OpenClaw workspace takes priority
    candidates = [
        Path.home() / ".openclaw" / "workspace" / "IDENTITY.md",  # OpenClaw workspace (primary)
        skill_dir.parent / "IDENTITY.md",  # skill/../IDENTITY.md (local dev)
    ]

    identity_path = None
    for c in candidates:
        if c.exists():
            identity_path = c
            break

    if identity_path is None:
        # Create at the most likely location
        identity_path = skill_dir.parent / "IDENTITY.md"
        print(f"IDENTITY.md not found; creating at {identity_path}")

    MARKER_START = "## InvestorClaw Data Integrity Rules"

    # Use a sibling lockfile to serialize concurrent invocations against
    # the same IDENTITY.md. Locking the target file directly would require
    # opening it for write before we've decided on new content, which is
    # hostile to the "read-modify-write" pattern below.
    lock_path = identity_path.with_suffix(identity_path.suffix + ".lock")
    lock_fh = None
    lock_acquired = False
    if _HAS_FCNTL:
        try:
            # Ensure parent exists for the lockfile
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_fh = open(lock_path, "a+")
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            lock_acquired = True
        except OSError as e:
            print(
                f"⚠️  Could not acquire identity update lock ({e}); proceeding without lock.",
                file=sys.stderr,
            )
            if lock_fh is not None:
                try:
                    lock_fh.close()
                except Exception:
                    pass
                lock_fh = None

    try:
        # Read existing content (inside the lock so concurrent runs see each
        # other's writes consistently)
        existing = identity_path.read_text() if identity_path.exists() else ""

        # Remove any existing InvestorClaw section to replace with fresh version
        if MARKER_START in existing:
            # Find and strip from the marker to the next "---" separator or EOF
            start_idx = existing.index(MARKER_START)
            # Walk back to the preceding "---" separator line
            preceding = existing.rfind("\n---\n", 0, start_idx)
            if preceding != -1:
                existing = existing[:preceding]
            else:
                existing = existing[:start_idx]
            existing = existing.rstrip()

        new_content = existing + _IDENTITY_DATA_INTEGRITY_SECTION
        identity_path.write_text(new_content)
        print(f"✅ IDENTITY.md updated with data-integrity rules: {identity_path}")
        return 0
    finally:
        if lock_fh is not None:
            try:
                if lock_acquired and _HAS_FCNTL:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_fh.close()
            except Exception:
                pass
