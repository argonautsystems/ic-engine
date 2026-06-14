---
name: investorclaw
description: Deterministic-first portfolio analyzer for Hermes via MCP-HTTP at localhost:18090. Holdings, performance, Sharpe + Sortino, FRED yields, bond duration, scenario rebalancing.
homepage: https://github.com/argonautsystems/InvestorClaw
user-invocable: true
metadata: {"license":"MIT-0","version":"4.8.0","runtime":"hermes","image":"ghcr.io/argonautsystems/ic-engine:4.8.0-cpu","mcp-endpoint":"http://localhost:18090/mcp"}
---

<!--
SPDX-License-Identifier: MIT-0
Copyright 2026 InvestorClaw Contributors

This SKILL.md is MIT-0-licensed and tailored for the NousResearch Hermes
Agent (v0.12+). The InvestorClaw service it connects to is Apache 2.0.
See LICENSE-MIT-0 in this directory.
-->

# InvestorClaw — Hermes Skill (v4.0)

> Powered by [InvestorClaw](https://investorclaw.app) (Apache 2.0).
> This skill file is MIT-0-licensed; the underlying service is Apache 2.0.

## TL;DR for hermes operators

InvestorClaw v4.0 turns hermes into a **first-class portfolio-analysis
agent**. The service runs as two local Docker containers and exposes
its capabilities over MCP-HTTP. Hermes 0.12+ registers the MCP servers
as native function-callable tool sources — the LLM sees
`investorclaw.portfolio_ask`, `mnemos.search_memories`, and friends in
the same tool catalog as `browser_*`, `terminal`, and `skill_view`.

**Headline upgrade — HER-1 is gone.** Read the next section if you
remember v2.x.

## What changed since v2.x — HER-1 elimination

If you ran InvestorClaw v2.x against hermes, you hit **HER-1**: the
"skill-as-doc-hint" caveat. In v2.x, InvestorClaw shipped as a hermes
skill bundle injected into the system prompt. The LLM had to use
hermes meta-tools (`skill_view`, `terminal`) to *read* the skill
documentation and then *imitate* the analyst commands by shelling out.
That indirection layer was lossy and slow, and the Linux baseline
empirical reliability landed around **8% (2.3/30)** on the standard
prompt barrage — vs **77%** on zeroclaw, which had real tool
registration.

**v4.0 ends that.** There is no skill bundle to inject. The
deterministic engine runs as a containerized service and publishes its
analytical surface over MCP-HTTP. Hermes 0.12+ registers MCP servers
declaratively in `~/.hermes/config.yaml` and exposes their tools to
the LLM directly — same dispatch path as any other built-in tool.

What this means in practice for hermes users:

- **No more meta-tool indirection.** The LLM calls
  `investorclaw.portfolio_ask` directly, not via `skill_view` →
  `terminal` → fragile shell parsing.
- **Reliability now matches other agent runtimes.** Expect the same
  routing accuracy as zeroclaw / openclaw — roughly an order of
  magnitude better than v2.x on the same prompts.
- **Memory is built in.** The `mnemos.*` tool family gives hermes a
  persistent memory layer it never had before, scoped to InvestorClaw
  observations and user preferences.
- **No skill bundle to keep in sync.** Bumping the service to a newer
  ic-engine version is `docker compose pull && docker compose up -d`.
  The tool catalog hermes sees is whatever the running service
  publishes.

## Architecture (hermes ⇄ InvestorClaw)

```
hermes (host)
  │
  │  config.yaml mcp_servers:
  │     investorclaw → http://localhost:18090/mcp
  │     mnemos       → http://localhost:5002/mcp
  ▼
Docker compose (~/.investorclaw/compose.yml)
  ├── argonautsystems/ic-engine:4.8.0-cpu       :8090   portfolio analysis MCP
  └── mnemos-os/mnemos-rs:4.2       :5002   memory + KG MCP
       (dashboard at :8092 for portfolio upload + key config)
```

The user runs `docker compose up -d` to install the service. Hermes
discovers tools at startup by handshaking with each MCP server.

## Tool surface

When InvestorClaw is running and hermes has reloaded its config,
the tool catalog gains:

### Portfolio analysis (`investorclaw.*`)

- `investorclaw.portfolio_ask` — natural-language portfolio question
  routed through the deterministic engine
- `investorclaw.portfolio_holdings` — current snapshot of positions,
  values, weights
- `investorclaw.portfolio_performance` — Sharpe, volatility, top /
  bottom performers, max drawdown
- `investorclaw.portfolio_bonds` — bond analytics (YTM, duration,
  FRED yield curve)
- `investorclaw.portfolio_analyst` — analyst ratings per holding
- `investorclaw.portfolio_news` — news correlation for held positions
- `investorclaw.portfolio_lookup` — ticker / account lookup
- `investorclaw.portfolio_optimize` — Sharpe / min-vol optimization
- `investorclaw.portfolio_rebalance` — current vs target with tax
  impact
- `investorclaw.portfolio_scenario` — what-if scenarios on holdings
- `investorclaw.portfolio_cashflow` — projected cashflow from bonds
- `investorclaw.portfolio_peer` — peer comparison vs benchmark
- `investorclaw.portfolio_setup` — auto-discover portfolio files in
  `/data/portfolios/`
- `investorclaw.portfolio_refresh` — refresh market data without
  re-uploading files
- `investorclaw.portfolio_guardrails` — view / configure
  educational-only guardrails

### Memory (`mnemos.*`)

- `mnemos.search_memories` — full-text + semantic search across
  remembered observations
- `mnemos.create_memory` — record an observation about user
  preferences, prior questions, or current investing context
- `mnemos.list_memories` — browse by category / date

## Usage idioms

Just ask portfolio questions in chat. Hermes' LLM picks the right
MCP tool from the catalog automatically.

### Cookbook — what to ask

| Intent | Phrasing |
|---|---|
| Holdings | "What's in my portfolio?" • "Show me my positions" |
| Performance | "How am I doing this year?" • "What's my Sharpe ratio?" |
| Bonds | "Show me my bond exposure and yield-to-maturity" |
| Allocation | "What's my sector exposure?" |
| Optimization | "Help me rebalance to a 60/40 target" |
| Market data | "What's the current price of NVDA?" |
| News | "Today's news on my holdings" |
| Reports | "Generate today's EOD report" • "Prepare an advisor brief" |
| Fresh data | "Prices moved — refresh before answering" |

The first call after a cold cache may take 30–60 seconds while the
deterministic pipeline builds the signed envelope; subsequent calls reuse
the cache.

```bash
hermes chat -q "What's in my portfolio?" \
  --provider together -m google/gemma-4-31B-it --yolo

hermes chat -q "What changed since last week?" \
  --provider together -m google/gemma-4-31B-it --yolo

hermes chat -q "Refresh my market data and show me the worst
                performer." \
  --provider together -m google/gemma-4-31B-it --yolo
```

## Recommended narrative model

hermes routes its chat completions through whichever provider the user
selects on the command line or in `~/.hermes/config.yaml`. **Anthropic
on hermes — paid path only since 2026-04-04**: routing OAuth-
subscription tokens to a claws-agent violates Anthropic's ToS per their
Apr 3 announcement. To use Anthropic models you need either (a) the
discounted "extra usage bundle" add-on for your subscription, or (b) a
direct Anthropic API key. Even with paid credits, Anthropic isn't
cost-competitive with Together for InvestorClaw narrative work; we
don't deploy Anthropic on our own fleet for hermes.

Recommended providers for the InvestorClaw narrative tier (set
`TOGETHER_API_KEY` in the container's `portfolios/keys.env` or via
`portfolio_keys_set`):

- **Default narrative** — Together AI `google/gemma-4-31B-it` — serverless
  tier, ~100 tok/s, ~$0.0008 / 1 K tokens, fleet default. This is what the
  InvestorClaw container expects via `INVESTORCLAW_NARRATIVE_MODEL`.
- **Higher-quality alternative** — Together AI `MiniMaxAI/MiniMax-M2` —
  larger context, but moved off Together's serverless tier 2026-05;
  requires a paid dedicated endpoint.
- **Local-only / offline** — Ollama `gemma4:e4b` on host — zero cloud
  cost, GPU-bound, no key required.

Recommended LLM behavior (the model already does this on its own,
but worth knowing):

1. **Portfolio questions →** call `investorclaw.portfolio_ask` with
   the user's natural-language question. The deterministic engine
   routes it to the correct analyzer and returns a structured
   `ic_result` envelope plus a narrative body.
2. **Follow-up questions →** call `mnemos.search_memories` first to
   pull relevant prior observations (risk tolerance, prior holdings
   discussions). Then call the appropriate `investorclaw.*` tool with
   that context.
3. **What-changed questions →** combine `mnemos.search_memories` for
   prior portfolio summaries with `investorclaw.portfolio_holdings`
   for the current snapshot; let the LLM diff them.
4. **After delivering an analysis →** call `mnemos.create_memory` to
   record salient observations the user might want to remember
   (e.g., "User flagged BABA as a never-sell sentimental position").
   Don't over-record.

## Important behaviors

- **Deterministic at the data layer.** If a portfolio file format
  isn't recognized, the tool returns a structured error with detected
  columns and supported formats. Surface that to the user — point
  them at the dashboard's column-mapping wizard at
  http://localhost:18092/portfolios/map.
- **Educational only — never investment advice.** All outputs include
  the disclaimer envelope. Echo it when summarizing.
- **MCP servers are local by default** at `http://localhost:18090/mcp`
  and `http://localhost:5002/mcp`. Remote deployments (Tailscale,
  cloud) just change the URLs — the tool surface is identical.
- **No portfolio? No problem.** The LLM can talk about generic
  market questions via `investorclaw.portfolio_ask` even before a
  portfolio file is uploaded; it'll guide the user to the dashboard.

## Install pointer

**Hermes does not use ClawHub** — ClawHub is the OpenClaw / ZeroClaw
skill registry. Running `clawhub install` drops the skill into an
openclaw path, not Hermes (and `hermes skills install investorclaw`
won't find it — it isn't in a Hermes registry). Install into Hermes
manually:

1. Copy this skill directory into `~/.hermes/skills/investorclaw/`.
2. Bring up the InvestorClaw container (the engine):
   `cd ~/.investorclaw && docker compose up -d`.
3. Paste the MCP block from `config-snippet.yaml` into
   `~/.hermes/config.yaml`, then restart Hermes.

`INSTALL.md` next to this file has the full step-by-step (skill drop,
compose up, config block, restart, verify).

> The **dashboard** (`http://localhost:18092`) and the agent's MCP
> tools both come from the container in step 2 — the skill itself is
> only the agent-side pointer, so Docker must be running either way.

(Claude Code / Claude Desktop users instead install the marketplace
plugin from `argonautsystems/InvestorClaw`; see `docs/GETTING_STARTED.md`.)

## What this skill does NOT do

- Does not manage money or execute trades
- Does not give investment advice
- Does not access user accounts or move funds
- Educational outputs only

## Reporting issues

This skill describes the InvestorClaw service from a hermes operator's
perspective. If a tool returns an unexpected result, the issue is in
the upstream service (Apache 2.0,
`mnemos-os/mnemos-ic-runtime` + `argonautsystems/InvestorClaw`), not in this
SKILL.md. If hermes can't see the tools at all, that's an install
issue — work through `INSTALL.md` and the troubleshooting section
there.

## Time-window / historical questions

Route last week / last month / last quarter / since DATE / historical performance questions to investorclaw.portfolio_performance_window, not portfolio_ask. Cookbook mapping: last week to period=1w; past two weeks to period=2w; last month or past month to period=1mo; last quarter to period=3mo; past six months to period=6mo; this year or YTD to period=ytd; last year to period=1y; last N days to the nearest supported period token when possible (1d/1w/2w/1mo/3mo/6mo/1y/2y) or to start_date=YYYY-MM-DD plus end_date=YYYY-MM-DD; since DATE to start_date=YYYY-MM-DD; explicit ranges to start_date=YYYY-MM-DD and end_date=YYYY-MM-DD. The tool returns signed per-holding start/end prices, return_pct, contribution, total_return_pct, total_pnl, and top_movers without narration. Keep portfolio_ask for open-ended why/explanation questions after quoting the deterministic window result.
