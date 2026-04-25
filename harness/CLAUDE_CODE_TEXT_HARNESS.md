# Claude Code text harness — InvestorClaw v2.2 routing pilot

Per RFC §6.3 the v2.2 acceptance gate measures routing quality across
runtimes. The 3 containerized runtimes (OpenClaw / ZeroClaw / Hermes) are
exercised automatically by `harness/run_cross_runtime_pilot.py` via
`docker exec`. Claude Code is intentionally NOT automated — robotic control
of a Claude Code session is brittle.

This document is the **manual harness for Claude Code**: open a fresh
Claude Code session on STUDIO with InvestorClaw loaded as a skill, paste
each prompt below, observe whether Claude invokes the expected tool, and
fill in the scoring table at the bottom.

## Setup

1. Verify the InvestorClaw skill is loaded in Claude Code (check `~/.claude/`
   plugin/skill paths or whatever your config uses).
2. Open a fresh chat (no prior context) so routing isn't influenced by
   prior turns.
3. Set the underlying model to **Gemini-flash-latest** for parity with the
   other runtimes' baseline.
4. For each scenario below, paste the prompt verbatim and check whether
   Claude routes to one of the listed `expected_tools`.

## Scoring rule

A scenario PASSES if Claude invokes any tool from `expected_tools`. If
Claude answers from training data without calling a tool, or calls an
unrelated tool, the scenario FAILS.

Acceptance gate (RFC §6.3): **Claude Code ≥ 10/10** (no regression — was
10/10 in v2.1.9 baseline).

---

## Prompts

### p01-holdings — `portfolio_view --section=holdings`
**Prompt:** `What's in my portfolio right now?`
**Expected tools:** `holdings`, `view`
**Observed tool(s):** ___
**Pass/Fail:** ___

### p02-performance — `portfolio_view --section=performance`
**Prompt:** `How has my portfolio performed this year?`
**Expected tools:** `performance`, `view`
**Observed tool(s):** ___
**Pass/Fail:** ___

### p03-bonds — `portfolio_bonds --section=analysis`
**Prompt:** `Show me my bond exposure and yield-to-maturity for fixed income.`
**Expected tools:** `bonds`, `fixed-income`
**Observed tool(s):** ___
**Pass/Fail:** ___

### p04-bond-strategy — `portfolio_bonds --section=strategy`
**Prompt:** `What bond laddering strategy should I use given current rates?`
**Expected tools:** `fixed-income`, `bonds`
**Observed tool(s):** ___
**Pass/Fail:** ___

### p05-rebalance — `portfolio_scenario --section=rebalance`
**Prompt:** `Should I rebalance my portfolio?`
**Expected tools:** `scenario`, `rebalance`
**Observed tool(s):** ___
**Pass/Fail:** ___

### p06-news-merger — `portfolio_market --section=news --topic=merger`
**Prompt:** `Any big mergers or acquisitions in the news today?`
**Expected tools:** `market`, `news`
**Observed tool(s):** ___
**Pass/Fail:** ___

### p07-news-crypto — `portfolio_market --section=news --topic=crypto`
**Prompt:** `What's happening in crypto markets today?`
**Expected tools:** `market`
**Observed tool(s):** ___
**Pass/Fail:** ___

### p08-deflect-concept — `portfolio_market --section=concept` (deflection)
**Prompt:** `What does yield-to-maturity mean?`
**Expected tools:** `concept`, `market`
**Observed tool(s):** ___
**Pass/Fail:** ___

### p09-deflect-market — `portfolio_market --section=market` (deflection — NOT news)
**Prompt:** `What's the current price of NVDA?`
**Expected tools:** `market`, `lookup`
**Observed tool(s):** ___
**Pass/Fail:** ___

### p10-synthesize — `portfolio_compute --section=synthesize`
**Prompt:** `Give me the full picture of my portfolio.`
**Expected tools:** `synthesize`, `compute`, `analysis`, `complete`
**Observed tool(s):** ___
**Pass/Fail:** ___

---

## Aggregate

Total scenarios: 10
Passed: ___ / 10
Gate (≥ 10/10): ___ PASS / FAIL

## Notes

Add any observations about routing surprises, unexpected tool chains, or
LLM behavior that informs follow-up tuning of `SKILL.toml` descriptions.

```
(your notes here)
```

## When done

Paste the filled-in scoring table back into the agent thread that
generated this harness, or commit it to `reports/v2.2-claude-code-pilot.md`
in the repo for v2.2 evidence alongside the JSON report from the
auto-pilot.
