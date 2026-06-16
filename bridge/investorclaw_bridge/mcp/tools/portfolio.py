# SPDX-License-Identifier: Apache-2.0
"""Portfolio analysis tools — pure handlers + tool descriptors.

The functions here are transport-agnostic: they take primitive args
(strings/dicts), return primitive dicts, and do not reference FastMCP
or FastAPI directly. transport.py wires them to MCP @app.tool()
decorators (FastMCP) and FastAPI POST routes.

This separation mirrors v5 mnemos's mnemos/mcp/tools/{memory,dag,kg,...}.py
domain split — handlers are testable in isolation and can be re-wired to
a different transport (e.g., SSE) without touching tool logic.

Tool naming: `portfolio_<verb>` follows the canonical v4.0 dockerized-skill
naming convention (`<domain>_<action>`) for cross-runtime compatibility
with the agentic-cobol harness.
"""
from __future__ import annotations

import json
from typing import Any

from .._runtime import _run_ic_engine

# ──────────────────────────────────────────────────────────────────────
# Pure tool handlers (transport-agnostic)
# ──────────────────────────────────────────────────────────────────────


async def portfolio_ask(question: str) -> dict[str, Any]:
    """Natural-language portfolio question. Routes through ic-engine's
    deterministic pipeline, returns the structured ic_result envelope
    plus narrative text body.

    ``--no-refresh`` is passed so the engine uses the cached envelope and
    only runs the narrator — it does not trigger a per-question news/data
    pipeline run (TTL=30s for news). Without this flag, each ask spawns a
    yfinance + LLM refresh subprocess that accumulates into zombie processes
    when the narrative provider is slow or unavailable. Explicit data
    freshness is handled by ``portfolio_refresh`` / the Regenerate button.
    The old note warning that ``--no-refresh`` suppressed routing is no
    longer valid — current ic-engine routes correctly from the cached
    envelope.

    Each successful response is auto-persisted to MNEMOS (best-effort) so
    the user can search history, retrieve by serial number (run_id), flag
    bad ones, and delete failures — see portfolio_response_* tools. The
    persisted mem_id is attached to the response under `mnemos_mem_id`
    when storage succeeds.
    """
    result = await _run_ic_engine(["ask", "--no-refresh", question])
    # Best-effort MNEMOS persistence; never raises, never blocks the response.
    try:
        from .responses import persist_response
        stored = persist_response(
            question=question,
            narrative=result.get("narrative") or "",
            ic_result=result.get("ic_result"),
            duration_ms=None,
        )
        if stored and stored.get("id"):
            result["mnemos_mem_id"] = stored["id"]
    except Exception:
        # MNEMOS optional — silent failure, response still goes back to caller.
        pass
    return result


async def portfolio_holdings() -> dict[str, Any]:
    """Current holdings snapshot — positions, values, weights, account hierarchy.

    Runs the DETERMINISTIC ``holdings`` command (fetch_holdings.py), which reads
    the authoritative holdings file and returns a compact JSON of total/net
    value, top equities (symbol, value, weight, gain/loss), sector weights, top
    bonds, and per-account breakdown. Deliberately NOT the narrative ``ask``
    path — ``ask`` refuses holdings questions whose data isn't in the cached
    envelope ("can't answer without making up numbers"), which left callers with
    no holdings at all. This path always returns the real positions.
    """
    return await _run_ic_engine(["holdings"])


async def portfolio_performance() -> dict[str, Any]:
    """Deterministic portfolio performance & risk: Sharpe ratio, Sortino, volatility, max drawdown, returns, top/bottom performers. Use for 'how am I doing', 'Sharpe', 'risk', 'drawdown', 'volatility', 'performance metrics'.

    Deterministic: runs the engine `performance` command (a dedicated section
    materializer), never the narrative `ask` path — so it returns real signed
    data instead of an anti-fabrication refusal.
    """
    return await _run_ic_engine(["performance"], timeout_sec=180.0)

async def portfolio_analyst() -> dict[str, Any]:
    """Wall Street analyst consensus per holding: ratings, price targets, upside. Use for 'what do analysts think', 'analyst ratings', 'price targets', 'buy/sell/hold'.

    Deterministic: runs the engine `analyst` command (a dedicated section
    materializer), never the narrative `ask` path — so it returns real signed
    data instead of an anti-fabrication refusal.
    """
    return await _run_ic_engine(["analyst"], timeout_sec=180.0)

