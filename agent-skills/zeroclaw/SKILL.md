---
name: investorclaw
description: Deterministic-first portfolio analyzer for ZeroClaw via MCP-HTTP at localhost:18090. Holdings, performance, Sharpe + Sortino, FRED yields, bond duration, scenario rebalancing.
homepage: https://github.com/argonautsystems/InvestorClaw
user-invocable: true
metadata: {"license":"MIT-0","version":"4.7.0","runtime":"zeroclaw","image":"ghcr.io/argonautsystems/ic-engine:4.7.0-cpu","mcp-endpoint":"http://localhost:18090/mcp"}
---

<!--
SPDX-License-Identifier: MIT-0
Copyright 2026 InvestorClaw Contributors

This SKILL.md is MIT-0-licensed. The InvestorClaw service it connects to is
Apache 2.0. See the project LICENSE-MIT-0 for full text.

This skill is audit-compliant for zeroclaw 0.7.3+: no scripts, no
symlinks, no curl-pipe-shell patterns, no remote markdown links.
-->

# InvestorClaw — zeroclaw skill

> Powered by [InvestorClaw](https://investorclaw.app) (Apache 2.0).
> Skill manifest is MIT; the underlying service is Apache 2.0.

## What this is

InvestorClaw is a containerized portfolio analysis service that exposes
its analytical capabilities to your zeroclaw agent over MCP-HTTP. Two
local servers register as separate MCP namespaces in your tool catalog:

- `investorclaw` (port 8090) — deterministic portfolio analysis
- `mnemos` (port 5002) — memory + knowledge graph

You speak to zeroclaw in natural language. zeroclaw routes the request
to the right MCP tool, calls it, and synthesizes a reply. **The user is
the orchestrator; the service is the substrate; zeroclaw is the
interface.**

## How zeroclaw connects

zeroclaw on master supports MCP via the `[mcp.servers.<name>]` block in
`~/.zeroclaw/config.toml`. Once that config is in place and the
InvestorClaw containers are running, the tools are auto-registered at
agent startup. No skill code, no shell-out, no per-tool wiring.

The two services run as a Docker compose stack, bound to localhost:

- `mnemos-os/mnemos-rs:4.2` → `localhost:5002`
- `ghcr.io/argonautsystems/ic-engine:4.7.0-cpu` → `localhost:18090`

**Quick install via ClawHub:**

```bash
clawhub install perlowja/investorclaw
```

**Claude Code / Claude Desktop:**

```
/plugin marketplace add argonautsystems/InvestorClaw
/plugin install investorclaw
```

If the user has not installed yet, see `INSTALL.md` in this skill
directory for full manual setup details. zeroclaw cannot install the service from
inside a skill (audit rules forbid scripted execution from skill
payload), but the future `zeroclaw services install <compose-url>`
upstream subcommand will close that gap with a single command.

## Tool surface

Once the MCP servers are registered, your tool catalog gains:

### Portfolio analysis (`investorclaw.*`)

- `investorclaw.portfolio_ask` — natural-language question routed
  through the deterministic engine; returns a structured `ic_result`
  envelope plus narrative text
- `investorclaw.portfolio_holdings` — current snapshot: positions,
  values, weights, cost basis
- `investorclaw.portfolio_performance` — Sharpe, volatility, top/bottom
  performers, max drawdown, returns over horizons
- `investorclaw.portfolio_bonds` — bond analytics: YTM, duration,
  convexity, FRED yield-curve overlay
- `investorclaw.portfolio_analyst` — analyst consensus per holding
- `investorclaw.portfolio_news` — news correlation for held positions
- `investorclaw.portfolio_lookup` — ticker / account lookup
- `investorclaw.portfolio_optimize` — Modern Portfolio Theory: Sharpe-
  max, min-vol, target-return frontiers
- `investorclaw.portfolio_rebalance` — current vs. target allocation
  with capital-gains impact
- `investorclaw.portfolio_scenario` — what-if scenarios (rate shocks,
  drawdowns, correlation breaks)
- `investorclaw.portfolio_cashflow` — projected cashflow calendar
  (coupons, dividends, maturities)
- `investorclaw.portfolio_peer` — peer/benchmark comparison
- `investorclaw.portfolio_setup` — auto-discover portfolio files in
  `/data/portfolios/`
- `investorclaw.portfolio_refresh` — refresh market data without
  re-uploading files
- `investorclaw.portfolio_guardrails` — view/configure educational-only
  guardrails

### Memory (`mnemos.*`)

- `mnemos.search_memories` — full-text + semantic search
- `mnemos.create_memory` — record an observation about the user's
  preferences, prior questions, or current investing context
- `mnemos.list_memories` — browse by category / date range

## Usage idioms

zeroclaw routes natural-language requests to MCP tools without manual
hinting. These are the expected interaction shapes:

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

The first call after a cold cache may take 30–60 seconds while the
deterministic pipeline builds the signed envelope; subsequent calls reuse
the cache.

**Snapshot questions**

- "What's in my portfolio?" → `investorclaw.portfolio_holdings`
- "How are my bonds doing?" → `investorclaw.portfolio_bonds`
- "What's my Sharpe ratio?" → `investorclaw.portfolio_performance`

**Open-ended analysis**

- "Why is my portfolio down this week?" →
  `investorclaw.portfolio_ask` (the engine routes to the right
  internal analyzer, e.g., `whatchanged` + `news`)
- "Should I rebalance?" → `investorclaw.portfolio_rebalance` followed
  by `investorclaw.portfolio_optimize` for a target allocation

**Continuity questions**

- "What did we talk about last time?" → `mnemos.search_memories` with
  the recent date range, then summarize
- "Remember that I want to keep BABA no matter what." →
  `mnemos.create_memory` with category=preferences

**Composite workflows**

- A portfolio review naturally chains:
  `mnemos.search_memories` (prior context) →
  `investorclaw.portfolio_holdings` (current state) →
  `investorclaw.portfolio_performance` (returns since last review) →
  `investorclaw.portfolio_news` (drivers) →
  `mnemos.create_memory` (record salient new observations)

zeroclaw will sequence these on its own when the user asks for a full
review. You don't have to script the chain.

## Recommended narrative model

zeroclaw routes its chat completions through whichever provider is
configured in `~/.zeroclaw/config.toml`. **Anthropic on zeroclaw — paid
path only since 2026-04-04**: routing OAuth-subscription tokens to a
claws-agent violates Anthropic's ToS per their Apr 3 announcement. To
use Anthropic models you need either (a) the discounted "extra usage
bundle" add-on for your subscription, or (b) a direct Anthropic API
key. Even with paid credits, Anthropic isn't cost-competitive with
Together for InvestorClaw narrative work; we don't deploy Anthropic on
our own fleet for zeroclaw.

Recommended providers for the InvestorClaw narrative tier (set
`TOGETHER_API_KEY` in the container's `portfolios/keys.env` or via
`portfolio_keys_set`):

- **Default narrative** — Together AI `google/gemma-4-31B-it` — serverless,
  ~100 tok/s, ~$0.0008 / 1 K tokens, fleet default.
- **Higher-quality alternative** — Together AI `MiniMaxAI/MiniMax-M2` —
  larger context, but moved off Together's serverless tier 2026-05;
  requires a paid dedicated endpoint.
- **Local-only / offline** — Ollama `gemma4:e4b` on host — zero cloud
  cost, GPU-bound, no key required.

zeroclaw's own model (separate from InvestorClaw's narrative tier)
follows the same posture — Together gemma-4-31B-it is the fleet default
for both layers; Anthropic remains a paid-API-only opt-in for end users.

