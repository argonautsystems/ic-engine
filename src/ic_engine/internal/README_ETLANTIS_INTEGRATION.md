# ETLANTIS Integration Boundary — Handoff Guide

**Status:** Stable as of ic-engine v2.4.6 (commit `ee83904`, 2026-04-27).
**Audience:** Cleanroom Claude session integrating ETLANTIS stage-6 output
into ic-engine; future Claude sessions doing dep work between the two
packages.

This document describes the integration surface that ETLANTIS code can
land against without conflict. It complements `README_STAGES.md` (the
developer guide for the three stages themselves) by adding the
boundary-level integration concerns.

---

## TL;DR

- **Land ETLANTIS code at:** `src/ic_engine/internal/data_downloader.py`,
  `data_extractor.py`, `data_transformer.py`. These are the canonical
  integration surfaces.
- **Don't touch:** `pipeline.py`, `commands/*.py`, `rendering/*`,
  `config/schema.py`, `models/*`, `services/*`. These just shipped clean
  in the v2.4.6 CDM-vs-legacy sweep.
- **Three integration patterns** are viable; see §3.
- **Version-bump discipline:** see §5. The harness contract gate at
  `harness/contract_check.py` enforces version alignment across
  `pyproject.toml`, `__init__.py`, and (for adapters) SKILL.toml +
  plugin.json + package.json.

---

## 1. The integration surface

The three Phase-2 data stages in `src/ic_engine/internal/` are
deliberately scaffolded with stable public surfaces and no consumer
commitments outside the test suite. As of v2.4.6:

```
$ grep -rn "from ic_engine.internal.data_" src
src/ic_engine/internal/test_data_stages.py:45  (test only)
```

This means:

- The stages are **not** wired into `pipeline.py` orchestration yet —
  `run_pipeline()` still flows through the legacy
  `commands/analyze_performance_polars.py` etc. The Phase-3 async
  orchestrator (`PortfolioPipeline` in `internal/pipeline.py`) DOES wire
  them in for the staged path, but the canonical CLI entry point uses
  the legacy flow.
- Replacing or extending the data stages will **not** break consumers
  outside the stage tests. ETLANTIS code can rewrite these freely.
- The stage interfaces — `DownloadResult`, `ExtractionResult`,
  `ExtractionSchema`, `TransformConfig`, plus the public methods of
  `DataDownloader` / `DataExtractor` / `DataTransformer` — are the
  contract. If ETLANTIS keeps these names, downstream wiring will keep
  working.

### What lives in each stage

| File | Public surface | Line count (v2.4.6) |
|---|---|---|
| `data_downloader.py` | `DataDownloader`, `DataProviderConfig`, `DownloadResult`, `DownloadStats` + provider adapters (yfinance, Finnhub, Massive, Alpha Vantage, NewsAPI) | 691 |
| `data_extractor.py` | `DataExtractor`, `ExtractionSchema`, `ExtractionResult` | 344 |
| `data_transformer.py` | `DataTransformer`, `TransformConfig` | 350 |

See `README_STAGES.md` for the full developer guide on each.

---

## 2. What etlantis owns canonically

etlantis is now a real shippable package at **v0.2.0** (cleanroom Phase
6 ship 2026-04-27, 324 tests, 8 codex review rounds). Not just patterns
lifted from a private dir anymore.

