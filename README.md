# ic-engine

Python portfolio-analysis library powering the InvestorClaw skill ecosystem.

## What this is

`ic-engine` is the deterministic Python core (CDM 5.x/6.x models, providers, computation pipeline, runtime, the `investorclaw` CLI) that two adapters consume to surface portfolio analysis to AI agents:

- **InvestorClaw v4.x (recommended)** — containerized application service via `ncz-os/mnemos-ic-runtime`. Exposes 20 MCP tools over HTTP at `:18090` and a 17-tab browser dashboard at `:18092`. Image: `ghcr.io/argonautsystems/ic-engine:4.4.2-cpu`. This is the primary path for end users.
- **InvestorClaude** — Claude Code marketplace plugin adapter (`argonautsystems/InvestorClaude`). Uses `/ask` and `/refresh` slash commands. Active on the v2.x line.

This repo is the canonical source for the engine code. Adapters depend on it via `uv pip`. The agent-skill contract template (the canonical L2 routing rules that both adapters render their `SKILL.md` from) also lives here at `contract/`.

## Where this runs

| Consumer | Path | Surface |
|---|---|---|
| **ncz-os/mnemos-ic-runtime** (v4.x, recommended) | Docker container, MCP-HTTP | 20 MCP tools at `:18090`, 17-tab dashboard at `:18092` |
| **argonautsystems/InvestorClaude** (v2.x) | Claude Code plugin, slash commands | `/ask`, `/refresh` |

Most consumers should deploy via the `mnemos-ic-runtime` container rather than installing ic-engine directly.

## Install (developer / standalone testing)

> **Note:** end users should deploy via the `ncz-os/mnemos-ic-runtime` container image. The steps below are for engine developers and standalone testing only.

When published to PyPI:

```bash
uv pip install ic-engine
```

From git (any host):

```bash
uv pip install "git+https://gitlab.com/perlowja/ic-engine.git"
```

Verify:

```bash
investorclaw --version    # → "investorclaw 2.3.0-rc1"
```

## Architecture context

This repo exists because of a 2026-04-25 conformance-test finding: when the engine and its agent-skill contracts lived together in one monorepo, the two adapter `SKILL.md` files silently drifted on slash prefixes, route names, and version stamps — shipping a bug class that's invisible until a debugger-framed conformance run catches it.

The fix is structural: separate the engine (this repo) from the runtime adapters (InvestorClaw, InvestorClaude), and centralize the agent-skill contract template here in `contract/` so both adapters render their `SKILL.md` from one source.

The full architectural decision is documented at:
**[InvestorClaw/docs/IC_DECOMPOSITION_SPEC.md](https://gitlab.com/perlowja/InvestorClaw/-/blob/main/docs/IC_DECOMPOSITION_SPEC.md)**

## What's in this repo

```
ic-engine/
├── src/ic_engine/             # Python package
│   ├── __init__.py            # __version__
│   ├── cli.py                 # `investorclaw` console script entry
│   ├── services/              # consultation, deduplication, PDF extraction
│   ├── models/                # CDM 5.x / 6.x portfolio models
│   ├── providers/             # yfinance, Finnhub, FRED, Polygon, etc
│   ├── runtime/               # router, bootstrap, environment, subprocess runner
│   ├── commands/              # actual command implementations
│   ├── internal/              # pipeline, stages, consultation, fingerprints
│   ├── setup/                 # install wizard, identity updater, hardware probe
│   ├── config/                # env loader, schema, paths, guardrail enforcer
│   ├── rendering/             # output, dashboards, stonkmode narration
│   └── workers/               # background enrichers
├── contract/                  # canonical L2 agent-skill contract
│   ├── routing_rules.md.template
│   ├── routes.toml
│   └── render.py              # adapters call this to render their SKILL.md
├── harness/                   # V13 enterprise barrage (engine conformance)
└── tests/                     # pytest suite
```

## License

Apache-2.0. See `LICENSE`.

## Status

`v2.3.0-rc1` — Phase 1 extraction prototype. Not yet on PyPI; install from git.