## Important behaviors

- **The investorclaw tools are deterministic.** If a portfolio CSV
  format isn't recognized, you'll get a structured error listing
  detected columns and supported formats. Surface the error verbatim;
  don't ask the LLM to guess column mappings. Direct the user to the
  dashboard wizard at `http://localhost:18092/portfolios/map`.

- **Trust the structured output, decorate the narrative.** Every
  `investorclaw.*` tool returns an `ic_result` envelope (the data) plus
  a narrative text body. The data is canonical; the narrative is
  decoration the agent can rewrite for tone.

- **Educational only — never investment advice.** All outputs include a
  disclaimer envelope. Echo it when summarizing.

- **mnemos memory is local.** Observations stay on the user's machine
  unless they explicitly export. Don't ask before recording obvious
  context (e.g., "User holds 28 positions"); do ask before recording
  anything sensitive (e.g., specific dollar amounts a user redacted in
  conversation).

- **Default endpoints are localhost.** If the user deploys
  InvestorClaw on a Tailscale VM or cloud host, the MCP server URLs
  change but the tool surface is identical. The `[mcp.servers.*]`
  blocks in `config.toml` are the single source of truth for endpoints.

## What this skill does NOT do

- Does not execute trades, move money, or access broker accounts
- Does not give investment advice — educational outputs only
- Does not embed portfolio data; the user's CSV/PDF files live under
  `~/.investorclaw/data/portfolios/` (mounted into the engine
  container)
- Does not ship any executable code: SKILL.md and SKILL.toml are
  metadata-only, by audit rule

## Audit compliance

This skill payload (`SKILL.md` + `SKILL.toml` in
`~/.zeroclaw/skills/investorclaw/`) is audit-compliant for zeroclaw
0.7.3+:

- No `*.sh`, `*.bash`, or other executables
- No symlinks
- No remote-script-piping patterns (the audit rejects shell-pipeline
  install hints; we use `docker compose up -d` against a vendored
  `compose.yml` instead)
- No remote markdown image/link references
- All install/operational instructions live in `INSTALL.md`, which is
  user-facing documentation outside the registered skill payload

## Reporting issues

This skill describes the InvestorClaw service. If a tool returns an
unexpected result, the bug is in the service (Apache 2.0, see
`github.com/argonautsystems/ic-engine` and `mnemos-os/mnemos-rs`), not in this
manifest.