The 9 subsystems live (per cleanroom's 2026-04-27 handoff):

| Subsystem | Status | Notes |
|---|---|---|
| `etlantis.config` | v0.1.0 | manifest_loader |
| `etlantis.ingest` | v0.1.0 + v0.2.0 polish | http_client + archive + reader |
| `etlantis.transform` | v0.1.0 | concat_frames + write_parquet |
| `etlantis.match` | v0.2.0 | ExactMatcher / FuzzyMatcher / SemanticMatcher |
| `etlantis.score` | v0.2.0 | WeightedScorer + ScoreBand |
| `etlantis.closures` | v0.2.0 | TransitionExtractor + SupertransitionDetector |
| `etlantis.analytics` | v0.2.0 | TrajectoryClassifier |
| `etlantis.geo` | v0.2.0 | RegionClassifier |
| `etlantis.pipeline` | stub | NOT landed; consumers compose stage runners app-side |
| `etlantis.enforcement` | stub | NOT landed; consumers compose joins app-side |

Implications for ic-engine:

- **HTTPDownloader-equivalent in ic-engine** (`data_downloader.py`) is
  still the local copy. etlantis's `etlantis.ingest.http_client` is the
  canonical implementation now, but consuming it requires either Pattern
  A (replace) or Pattern B (optional dep) — both are viable, see §3.
- **Schema-standardization in ic-engine** (`data_transformer.py`)
  conceptually maps to `etlantis.transform`, but ic-engine's transformer
  has CDM-aware portfolio knowledge that etlantis doesn't. Likely keep
  ic-engine's local copy and use etlantis only for the
  `concat_frames`/`write_parquet` primitives if needed.
- **Cache-first extractor** (`data_extractor.py`) doesn't have a direct
  etlantis equivalent — cleanroom's reference was RiskyEats SunBiz
  parser, which is now in RiskyEats v1.0.0-rc2 (slim adapter on
  etlantis), not in etlantis itself.
- **`etlantis.pipeline` runner is still stubbed.** Cleanroom's RiskyEats
  slim adapter has its own ~280 LOC stage runner in app code rather
  than waiting on it. ic-engine's `internal/pipeline.py`
  (`PortfolioPipeline`) is similar app-side glue — keep it.
- **fleet_provider** — was tentatively scoped under etlantis but does
  NOT appear in v0.2.0 subsystems. May still live at
  `~/Projects/ETLANTIS/etlantis/fleet_provider.py` (private argonas
  remote, ETLANTIS-shared infra) or may have been deferred. Confirm
  with cleanroom before assuming it exists.

---

## 3. Three viable integration patterns

When ETLANTIS code is ready to integrate, choose one:

### Pattern A — Replace ic-engine internal stages with `etlantis` imports

```python
# src/ic_engine/internal/data_downloader.py becomes a thin re-export:
from etlantis.downloader import HTTPDownloader as DataDownloader  # noqa: F401
from etlantis.downloader.config import HTTPDownloaderConfig as DataProviderConfig  # noqa: F401
```

**Pros:** Single source of truth. ETLANTIS upgrades flow into ic-engine
via `pip install etlantis>=X.Y` bumps.
**Cons:** Adds `etlantis` as a hard dependency of ic-engine; users who
just want portfolio analysis pull in ETL infrastructure they don't need.
The `pyproject.toml` dep list grows.

### Pattern B — Keep ic-engine local copies, add `etlantis` as optional dep

```python
# pyproject.toml
[project.optional-dependencies]
etlantis = ["etlantis>=X.Y"]
```

`data_downloader.py` keeps its own implementation. ETLANTIS-shaped
sub-features (e.g., a richer retry policy) become opt-in via the
extra. ETLANTIS code can also import ic-engine's stages for its own use
without ic-engine importing back.

**Pros:** Clean dep graph; ic-engine stays self-contained.
**Cons:** Two implementations to keep in sync. Bug fixes in ETLANTIS
need to be ported manually until the next pattern-A migration.

### Pattern C — Promote shared parts to `clio`

If ETLANTIS code identifies stage primitives that are foundation-level
(no domain knowledge of finance, no portfolio assumptions), the right
home may be `clio` rather than `ic-engine` or `etlantis`. clio already
has `runtime/hardware.py` and `extract/{vision,schema_map,normalize}.py`
in this category. The stage HTTPDownloader pattern is a candidate;
DataExtractor's cache-first integrity-check pattern is a candidate.
DataTransformer's CDM-aware schema mapping is NOT a candidate (too
domain-specific).

