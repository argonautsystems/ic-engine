# SPDX-License-Identifier: Apache-2.0
"""Entry point for the InvestorClaw v4.0 bridge.

Two listeners, one Python process:
  :8090  — MCP-HTTP server (FastMCP) — agent-facing tool surface
  :8092  — Dashboard web UI + REST + /healthz — user-facing config UI

Both ports share ic-engine session, sqlite db, and MnemosClient instance.

For the v4.0 beta pilot (24h timeline as of 2026-05-01):
  - MCP-HTTP at :8090 is load-bearing — at least 4 tools registered
    (portfolio_ask, portfolio_holdings, portfolio_refresh, portfolio_setup)
  - Dashboard at :8092 is minimal — placeholder static page + /healthz
  - mnemos-rs sibling container is OPTIONAL (not blocking for beta)
"""
from __future__ import annotations

import logging
import math
import os
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import asyncio
import structlog


logger = structlog.get_logger("investorclaw_bridge.serve")
_sweep_lock = asyncio.Lock()
_sweep_in_progress = False


def is_sweeping() -> bool:
    """Return whether a regenerate/refresh sweep owns the process-wide lock."""
    return _sweep_in_progress


def _parse_stale_hours(raw: str | None = None, caller: str = "auto_initialize") -> float:
    """Parse IC_SECTION_STALE_HOURS, falling back to the safe 24h default.

    `caller` identifies the call site in log output so operators can
    distinguish startup-time vs runtime invocations.
    """
    raw_value = os.environ.get("IC_SECTION_STALE_HOURS", "24") if raw is None else raw
    def _warn(reason: str) -> None:
        logger.warning(
            "bridge.section_freshness.invalid_stale_hours",
            raw=raw_value,
            fallback=24.0,
            reason=reason,
            caller=caller,
        )
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        _warn("parse_failed")
        return 24.0
    if value < 0.0:
        _warn("negative")
        return 24.0
    if value == 0.0:
        _warn("zero")
        return 24.0
    if math.isinf(value) or math.isnan(value):
        _warn("non_finite")
        return 24.0
    return value


def _sweep_already_running_result() -> dict[str, Any]:
    sections = [
        "setup", "refresh", "performance", "bonds", "analyst", "news",
        "whatchanged", "scenario", "optimize", "rebalance", "cashflow",
        "peer", "markets", "synthesize",
    ]
    status = {"status": "already_running", "skipped": True}
    return {section: dict(status) for section in sections}


