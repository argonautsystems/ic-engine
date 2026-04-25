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
Stonkmode control — activate, deactivate, and check status.

Usage:
    python3 stonkmode_control.py on
    python3 stonkmode_control.py off
    python3 stonkmode_control.py status
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ic_engine.rendering.stonkmode import STATE_FILE, load_state, save_state
from ic_engine.rendering.stonkmode_pairings import get_pairing_dynamic, select_pair
from ic_engine.rendering.stonkmode_personas import get_persona


def activate(lead_id: str = "", foil_id: str = "") -> int:
    """Select a pair, write state, and print activation banner."""
    if not lead_id or not foil_id:
        lead_id, foil_id = select_pair()
    lead = get_persona(lead_id)
    foil = get_persona(foil_id)

    lead_archetype = lead["archetype"]
    foil_archetype = foil["archetype"]
    dynamic = get_pairing_dynamic(lead_archetype, foil_archetype, lead_id, foil_id)

    state = {
        "enabled": True,
        "lead_id": lead_id,
        "foil_id": foil_id,
        "lead_name": lead["name"],
        "foil_name": foil["name"],
        "lead_archetype": lead_archetype,
        "foil_archetype": foil_archetype,
        "pairing_dynamic": dynamic,
        "activated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "segment_count": 0,
        "session_message_history": [],
    }
    save_state(state)

    print()
    print("STONKMODE ACTIVATED")
    print()
    print(f"Today's desk: {lead['name']} (lead)")
    print(f"            + {foil['name']} (foil)")
    print()
    print(f"Pairing: {dynamic}")
    print()
    print("All /portfolio commands will be delivered")
    print("in character until /portfolio stonkmode off.")
    print()

    return 0


def deactivate() -> int:
    """Remove state file and print deactivation message."""
    state = load_state()
    segment_count = 0
    if state:
        segment_count = state.get("segment_count", 0)

    if STATE_FILE.exists():
        STATE_FILE.unlink()

    print()
    print("STONKMODE DEACTIVATED")
    if segment_count:
        print(f"Segments delivered: {segment_count}")
    print()
    print("Regular output mode restored.")
    print()

    return 0


def status() -> int:
    """Print current stonkmode state."""
    state = load_state()

    if not state or not state.get("enabled"):
        print()
        print("Stonkmode: OFF")
        print()
        print("Activate with: /portfolio stonkmode on")
        print()
        return 0

    lead_name = state.get("lead_name", "Unknown")
    foil_name = state.get("foil_name", "Unknown")
    activated = state.get("activated_at", "Unknown")
    segments = state.get("segment_count", 0)
    dynamic = state.get("pairing_dynamic", "")

    print()
    print("Stonkmode: ON")
    print()
    print(f"Lead: {lead_name}")
    print(f"Foil: {foil_name}")
    print(f"Activated: {activated}")
    print(f"Segments: {segments}")
    if dynamic:
        print()
        print(f"Pairing: {dynamic}")
    print()
    print("Deactivate with: /portfolio stonkmode off")
    print()

    return 0


def main() -> int:
    """Route subcommands."""
    if len(sys.argv) < 2:
        return status()

    subcmd = sys.argv[1].lower().strip()

    if subcmd in ("on", "activate", "enable"):
        # Optional: --lead <id> --foil <id> to force a specific pair
        lead_arg = foil_arg = ""
        args = sys.argv[2:]
        for i, a in enumerate(args):
            if a == "--lead" and i + 1 < len(args):
                lead_arg = args[i + 1]
            elif a == "--foil" and i + 1 < len(args):
                foil_arg = args[i + 1]
        return activate(lead_arg, foil_arg)
    elif subcmd in ("off", "deactivate", "disable"):
        return deactivate()
    elif subcmd in ("status", "state", "check"):
        return status()
    else:
        print(f"Unknown stonkmode subcommand: {subcmd}")
        print("Usage: /portfolio stonkmode [on|off|status]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