**Pros:** Both ic-engine and ETLANTIS depend on clio for shared
primitives; the dep graph fans in cleanly.
**Cons:** clio version bumps now drive both downstream packages; needs
careful semver discipline.

**Recommendation:** Default to Pattern B. etlantis v0.2.0 is shipped and
stable but `etlantis.pipeline` and `etlantis.enforcement` are still
stubbed (per cleanroom handoff 2026-04-27); the runtime contract is
incomplete for full Pattern A migration. Promote to Pattern A subsystem-
by-subsystem as etlantis subsystems mature past v0.x and as ic-engine
surfaces specific consumer needs (e.g., if `litellm_consultation.py`
grows multi-provider routing and fleet_provider lands in etlantis).
Pattern C (promote to clio) is correct for genuinely foundation-level
pieces but should not be the default.

### Fleet substrate map (post-cleanroom 2026-04-27)

```
clio       v0.1.0   AI extraction primitives  (Muse of history)
etlantis   v0.2.0   ETL substrate              (Atlantis)
calliope   queued   static-site rendering      (Phase 7, cleanroom-side)
mnemos     prod     memory                     (Mnemosyne)
```

calliope is the codename for the Phase 7 static-site rendering substrate
to be extracted from cleanroom. Not directly relevant to ic-engine
unless ic-engine's `rendering/` module grows external consumers — keep
on the radar but don't pre-integrate.

---

## 4. Hard rules — what NOT to touch

These surfaces just shipped clean in v2.4.6 (21 codex review cycles to
final APPROVE) and are out-of-scope for ETLANTIS integration work:

1. **`src/ic_engine/pipeline.py`** — the canonical CLI pipeline. Heavy
   CDM-vs-legacy translation logic. Touching this re-opens the v2.4.x
   review loop.
2. **`src/ic_engine/commands/*.py`** — all consumer commands
   (export_report, fa_discussion, lookup, news_fetch_planner,
   model_guardrails, artifact_helpers, stonkmode, etc.) just received
   their dual-key fallback fixes for CDM/legacy/compact summary shapes.
   Don't re-litigate.
3. **`src/ic_engine/rendering/*`** — EOD email template, FA discussion
   render, PWA dashboard (charts.js, app.js, dashboard-integrated.html).
   Schema-aware percentage rendering and shape variants are dialed in.
4. **`src/ic_engine/config/schema.py`** — `normalize_portfolio()`. CDM
   normalization is stable; v2.4.6 added one careful idempotence
   change.
5. **`src/ic_engine/models/*`** — CDM dataclasses. No reason to touch
   these for ETLANTIS work.
6. **`src/ic_engine/services/{portfolio_utils,summary_utils}.py`** —
   summary normalization helpers. New in v2.4.6, used by all consumers.

If ETLANTIS code touches any of these, it expands scope beyond the
integration boundary — re-review across the full consumer surface
becomes necessary.

---

## 5. Version + release discipline

### Version files (must stay aligned)

| File | Format |
|---|---|
| `pyproject.toml` | `version = "X.Y.Z"` |
| `src/ic_engine/__init__.py` | `__version__ = "X.Y.Z"` |
| `uv.lock` | refreshed via `uv lock` after pyproject bump |

The harness contract gate (`harness/contract_check.py`) does NOT enforce
ic-engine version alignment beyond pyproject + `__init__.py`. The
adapter repos (InvestorClaw, InvestorClaude) have their own gates that
also check SKILL.toml / openclaw.plugin.json / package.json /
plugin.json — those run in those repos' CI.

### Release flow

1. Bump `pyproject.toml` + `src/ic_engine/__init__.py`
2. `uv lock`
3. `uv run pytest && uv run ruff check . && uv run black --check src/ tests/`
4. Run codex `adversarial-review` per directive 7
5. Commit with `release: ic-engine vX.Y.Z (...)`
6. Tag `vX.Y.Z`
7. Push to `gitlab` and `origin` (argonas via sshpass-root)
8. **Skip github until 2026-04-30** (per fleet policy)
9. Adapter repos (InvestorClaw, InvestorClaude) bump their `ic-engine`
   git pin in pyproject and ship corresponding patch versions

