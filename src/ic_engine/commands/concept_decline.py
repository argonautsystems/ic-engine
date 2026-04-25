#!/usr/bin/env python3
# Copyright 2026 InvestorClaw Contributors
# Licensed under the Apache License, Version 2.0
"""
Concept-deflection stub. Emits the canonical Pattern 7 / Pattern 8
response from `references/presentation-nl-query-routing.md` when the
user asks a finance-concept question ("what is YTM?", "explain bond
duration", "define alpha") or a market-wide question ("how is the S&P
performing?", "what's the Fed doing?") rather than something about
their own holdings.

Routing guidance: the agent should prefer this when the question shape
matches Pattern 7 (concept) or Pattern 8 (market-wide). It exists as
an explicit skill command so the agent has a correct, ic_result-
verified target to route to instead of either:
  (a) answering from training data (produces an ic_result-absent
      response that looks authoritative but is unverifiable), or
  (b) passing the NL question to `lookup.py` as a symbol (produces
      "Symbol '...' not found in holdings" exit 1).
"""

from __future__ import annotations

import json
import sys
import time

CONCEPT_MESSAGE = (
    "That's a general finance-concept question, not specific to your "
    "portfolio. InvestorClaw is scoped to your actual holdings and "
    "does not run a glossary or education layer. Try asking 'show me "
    "my [bonds / performance / holdings]' instead, or use a general-"
    "purpose knowledge source for concept explanations."
)

MARKET_MESSAGE = (
    "InvestorClaw is scoped to your own portfolio — it joins news, "
    "analyst, and performance data to positions you actually hold. "
    "For open-market commentary, VIX levels, Fed policy, or "
    "macroeconomic data, use a dedicated market-data tool instead."
)


def main(argv: list[str]) -> int:
    started = time.time()
    mode = "concept"
    if argv and argv[0].lower() in {"market", "macro", "market-wide"}:
        mode = "market"
    envelope = {
        "disclaimer": "EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        "is_investment_advice": False,
        "status": "declined",
        "reason": (
            "concept_question_out_of_scope"
            if mode == "concept"
            else "market_wide_question_out_of_scope"
        ),
        "guidance": CONCEPT_MESSAGE if mode == "concept" else MARKET_MESSAGE,
        "agent_instructions": (
            "Present the guidance above verbatim. Do NOT answer the "
            "user's question from training data — doing so produces an "
            "unverifiable response and bypasses the skill contract."
        ),
    }
    print(json.dumps(envelope))
    elapsed_ms = int((time.time() - started) * 1000)
    print(
        json.dumps(
            {
                "ic_result": {
                    "script": "concept_decline.py",
                    "exit_code": 0,
                    "duration_ms": elapsed_ms,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