async def portfolio_optimize() -> dict[str, Any]:
    """Portfolio optimization (Modern Portfolio Theory): Sharpe-max, min-volatility, and target-return allocations on the efficient frontier. Use for 'optimal allocation', 'maximize my Sharpe', 'minimum-volatility allocation', 'efficient frontier'.

    Deterministic: runs the engine `optimize` command (a dedicated section
    materializer), never the narrative `ask` path — so it returns real signed
    data instead of an anti-fabrication refusal.
    """
    return await _run_ic_engine(["optimize"], timeout_sec=180.0)

async def portfolio_rebalance() -> dict[str, Any]:
    """Rebalancing analysis: current vs target allocation with a trade list and capital-gains / tax impact. Use for 'should I rebalance', 'rebalance with tax', 'target allocation', 'how to rebalance'.

    Deterministic: runs the engine `rebalance` command (a dedicated section
    materializer), never the narrative `ask` path — so it returns real signed
    data instead of an anti-fabrication refusal.
    """
    return await _run_ic_engine(["rebalance"], timeout_sec=180.0)

async def portfolio_bonds() -> dict[str, Any]:
    """Fixed-income analytics: bond exposure, yield-to-maturity, duration, convexity, coupons, maturities, laddering. Use for 'my bonds', 'YTM', 'bond strategy', 'fixed income', 'bond ladder', 'duration'.

    Deterministic: runs the engine `bonds` command (a dedicated section
    materializer), never the narrative `ask` path — so it returns real signed
    data instead of an anti-fabrication refusal.
    """
    return await _run_ic_engine(["bonds"], timeout_sec=180.0)

async def portfolio_cashflow() -> dict[str, Any]:
    """Projected cash-flow calendar: upcoming dividends, bond coupons, and maturities. Use for 'cash flow', 'dividends', 'income', 'coupons next quarter', 'distributions'.

    Deterministic: runs the engine `cashflow` command (a dedicated section
    materializer), never the narrative `ask` path — so it returns real signed
    data instead of an anti-fabrication refusal.
    """
    return await _run_ic_engine(["cashflow"], timeout_sec=180.0)

async def portfolio_eod() -> dict[str, Any]:
    """End-of-day portfolio report: daily summary, P&L, movers, and index closes. Use for 'EOD report', 'daily summary', 'end of day', 'todays report'.

    Deterministic: runs the engine `eod` command (a dedicated section
    materializer), never the narrative `ask` path — so it returns real signed
    data instead of an anti-fabrication refusal.
    """
    return await _run_ic_engine(["eod"], timeout_sec=180.0)

async def portfolio_news() -> dict[str, Any]:
    """News correlated to holdings: recent headlines per position plus broad market news. Use for 'news on my holdings', 'mergers or M&A today', 'market news', 'crypto news', 'whats happening in markets', 'dollar/forex news'.

    Deterministic: runs the engine `news` command (a dedicated section
    materializer), never the narrative `ask` path — so it returns real signed
    data instead of an anti-fabrication refusal.
    """
    return await _run_ic_engine(["news"], timeout_sec=180.0)