async def _run_with_sweep_lock(
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    global _sweep_in_progress
    if _sweep_lock.locked():
        logger.info("regenerate_sweep.already_running")
        return _sweep_already_running_result()
    await _sweep_lock.acquire()
    _sweep_in_progress = True
    try:
        return await operation()
    finally:
        _sweep_in_progress = False
        _sweep_lock.release()


async def _regenerate_sweep(
    run_ic_engine: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run the full data-refresh + analyzer sweep that backs every tab.

    Sequenced so a failing section doesn't abort the rest. Engine sections
    that don't write JSON (or aren't installed) just no-op.
    """
    if run_ic_engine is None:
        from .mcp._runtime import _run_ic_engine as run_ic_engine

    # Map section command → expected report filename under /data/reports/.
    # Sections that already write their own file are omitted; entries here
    # cause the bridge to write the stdout envelope when the engine doesn't.
    _SECTION_REPORT_FILES: dict[str, str] = {
        "optimize":  "optimize.json",
        "rebalance": "rebalance.json",
        "cashflow":  "cashflow.json",
        "peer":      "peer.json",
        "markets":   "markets.json",
        "synthesize": "portfolio_analysis.json",
    }

    import json as _json
    import pathlib as _pathlib

    _reports_dir = _pathlib.Path(os.environ.get("IC_REPORTS_DIR", "/data/reports"))

    def _persist_section(sec: str, result: dict[str, Any]) -> None:
        """Write the engine stdout to the expected report file if missing."""
        fname = _SECTION_REPORT_FILES.get(sec)
        if not fname:
            return
        dest = _reports_dir / fname
        stdout = result.get("stdout", "")
        if not stdout:
            return

        # 1. Try to parse full stdout as JSON (handles multi-line JSON like optimize.py output)
        stdout_stripped = stdout.strip()
        # Find first { ... } block by locating the JSON object before any ic_result trailer
        for boundary_kw in ['{"ic_result"', "ic_result"]:
            if boundary_kw in stdout_stripped:
                candidate = stdout_stripped[:stdout_stripped.rfind(boundary_kw)].rstrip()
                if candidate.endswith(","):
                    candidate = candidate[:-1].rstrip()
                try:
                    data = _json.loads(candidate)
                    if isinstance(data, dict) and "ic_result" not in data:
                        _reports_dir.mkdir(parents=True, exist_ok=True)
                        dest.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
                        return
                except Exception:
                    pass

        # 2. Try parsing whole stdout as JSON (no ic_result present)
        try:
            data = _json.loads(stdout_stripped)
            if isinstance(data, dict) and "ic_result" not in data:
                _reports_dir.mkdir(parents=True, exist_ok=True)
                dest.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
                return
        except Exception:
            pass

        # 3. Extract the first valid single-line JSON object from stdout (legacy format).
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{") and '"ic_result"' not in line:
                try:
                    data = _json.loads(line)
                    _reports_dir.mkdir(parents=True, exist_ok=True)
                    dest.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    return
                except Exception:
                    pass
        # Fallback: write full stdout as a JSON string.
        try:
            _reports_dir.mkdir(parents=True, exist_ok=True)
            dest.write_text(_json.dumps({"raw": stdout}), encoding="utf-8")
        except Exception:
            pass

    async def _run() -> dict[str, Any]:
        results: dict[str, Any] = {}
        # Setup is fast and idempotent — re-discovers any newly uploaded files.
        results["setup"] = await run_ic_engine(["setup"], timeout_sec=300.0)
        # The big refresh — pulls fresh prices for every position.
        results["refresh"] = await run_ic_engine(["refresh"], timeout_sec=1800.0)
        # Per-section analyzers. Each writes its own JSON under /data/reports/.
        # Bridge persists stdout for sections that don't self-write.
        sections = [
            "performance", "bonds", "analyst", "news", "whatchanged",
            "scenario", "optimize", "rebalance", "cashflow", "peer",
            "markets", "synthesize",
        ]
        for sec in sections:
            try:
                result = await run_ic_engine([sec], timeout_sec=900.0)
                results[sec] = result
                _persist_section(sec, result)
            except Exception as e:  # noqa: BLE001
                results[sec] = {"error": f"{type(e).__name__}: {e}"}
        return results

    return await _run_with_sweep_lock(_run)


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


def _bind_addr_to_host_port(bind: str) -> tuple[str, int]:
    """Parse a bind string like '0.0.0.0:8090' into (host, port)."""
    if ":" not in bind:
        raise ValueError(f"Invalid bind address {bind!r} — expected HOST:PORT")
    host, port_str = bind.rsplit(":", 1)
    return host, int(port_str)


def main() -> int:
    """Start the bridge.

    Loads ic-engine bridge tooling + starts FastMCP at IC_MCP_BIND and
    a tiny FastAPI app at IC_DASHBOARD_BIND for /healthz + dashboard
    static files. Both run in the same uvicorn process via mounted
    sub-apps (one ASGI app at root, another mounted at /mcp).
    """
    _configure_logging()
    logger = structlog.get_logger("investorclaw_bridge.serve")

    # Defer heavy imports until after logging is configured (so import
    # errors get logged with structlog).
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
        import uvicorn
    except ImportError as e:
        logger.error("bridge.import_error", missing=str(e))
        sys.stderr.write(
            f"FATAL: missing required dependency: {e}. "
            f"Did you install via `pip install -e bridge/`?\n"
        )
        return 2

    try:
        # FastMCP — provides MCP-HTTP transport
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        # FastMCP isn't packaged yet on all systems; for beta we can
        # serve a minimal /mcp endpoint manually if FastMCP is missing.
        FastMCP = None  # type: ignore[assignment]
        logger.warning(
            "bridge.fastmcp_missing",
            note="MCP server will be a placeholder; install `mcp` package for full functionality",
        )

    from . import mcp_server

    # Load /data/keys.env into os.environ so ic-engine subprocesses inherit
    # the keys (FINNHUB_KEY, MASSIVE_API_KEY, ALPHA_VANTAGE_KEY, NEWSAPI_KEY,
    # TOGETHER/INVESTORCLAW_NARRATIVE_API_KEY, etc.). The key_resolver module
    # parses + validates mode 0600; we only set keys not already in env so
    # operator-set ENV (Dockerfile / compose) wins over file values.
    from pathlib import Path
    from . import key_resolver

    keys_path = Path(os.environ.get("IC_KEYS_FILE", "/data/keys.env"))
    try:
        loaded = key_resolver.load_keys_env(keys_path)
        added = 0
        for k, v in loaded.items():
            if k not in os.environ:
                os.environ[k] = v
                added += 1
        logger.info(
            "bridge.keys.loaded",
            path=str(keys_path),
            in_file=len(loaded),
            added_to_env=added,
            keys=sorted(loaded.keys()),
        )
    except Exception as e:
        logger.warning(
            "bridge.keys.load_failed",
            path=str(keys_path),
            error=f"{type(e).__name__}: {e}",
        )

    mcp_bind = os.environ.get("IC_MCP_BIND", "0.0.0.0:8090")
    dashboard_bind = os.environ.get("IC_DASHBOARD_BIND", "0.0.0.0:8092")
    mnemos_base = os.environ.get("MNEMOS_BASE", "http://mnemos:5002")

    logger.info(
        "bridge.start",
        mcp_bind=mcp_bind,
        dashboard_bind=dashboard_bind,
        mnemos_base=mnemos_base,
        portfolio_dir=os.environ.get("IC_PORTFOLIO_DIR", "/data/portfolios"),
        engine_portfolio_dir=os.environ.get("INVESTOR_CLAW_PORTFOLIO_DIR", "(unset)"),
    )

    # ── Build the dashboard FastAPI app ────────────────────────────────
    dashboard_app = FastAPI(title="InvestorClaw v4.0", version="4.0.0a1")

    @dashboard_app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Health includes init state so agents polling /healthz can also
        # gate on `init_state == "ready"` without an extra round-trip.
        from .mcp.tools import get_init_state as _get_init_state
        h = mcp_server.health_check()
        snap = _get_init_state()
        h["init_state"] = snap["state"]
        h["init_ready"] = snap["ready"]
        h["init_current_stage"] = snap["current_stage"]
        h["init_elapsed_ms"] = snap["elapsed_ms"]
        h["sweep_in_progress"] = is_sweeping()
        return JSONResponse(h)

    @dashboard_app.get("/api/version")
    async def version() -> JSONResponse:
        return JSONResponse({
            "version": os.environ.get("IC_ENGINE_VERSION", "unknown"),
            "service": "investorclaw-bridge",
        })

    # Wire the first-run setup API + bare-metal HTML form
    # (per GRAEAE 2026-05-01 — keep pilots in browser, out of nano)
    from . import setup_api
    setup_api.attach_to(dashboard_app)

    # Mount static dashboard files at /static (root is taken by setup redirect)
    dashboard_dir = "/opt/ic-engine/dashboard"
    if os.path.isdir(dashboard_dir):
        dashboard_app.mount(
            "/static", StaticFiles(directory=dashboard_dir, html=True), name="dashboard"
        )

    # Mount /reports → /data/reports so users can browse generated EOD HTML
    # files, JSON snapshots, etc. directly via http://localhost:18092/reports/
    # The reports dir is the bind-mounted ./reports/ on the host.
    reports_dir = os.environ.get("IC_REPORTS_DIR", "/data/reports")
    if os.path.isdir(reports_dir):
        dashboard_app.mount(
            "/reports",
            StaticFiles(directory=reports_dir, html=True),
            name="reports",
        )

    # Tabbed dashboard at / + per-section views at /dashboard/<tab>
    # — mounted last so it doesn't get preempted by other root-route registrations.
    from . import dashboard
    from .mcp.tools.portfolio import get_init_state as _get_init_state
    from .mcp.tools.keys import (
        portfolio_keys_status,
        portfolio_keys_set,
        portfolio_keys_recommend,
        portfolio_keys_delete,
    )
    from .mcp.tools.keys_backup import (
        portfolio_keys_backup,
        portfolio_keys_restore,
        portfolio_keys_backups_list,
    )
    from .mcp.tools.upgrade import portfolio_export, portfolio_import
    from .mcp._runtime import _run_ic_engine
    from .portfolio_templates import list_templates, apply_template
    from . import provider_routing as _provider_routing
    # Hydrate os.environ from the persisted routing file (if any) so the
    # first ic-engine subprocess inherits the dashboard-saved override.
    _provider_routing.hydrate_environ_from_file()
    from . import provider_diagnostics as _provider_diagnostics

    dashboard.attach_to(
        dashboard_app,
        get_init_state=_get_init_state,
        get_keys_status=portfolio_keys_status,
        set_key=lambda name, value: portfolio_keys_set({name: value}),
        regenerate=lambda: _regenerate_sweep(_run_ic_engine),
        get_keys_recommend=portfolio_keys_recommend,
        delete_key=portfolio_keys_delete,
        backup_keys=portfolio_keys_backup,
        restore_keys=portfolio_keys_restore,
        list_backups=portfolio_keys_backups_list,
        list_templates=list_templates,
        apply_template=apply_template,
        get_provider_routing=_provider_routing.load_routing,
        set_provider_routing=lambda primary, chain: _provider_routing.save_routing(
            primary=primary, fallback_chain=chain
        ),
        export_config=portfolio_export,
        import_config=portfolio_import,
        diagnostics_check=_provider_diagnostics.check_provider,
        diagnostics_supported=_provider_diagnostics.supported_providers,
    )

    # ── Build the MCP-HTTP app ────────────────────────────────────────
    # FastMCP exposes its own ASGI app via streamable_http_app(), but the
    # session manager inside it must be started via an async context
    # manager (`session_manager.run()`) — wired through FastAPI's lifespan
    # parameter. Mounting without the lifespan gives:
    #   RuntimeError: Task group is not initialized. Make sure to use run().
    # See FastMCP docs for the canonical lifespan-mount pattern.
    from contextlib import asynccontextmanager

    if FastMCP is not None:
        # MCP server's StreamableHTTP transport ships DNS-rebinding protection
        # (mcp.server.transport_security.TransportSecuritySettings, on by
        # default). With allowed_hosts=[] and protection on, every Host header
        # except 127.0.0.1/localhost is rejected. That breaks the v4.0
        # container-to-container path: an agent in a sibling container hits the
        # bridge over the docker bridge IP (172.17.0.1:18090 in default bridge
        # mode, or `<service-name>:8090` in compose's user-defined bridge) and
        # gets `Streamable HTTP error: Invalid Host header`.
        #
        # The convention (RFC §8) is localhost-only by default, but localhost
        # is the *host*, not the *container*'s view of the host. For the
        # in-cluster MCP path to work at all, we need to allow the container
        # bridge IPs and compose service names.
        #
        # Default: disable rebinding protection (this service is meant to be
        # reached by sibling containers + localhost; not exposed publicly).
        # Override via env: MCP_ALLOWED_HOSTS=host1,host2,... to keep
        # protection on with an explicit allowlist.
        from mcp.server.transport_security import TransportSecuritySettings
        _allowed_hosts = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
        if _allowed_hosts:
            _sec = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[h.strip() for h in _allowed_hosts.split(",") if h.strip()],
            )
        else:
            _sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        # stateless_http: no per-client session to expire on idle or to be
        # invalidated by an engine restart. A stateful streamable-http session
        # silently dropped the agent's tool registry after ~10-15 min idle (or
        # whenever ic-engine recreated), surfacing as "Unknown tool" in the agent
        # WITHOUT any request reaching the engine. Stateless makes every request
        # self-contained, so the MCP client always sees the live tool list.
        # json_response keeps responses plain JSON for the widest client compat.
        try:
            mcp_app = FastMCP(
                "investorclaw",
                transport_security=_sec,
                stateless_http=True,
                json_response=True,
            )
        except TypeError:
            # Older mcp SDK without stateless_http support — fall back to stateful.
            mcp_app = FastMCP("investorclaw", transport_security=_sec)
        mcp_server.register_tools(mcp_app)
        try:
            mcp_asgi = mcp_app.streamable_http_app()
        except AttributeError:
            try:
                mcp_asgi = mcp_app.sse_app()
            except AttributeError:
                logger.error(
                    "bridge.mcp_transport_unavailable",
                    note="FastMCP installed but no HTTP/SSE transport found; check mcp package version",
                )
                mcp_app = None
                mcp_asgi = None
    else:
        mcp_app = None
        mcp_asgi = None

    @asynccontextmanager
    async def mcp_lifespan(app):
        if mcp_app is not None and hasattr(mcp_app, "session_manager"):
            async with mcp_app.session_manager.run():
                yield
        else:
            yield

    # MCP-bound FastAPI app — mounts FastMCP at root so the inner /mcp path
    # becomes /mcp on the agent's URL. /healthz lives at root alongside.
    mcp_app_root = FastAPI(
        title="InvestorClaw MCP",
        version="4.0.0a1",
        lifespan=mcp_lifespan,
    )

    @mcp_app_root.get("/healthz")
    async def mcp_healthz() -> JSONResponse:
        from .mcp.tools import get_init_state as _get_init_state
        h = mcp_server.health_check()
        snap = _get_init_state()
        h["init_state"] = snap["state"]
        h["init_ready"] = snap["ready"]
        h["init_current_stage"] = snap["current_stage"]
        h["init_elapsed_ms"] = snap["elapsed_ms"]
        h["sweep_in_progress"] = is_sweeping()
        return JSONResponse(h)

    # Register REST wrappers BEFORE the FastMCP mount; FastAPI route
    # resolution prefers explicitly-registered routes over mounted apps,
    # so /api/portfolio/* takes precedence over the FastMCP catch-all.
    mcp_server.register_rest_routes(mcp_app_root)

    if mcp_asgi is not None:
        # Mount at root: FastMCP's internal /mcp path becomes the public /mcp.
        # This is the canonical path agents expect (see RFC §6.2 transport=http).
        mcp_app_root.mount("/", mcp_asgi)
    else:
        @mcp_app_root.get("/mcp")
        async def mcp_placeholder() -> JSONResponse:
            return JSONResponse(
                {
                    "error": "mcp_transport_unavailable",
                    "note": (
                        "FastMCP HTTP transport not initialized. "
                        "Install mcp>=1.4 or check ASGI integration."
                    ),
                },
                status_code=503,
            )

    # ── Run both listeners ─────────────────────────────────────────────
    # uvicorn can serve multiple ASGI apps via Config + Server multiplexing.
    # For simplicity in beta, we launch two uvicorn instances in asyncio.gather.
    import asyncio

    mcp_host, mcp_port = _bind_addr_to_host_port(mcp_bind)
    dash_host, dash_port = _bind_addr_to_host_port(dashboard_bind)

    mcp_config = uvicorn.Config(
        mcp_app_root, host=mcp_host, port=mcp_port, log_level="info"
    )
    dash_config = uvicorn.Config(
        dashboard_app, host=dash_host, port=dash_port, log_level="info"
    )
    mcp_uvicorn = uvicorn.Server(mcp_config)
    dash_uvicorn = uvicorn.Server(dash_config)

    # Boot-time auto-initialize so by the time any agent connects, the
    # envelope cache is already warm. The agent gets sub-second responses
    # on its very first portfolio_ask instead of the 5–15min cold-cache
    # path. Disable with IC_INITIALIZE_ON_BOOT=0.
    initialize_on_boot = os.environ.get("IC_INITIALIZE_ON_BOOT", "1").strip() not in ("0", "false", "no", "")

    async def _auto_initialize() -> None:
        # Brief delay so the listeners are up before we spawn engine subprocesses.
        await asyncio.sleep(2.0)
        try:
            from .mcp.tools.portfolio import portfolio_initialize
            logger.info("bridge.auto_initialize.start")
            result = await portfolio_initialize(seed_question="What is in my portfolio?")
            logger.info(
                "bridge.auto_initialize.done",
                ready=result.get("ready"),
                total_duration_ms=result.get("total_duration_ms"),
                stages=[s.get("stage") + ":" + str(s.get("exit_code")) for s in result.get("stages", [])],
            )
        except Exception as e:
            logger.warning("bridge.auto_initialize.failed", error=f"{type(e).__name__}: {e}")
            return

        # v4.4.0 — detect stale per-section JSONs and run the full
        # regenerate sweep in background. Without this, every cobol
        # prompt's narrator detects per-section staleness on first call
        # and triggers in-process refreshes — at ~60s × N sections,
        # any non-cached prompt blows the curl timeout. Sweep runs
        # async so bridge stays "ready" for incoming calls; sections
        # that complete during the sweep become available to subsequent
        # asks immediately.
        try:
            from . import section_freshness
            should_sweep, report = section_freshness.should_run_full_sweep(
                max_age_hours=_parse_stale_hours()
            )
            logger.info(
                "bridge.section_freshness.checked",
                stale_count=report.get("stale_count"),
                stale_names=report.get("stale_names"),
                any_core_stale=report.get("any_core_stale"),
            )
            if should_sweep:
                logger.info(
                    "bridge.regenerate_sweep.scheduled",
                    reason="core_sections_stale",
                    stale_names=report.get("stale_names"),
                )

                async def _bg_sweep():
                    try:
                        sweep_t0 = time.monotonic()
                        sweep_result = await _regenerate_sweep(_run_ic_engine)
                        sweep_dur_ms = int((time.monotonic() - sweep_t0) * 1000)
                        logger.info(
                            "bridge.regenerate_sweep.done",
                            duration_ms=sweep_dur_ms,
                            sections_run=sorted(sweep_result.keys()),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "bridge.regenerate_sweep.failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )

                asyncio.create_task(_bg_sweep())
        except Exception as e:
            logger.warning(
                "bridge.section_freshness.check_failed",
                error=f"{type(e).__name__}: {e}",
            )

    async def _run_both() -> None:
        tasks = [mcp_uvicorn.serve(), dash_uvicorn.serve()]
        if initialize_on_boot:
            tasks.append(_auto_initialize())
        await asyncio.gather(*tasks)

    try:
        asyncio.run(_run_both())
    except KeyboardInterrupt:
        logger.info("bridge.shutdown", reason="keyboard_interrupt")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
