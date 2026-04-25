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
First-run detection and setup offer.

Checks if InvestorClaw is being run for the first time and offers
interactive setup wizard to configure the LLM.
"""

import json
import sys
from pathlib import Path

CONFIG_FILE = Path.home() / ".investorclaw" / "setup_config.json"


def is_first_run() -> bool:
    """Check if this is first time running InvestorClaw."""
    return not CONFIG_FILE.exists()


def get_config() -> dict:
    """Load configuration if it exists."""
    if not CONFIG_FILE.exists():
        return {}

    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def offer_setup_wizard() -> bool:
    """
    Offer to run setup wizard if first run.

    Returns:
        True if wizard was run or user skipped
        False if user wants to continue without setup
    """
    if not is_first_run():
        return True  # Already configured, proceed

    # Skip interactive prompt when not running in a terminal (agentic sessions, CI)
    if not sys.stdin.isatty():
        return True

    print("\n" + "=" * 70)
    print("FIRST RUN DETECTED")
    print("=" * 70)
    print("""
InvestorClaw uses a unified single-tier LLM architecture via OpenClaw gateway.
One model handles all tasks: routing, analysis, and guardrail enforcement.

Run the setup wizard to configure your LLM:
  → Recommended: xAI Grok 4.1-fast (~$10-20/month, 2M context, 4M TPM)
  → Alternative: OpenAI GPT-4.1-nano (~$10-20/month, 1M context)

This wizard takes ~2-3 minutes.
""")

    response = input("Run setup wizard now? [y/n/skip]: ").strip().lower()

    if response == "y":
        # Import here to avoid circular dependency
        from setup.setup_wizard import SetupWizard

        wizard = SetupWizard()
        wizard.run()
        return True
    elif response == "skip":
        print("\n⏭️  Skipping setup. You can run setup later:")
        print("   python skill/setup_wizard.py\n")
        return True
    else:
        return False


def check_and_offer() -> None:
    """
    Check for first run and offer setup.

    Call this at the start of investorclaw.py main()
    """
    # Skip interactive prompt when not running in a terminal (agentic sessions, CI)
    if not sys.stdin.isatty():
        return

    if not is_first_run():
        return  # Already configured

    if not offer_setup_wizard():
        print("\n⚠️  LLM configuration required to continue.")
        print("Run setup wizard: python skill/setup_wizard.py\n")
        sys.exit(1)


if __name__ == "__main__":
    # Test script
    if is_first_run():
        print("First run detected")
        offer_setup_wizard()
    else:
        print("Already configured")
        config = get_config()
        print(f"Config: {json.dumps(config, indent=2)}")
