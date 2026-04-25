#!/usr/bin/env python3
# Copyright 2026 InvestorClaw Contributors
# Licensed under the Apache License, Version 2.0
"""
Dashboard-deferral stub. Emits the canonical deferral message from
`references/presentation-nl-query-routing.md` Pattern 10 so the agent
always receives a clean, quotable response for `/portfolio dashboard`
instead of an `Unknown command: dashboard` error.

The interactive PWA dashboard moved to `claude/commands/_incomplete/`
in v2.1.0 and remains deferred through v2.1.x. This stub keeps the
command surface well-defined while the dashboard is in development.
"""

from __future__ import annotations

import json
import sys
import time

MESSAGE = (
    "The interactive PWA dashboard is in development for a future "
    "release and isn't shipped in this install. In place of the "
    "dashboard, run `/portfolio analysis` for a narrative walkthrough "
    "or `/portfolio complete` for the full 8-stage pipeline — both "
    "produce the same underlying data the dashboard will visualize."
)


def main() -> int:
    started = time.time()
    envelope = {
        "disclaimer": "EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        "is_investment_advice": False,
        "status": "deferred",
        "reason": "dashboard_in_development",
        "guidance": MESSAGE,
        "alternatives": ["/portfolio analysis", "/portfolio complete"],
    }
    print(json.dumps(envelope))
    elapsed_ms = int((time.time() - started) * 1000)
    print(
        json.dumps(
            {
                "ic_result": {
                    "script": "dashboard_deferred.py",
                    "exit_code": 0,
                    "duration_ms": elapsed_ms,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