async def portfolio_performance_window(
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Deterministic per-holding + portfolio return/P&L over a time window.

    Use for ANY temporal/historical performance query. This path passes
    explicit parameters to ic-engine and does not depend on NL narration.
    """
    args = ["performance-window"]
    if period:
        args.extend(["--period", period])
    if start_date:
        args.extend(["--start", start_date])
    if end_date:
        args.extend(["--end", end_date])
    result = await _run_ic_engine(args, timeout_sec=1800.0)
    narrative = result.get("narrative") or ""
    if isinstance(narrative, str):
        for line in reversed(narrative.splitlines()):
            raw = line.strip()
            if raw.startswith("{") and '"sections"' in raw and '"ic_result"' in raw:
                try:
                    envelope = json.loads(raw)
                    if isinstance(envelope, dict):
                        result.clear()
                        result.update(envelope)
                    break
                except json.JSONDecodeError:
                    pass
    return result


async def portfolio_market_snapshot(
    symbols: str | None = None,
    benchmarks: bool = True,
) -> dict[str, Any]:
    """Real-time market snapshot: current price + day change % for the user's
    holdings plus whole-market benchmarks (SPX/NDX/DJI/VIX, BTC/ETH).

    Use for INTRADAY / "right now" / "how are my holdings doing today" / "are any
    of my positions up or down X% today" / market-level checks. Provider-agnostic
    (engine-owned quote chain); the agent must NOT shell out to a vendor API.
    Cached ~30s so repeated scans don't re-poll providers.
    """
    args = ["market-snapshot"]
    if symbols:
        args.extend(["--symbols", symbols])
    if not benchmarks:
        args.append("--no-benchmarks")
    result = await _run_ic_engine(args, timeout_sec=120.0)
    narrative = result.get("narrative") or ""
    if isinstance(narrative, str):
        for line in reversed(narrative.splitlines()):
            raw = line.strip()
            if raw.startswith("{") and '"sections"' in raw and '"ic_result"' in raw:
                try:
                    envelope = json.loads(raw)
                    if isinstance(envelope, dict):
                        result.clear()
                        result.update(envelope)
                    break
                except json.JSONDecodeError:
                    pass
    return result


async def portfolio_refresh() -> dict[str, Any]:
    """Refresh market data without re-uploading portfolio files.

    Re-runs the ic-engine refresh pipeline against current portfolio files
    in /data/portfolios/. Pulls fresh prices via yfinance / FRED / Finnhub.
    Large portfolios (200+ positions) need ~3-5min on a cold yfinance cache,
    so timeout matches the broader subprocess default (600s).
    """
    from ...serve import _run_with_sweep_lock

    return await _run_with_sweep_lock(
        lambda: _run_ic_engine(["refresh"], timeout_sec=1800.0)
    )


async def portfolio_setup() -> dict[str, Any]:
    """Auto-discover portfolio files in /data/portfolios/.

    Use on first run or after the user uploads a new portfolio file. Returns
    a summary of detected files (pdf/xls/csv) and engine readiness.
    """
    return await _run_ic_engine(["setup"])


# ──────────────────────────────────────────────────────────────────────
# Init telemetry — module-level state mutated by portfolio_initialize so
# any caller can poll readiness without invoking the slow init themselves.
# ──────────────────────────────────────────────────────────────────────


import time as _time

_INIT_STATE: dict[str, Any] = {
    "state": "not_started",          # not_started | initializing | ready | failed
    "started_at": None,              # epoch seconds when initialize began
    "completed_at": None,            # epoch seconds when initialize finished (success or fail)
    "current_stage": None,           # setup | refresh | seed_ask | None
    "stages_completed": [],          # list of {stage, exit_code, duration_ms, finished_at}
    "stages_total": 3,               # setup + refresh + seed_ask
    "elapsed_ms": 0,
    "last_error": None,              # str | None
    "ready": False,                  # convenience: state == "ready"
    "sweep_in_progress": False,
}


def get_init_state() -> dict[str, Any]:
    """Snapshot the current init state. Updated live by portfolio_initialize."""
    snapshot = dict(_INIT_STATE)
    snapshot["ready"] = _INIT_STATE["state"] == "ready"
    try:
        from ...serve import is_sweeping
        snapshot["sweep_in_progress"] = is_sweeping()
    except Exception:
        snapshot["sweep_in_progress"] = False
    if _INIT_STATE["started_at"]:
        end = _INIT_STATE["completed_at"] or _time.time()
        snapshot["elapsed_ms"] = int((end - _INIT_STATE["started_at"]) * 1000)
    return snapshot


def _set_init_state(**kwargs) -> None:
    """Mutate the module-level init state. Telemetry subscribers (status
    endpoint, healthz, MCP tool) read from get_init_state()."""
    _INIT_STATE.update(kwargs)
    if "state" in kwargs:
        _INIT_STATE["ready"] = kwargs["state"] == "ready"


async def portfolio_initialize_status() -> dict[str, Any]:
    """Return a live snapshot of the boot/init state. Agents should poll
    this and only fire portfolio_ask once `ready: true`. Cheap and
    side-effect-free — safe to call from a tight loop.
    """
    return get_init_state()


async def portfolio_initialize(seed_question: str | None = None) -> dict[str, Any]:
    """One-shot bootstrap: discover portfolio files, refresh all sections,
    optionally fire a seed ask to warm the LLM-narrative cache. After this
    returns success, every subsequent portfolio_ask within the section TTLs
    hits the warm envelope cache and returns in 1-3 seconds.

    Designed for first-run install paths (the agent or container boot sequence
    can call this once) and after manual portfolio file uploads.

    Returns dict with per-stage status, total elapsed time, and the run_ids
    of each underlying call (so the agent can retrieve any of them via
    portfolio_response_get for diagnostics).

    Args:
        seed_question: optional natural-language question to fire after
            refresh. Defaults to a holdings-flavored prompt that exercises
            the narrator path. Pass empty string to skip the seed ask.
    """
    import time

    overall_start = time.monotonic()
    epoch_start = time.time()
    stages: list[dict[str, Any]] = []
    will_seed = seed_question != ""
    stages_total = 3 if will_seed else 2

    _set_init_state(
        state="initializing",
        started_at=epoch_start,
        completed_at=None,
        current_stage=None,
        stages_completed=[],
        stages_total=stages_total,
        last_error=None,
    )

    def _record_stage(name: str, result: dict[str, Any], t0: float, extra: dict | None = None) -> dict:
        stage_record = {
            "stage": name,
            "exit_code": result.get("exit_code", -1),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "ic_result": (result.get("ic_result") or {}).get("ic_result", {}),
            "finished_at": time.time(),
        }
        if extra:
            stage_record.update(extra)
        stages.append(stage_record)
        completed = list(_INIT_STATE.get("stages_completed", [])) + [stage_record]
        _set_init_state(stages_completed=completed, current_stage=None)
        if stage_record["exit_code"] != 0:
            _set_init_state(
                last_error=f"{name} exited {stage_record['exit_code']}",
            )
        return stage_record

    try:
        # Stage 1: setup — auto-discover any portfolio files in /data/portfolios.
        _set_init_state(current_stage="setup")
        t0 = time.monotonic()
        setup_result = await _run_ic_engine(["setup"])
        _record_stage("setup", setup_result, t0)

        # Stage 2: refresh — populate envelope.sections.* (holdings, performance,
        # bonds, analyst, news, synthesize, optimize, cashflow, peer). Large
        # portfolios may take 5-15min cold.
        _set_init_state(current_stage="refresh")
        t0 = time.monotonic()
        refresh_result = await _run_ic_engine(["refresh"], timeout_sec=1800.0)
        _record_stage("refresh", refresh_result, t0)

        # Stage 3: seed ask — warms the narrator path so the very first user
        # ask doesn't hit a cold LLM connection. Skip if caller passed empty
        # string.
        if will_seed:
            _set_init_state(current_stage="seed_ask")
            prompt = seed_question or "What is in my portfolio? Top 3 positions."
            t0 = time.monotonic()
            ask_result = await _run_ic_engine(["ask", prompt], timeout_sec=600.0)
            _record_stage(
                "seed_ask", ask_result, t0,
                extra={"narrative_chars": len(ask_result.get("narrative") or "")},
            )
    except Exception as exc:
        _set_init_state(
            state="failed",
            completed_at=time.time(),
            current_stage=None,
            last_error=f"{type(exc).__name__}: {exc}",
        )
        raise

    final_state = "ready" if all(s.get("exit_code") == 0 for s in stages) else "failed"
    _set_init_state(state=final_state, completed_at=time.time(), current_stage=None)

    return {
        "initialized": final_state == "ready",
        "ready": final_state == "ready",
        "state": final_state,
        "total_duration_ms": int((time.monotonic() - overall_start) * 1000),
        "stages": stages,
        "next_step": (
            "Subsequent portfolio_ask calls will hit the warm envelope cache "
            "(1-3s) until the per-section TTL expires (5-10 min depending "
            "on section). Poll portfolio_initialize_status to see live state."
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# Tool descriptors — registry shape mirrors v5 mnemos
# ──────────────────────────────────────────────────────────────────────


def _tool(description: str, parameters: dict, required: list[str], handler) -> dict:
    """Mirror of v5 mnemos `_tool()` factory — produces the registry entry shape."""
    return {
        "description": description,
        "parameters": parameters,
        "required": required,
        "handler": handler,
    }


TOOLS: dict[str, dict[str, Any]] = {
    "portfolio_ask": _tool(
        description=(
            "PRIMARY TOOL — call this for ANY portfolio question. The "
            "container auto-initialized at boot, so the data cache is "
            "already warm: holdings, performance, bonds, analyst ratings, "
            "news (per-symbol + general/forex/crypto/merger categories), "
            "synthesis, optimization, cashflow projections, peer analysis, "
            "and Treasury yield curve are ALL pre-loaded into the envelope. "
            "Just call portfolio_ask with the user's question verbatim — "
            "you do NOT need to call portfolio_setup, portfolio_refresh, or "
            "portfolio_initialize first. The engine routes deterministically "
            "and the narrator returns a verified natural-language answer "
            "with envelope-quoted numbers (no hallucination)."
        ),
        parameters={
            "question": {
                "type": "string",
                "description": (
                    "The user's portfolio question, e.g. 'What is in my "
                    "portfolio?', 'How is performance?', 'What are my biggest "
                    "tech holdings?'"
                ),
            },
        },
        required=["question"],
        handler=portfolio_ask,
    ),
    "portfolio_holdings": _tool(
        description=(
            "Get the current portfolio holdings snapshot — positions, values, "
            "weights, account hierarchy as structured data."
        ),
        parameters={},
        required=[],
        handler=portfolio_holdings,
    ),
    "portfolio_performance": _tool(
        description=(
            "Deterministic portfolio performance & risk: Sharpe ratio, Sortino, volatility, max drawdown, returns, top/bottom performers. Use for 'how am I doing', 'Sharpe', 'risk', 'drawdown', 'volatility', 'performance metrics'."
        ),
        parameters={},
        required=[],
        handler=portfolio_performance,
    ),
    "portfolio_analyst": _tool(
        description=(
            "Wall Street analyst consensus per holding: ratings, price targets, upside. Use for 'what do analysts think', 'analyst ratings', 'price targets', 'buy/sell/hold'."
        ),
        parameters={},
        required=[],
        handler=portfolio_analyst,
    ),
    "portfolio_optimize": _tool(
        description=(
            "Portfolio optimization (Modern Portfolio Theory): Sharpe-max, min-volatility, and target-return allocations on the efficient frontier. Use for 'optimal allocation', 'maximize my Sharpe', 'minimum-volatility allocation', 'efficient frontier'."
        ),
        parameters={},
        required=[],
        handler=portfolio_optimize,
    ),
    "portfolio_rebalance": _tool(
        description=(
            "Rebalancing analysis: current vs target allocation with a trade list and capital-gains / tax impact. Use for 'should I rebalance', 'rebalance with tax', 'target allocation', 'how to rebalance'."
        ),
        parameters={},
        required=[],
        handler=portfolio_rebalance,
    ),
    "portfolio_bonds": _tool(
        description=(
            "Fixed-income analytics: bond exposure, yield-to-maturity, duration, convexity, coupons, maturities, laddering. Use for 'my bonds', 'YTM', 'bond strategy', 'fixed income', 'bond ladder', 'duration'."
        ),
        parameters={},
        required=[],
        handler=portfolio_bonds,
    ),
    "portfolio_cashflow": _tool(
        description=(
            "Projected cash-flow calendar: upcoming dividends, bond coupons, and maturities. Use for 'cash flow', 'dividends', 'income', 'coupons next quarter', 'distributions'."
        ),
        parameters={},
        required=[],
        handler=portfolio_cashflow,
    ),
    "portfolio_eod": _tool(
        description=(
            "End-of-day portfolio report: daily summary, P&L, movers, and index closes. Use for 'EOD report', 'daily summary', 'end of day', 'todays report'."
        ),
        parameters={},
        required=[],
        handler=portfolio_eod,
    ),
    "portfolio_news": _tool(
        description=(
            "News correlated to holdings: recent headlines per position plus broad market news. Use for 'news on my holdings', 'mergers or M&A today', 'market news', 'crypto news', 'whats happening in markets', 'dollar/forex news'."
        ),
        parameters={},
        required=[],
        handler=portfolio_news,
    ),
    "portfolio_performance_window": _tool(
        description=(
            "Deterministic per-holding + portfolio return/P&L and top movers "
            "over an explicit time window (period like 1w/1mo/3mo/ytd/1y/max, "
            "or start_date/end_date). Use for ANY 'last week / last month / "
            "past X / since DATE / historical' question — does not depend on narration."
        ),
        parameters={
            "period": {
                "type": "string",
                "description": "Optional period token: 1d, 1w, 2w, 1mo, 3mo, 6mo, ytd, 1y, 2y, max.",
            },
            "start_date": {
                "type": "string",
                "description": "Optional ISO YYYY-MM-DD inclusive start date; use for 'since DATE'.",
            },
            "end_date": {
                "type": "string",
                "description": "Optional ISO YYYY-MM-DD inclusive end date; defaults to engine EOD.",
            },
        },
        required=[],
        handler=portfolio_performance_window,
    ),
    "portfolio_market_snapshot": _tool(
        description=(
            "Real-time market snapshot — current price + day change% for the "
            "user's holdings plus benchmarks (SPX/NDX/DJI/VIX, BTC/ETH). Use for "
            "INTRADAY / right-now / 'how are my holdings doing today' / 'any "
            "positions up or down X% today' / market-level checks. Provider-"
            "agnostic and engine-owned; do NOT shell out to a vendor API."
        ),
        parameters={
            "symbols": {
                "type": "string",
                "description": "Optional comma-separated tickers (overrides holdings).",
            },
            "benchmarks": {
                "type": "boolean",
                "description": "Include index/crypto benchmarks (default true).",
            },
        },
        required=[],
        handler=portfolio_market_snapshot,
    ),
    "portfolio_refresh": _tool(
        description=(
            "ADVANCED — force a fresh data pull. The container already auto-"
            "refreshes stale sections (TTLs 30s-300s) on every portfolio_ask, "
            "so you almost never need this. Call only when the user "
            "explicitly asks for a manual refresh, OR after they upload a "
            "new portfolio file via /api/portfolio/setup."
        ),
        parameters={},
        required=[],
        handler=portfolio_refresh,
    ),
    "portfolio_setup": _tool(
        description=(
            "ADVANCED — auto-discover portfolio files in /data/portfolios/. "
            "Already runs at container boot. Call only after the user uploads "
            "a NEW portfolio file mid-session. For everyday use just call "
            "portfolio_ask."
        ),
        parameters={},
        required=[],
        handler=portfolio_setup,
    ),
    "portfolio_initialize_status": _tool(
        description=(
            "POLL THIS BEFORE FIRST ASK. Returns the current init state "
            "(not_started | initializing | ready | failed) plus per-stage "
            "progress (setup, refresh, seed_ask) with exit codes and "
            "elapsed time. The container auto-initializes at boot, so on "
            "first connection you should poll this until `ready: true` "
            "before firing portfolio_ask. Cheap and side-effect-free — "
            "safe to call in a tight loop. Typical cold init takes 5-15 "
            "minutes on a 200+ position portfolio."
        ),
        parameters={},
        required=[],
        handler=portfolio_initialize_status,
    ),
    "portfolio_initialize": _tool(
        description=(
            "One-shot bootstrap: setup + refresh + optional seed ask. Returns "
            "after the envelope cache is fully populated, so every subsequent "
            "portfolio_ask hits the warm cache and answers in 1-3 seconds. "
            "Call this once after install, after uploading new portfolio "
            "files, or whenever you want to force a fresh data pull. The "
            "container can also auto-initialize at boot via env flag "
            "IC_INITIALIZE_ON_BOOT=1."
        ),
        parameters={
            "seed_question": {
                "type": "string",
                "description": (
                    "Optional natural-language question to fire after refresh "
                    "to warm the narrator/LLM cache. Defaults to a holdings "
                    "prompt; pass empty string to skip."
                ),
            },
        },
        required=[],
        handler=portfolio_initialize,
    ),
}
