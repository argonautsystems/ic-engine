# ic-engine

Python portfolio-analysis library powering the InvestorClaw skill ecosystem.

## What this is

`ic-engine` is the deterministic Python core (CDM 5.x/6.x models, providers, computation pipeline, runtime, the `investorclaw` CLI) that two adapters consume to surface portfolio analysis to AI agents:

- **InvestorClaw** — the claws-runtime adapter (OpenClaw / Hermes / ZeroClaw / standalone CLI). Uses `/portfolio` slash prefix.
- **InvestorClaude** — the Claude Code plugin adapter. Uses `/investorclaw:*` slash prefix.

This repo is the canonical source for the engine code. Adapters depend on it via `uv pip`. The agent-skill contract template (the canonical L2 routing rules that both adapters render their `SKILL.md` from) also lives here at `contract/`.

## Install

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