### Codex review (directive 7)

Every non-trivial commit goes through `codex review --uncommitted`
before push. If the review returns `needs-attention`, delegate the FIX
to codex itself via `codex exec --sandbox workspace-write` — do NOT
hand-implement findings. Codex has in-place iteration authority within
a single execution session. Loop until verdict = `approve`.

The v2.4.6 cycle ran 21 review rounds before reaching APPROVE — that's
the upper bound to expect for a comprehensive sweep. Surgical fixes
typically converge in 2-4 rounds.

---

## 6. Fleet remote convention

| Remote | Role | URL | Auth |
|---|---|---|---|
| `origin` | Source of truth | `<your-internal-git-host>:/path/to/ic-engine.git` | sshpass root |
| `gitlab` | CI/CD canonical | `https://gitlab.com/perlowja/ic-engine.git` | personal PAT |
| `github` | OSS surface (deferred to 2026-04-30+) | `https://github.com/perlowja/ic-engine.git` | personal PAT |

Author email: `Jason Perlow <jperlow@gmail.com>` — never an employer
email.

---

## 7. CI trigger model

All four fleet repos (clio, ic-engine, InvestorClaw, InvestorClaude)
use the same `workflow:rules` trigger gate as of 2026-04-27:

```yaml
workflow:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push"'
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
    - if: '$CI_COMMIT_TAG'
```

Per-job `rules:` are reserved for jobs that need a stricter scope
(e.g., ic-engine's `v13-harness` is main+tag only because it's heavy).

When ETLANTIS code lands, ETLANTIS-side CI should also adopt this gate
for fleet consistency. cleanroom can either standardize this in the
ETLANTIS repo or wait for a separate fleet-CI sweep.

---

## 8. Pre-existing test coverage that ETLANTIS code must not break

Tests that gate the integration surface:

```
tests/test_pipeline.py                   # 28 tests — pipeline.py orchestration
tests/test_cdm_consumer_fallbacks.py     # 11 tests — dual-key fallbacks
tests/test_normalization.py              # CDM normalization invariants
src/ic_engine/internal/test_data_stages.py  # data stage unit tests
```

Total v2.4.6 test count: 732 pass, 3 skipped. ETLANTIS integration must
keep this green.

---

## 9. Open questions for the integration step

cleanroom's stage-6 work + the eventual ETLANTIS integration commit
should resolve these:

1. **Pattern A vs B vs C** — pick one (see §3). My read: Pattern B
   first, with eventual Pattern A migration when ETLANTIS ships its
   first stable public release.
2. **fleet_provider lift** — does ic-engine need its own copy, or pull
   from etlantis as an extra dep? Today: not needed. When
   `litellm_consultation.py` grows multi-provider routing: yes, lift
   from etlantis.
3. **Pipeline.py wiring** — currently `run_pipeline()` does NOT use the
   data stages. The Phase-3 async orchestrator does. Decide whether to
   converge the two or leave the legacy path stable as the canonical
   CLI entry.
4. **clio promotion candidates** — list any stage primitives that are
   genuinely foundation-level (per Pattern C criteria). Move
   incrementally; don't promote in bulk.

---

## 10. Out-of-scope

- The InvestorClaw + InvestorClaude adapter repos. Their ic-engine pin
  bump for ETLANTIS-affecting changes is a one-line pyproject edit;
  cleanroom does not need to touch them.
- The clio repo unless choosing Pattern C.
- Any consumer-side rendering or commands code (see §4).

---

*Drafted 2026-04-27 by personal-claude STUDIO session as part of v2.4.6
ship + ETLANTIS readiness work. Update this doc when ETLANTIS integration
lands or when any §4 surface needs touching.*
