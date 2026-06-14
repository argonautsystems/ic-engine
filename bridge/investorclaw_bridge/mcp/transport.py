# SPDX-License-Identifier: Apache-2.0
"""Transport adapters: FastMCP (MCP-HTTP) + FastAPI REST.

Both adapters iterate the canonical TOOL_REGISTRY in mcp/tools/__init__.py
and wire each tool to the appropriate decorator. Tool business logic lives
in tools/portfolio.py as pure async functions; this module is purely
transport plumbing.

The two transports are co-equal:
- MCP (FastMCP streamable_http) — primary, RFC-blessed.
- REST (FastAPI POST routes) — fallback for runtimes whose native MCP
  client integration has gaps. Added in v4.0.5; same backend, same
  ic_result envelope.

Future migration: when the MCP Python SDK's SSE transport
(mcp.server.sse.SseServerTransport) is preferred over FastMCP's
streamable_http_app(), only this module changes. tools/* and _runtime
stay put.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel as _BaseModel

from ._runtime import logger
from .tools import TOOL_REGISTRY

# ──────────────────────────────────────────────────────────────────────
# MCP transport (FastMCP streamable-http)
# ──────────────────────────────────────────────────────────────────────


def register_tools(app: Any) -> None:
    """Register all tools in TOOL_REGISTRY on a FastMCP application.

    `app` is an mcp.server.fastmcp.FastMCP instance. The caller wires
    transport (HTTP at :8090) + auth.
    """

    @app.tool()
    async def portfolio_ask(question: str) -> dict[str, Any]:
        """Ask a natural-language portfolio question via ic-engine."""
        return await TOOL_REGISTRY["portfolio_ask"]["handler"](question)

    @app.tool()
    async def portfolio_holdings() -> dict[str, Any]:
        """Current portfolio holdings snapshot."""
        return await TOOL_REGISTRY["portfolio_holdings"]["handler"]()

    @app.tool()
    async def portfolio_performance_window(
        period: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Deterministic explicit-window portfolio performance."""
        return await TOOL_REGISTRY["portfolio_performance_window"]["handler"](
            period, start_date, end_date
        )

    @app.tool()
    async def portfolio_refresh() -> dict[str, Any]:
        """Refresh market data without re-uploading portfolio files."""
        return await TOOL_REGISTRY["portfolio_refresh"]["handler"]()

    @app.tool()
    async def portfolio_setup() -> dict[str, Any]:
        """Auto-discover portfolio files in /data/portfolios/."""
        return await TOOL_REGISTRY["portfolio_setup"]["handler"]()

    @app.tool()
    async def portfolio_initialize(seed_question: str = "") -> dict[str, Any]:
        """One-shot bootstrap: setup + refresh + optional seed ask. After
        success every subsequent portfolio_ask hits the warm envelope cache."""
        return await TOOL_REGISTRY["portfolio_initialize"]["handler"](seed_question or None)

    @app.tool()
    async def portfolio_initialize_status() -> dict[str, Any]:
        """Live init state — poll until `ready: true` before firing portfolio_ask."""
        return await TOOL_REGISTRY["portfolio_initialize_status"]["handler"]()

    @app.tool()
    async def portfolio_keys_status() -> dict[str, Any]:
        """Report which API keys are currently configured (names only)."""
        return await TOOL_REGISTRY["portfolio_keys_status"]["handler"]()

    @app.tool()
    async def portfolio_keys_set(keys: dict[str, str]) -> dict[str, Any]:
        """Set one or more API keys; persists to /data/keys.env (mode 0600)."""
        return await TOOL_REGISTRY["portfolio_keys_set"]["handler"](keys)

    @app.tool()
    async def portfolio_keys_delete(name: str) -> dict[str, Any]:
        """Delete a single configured API key by name."""
        return await TOOL_REGISTRY["portfolio_keys_delete"]["handler"](name)

    @app.tool()
    async def portfolio_response_get(run_id: str) -> dict[str, Any]:
        """Retrieve a stored portfolio response by run_id (serial number)."""
        return await TOOL_REGISTRY["portfolio_response_get"]["handler"](run_id)

    @app.tool()
    async def portfolio_response_list(limit: int = 10) -> dict[str, Any]:
        """List recent stored portfolio responses."""
        return await TOOL_REGISTRY["portfolio_response_list"]["handler"](limit)

    @app.tool()
    async def portfolio_response_delete(run_id: str) -> dict[str, Any]:
        """Delete a stored portfolio response by run_id."""
        return await TOOL_REGISTRY["portfolio_response_delete"]["handler"](run_id)

    @app.tool()
    async def portfolio_response_flag_bad(run_id: str, reason: str = "") -> dict[str, Any]:
        """Flag a stored portfolio response as bad without deleting it."""
        return await TOOL_REGISTRY["portfolio_response_flag_bad"]["handler"](run_id, reason)

    logger.info(
        "mcp.tools.registered",
        tools=list(TOOL_REGISTRY.keys()),
    )


# ──────────────────────────────────────────────────────────────────────
# REST transport (FastAPI POST routes — fallback for flaky MCP clients)
# ──────────────────────────────────────────────────────────────────────


# Pydantic body model for /api/portfolio/ask. Defined at module scope (NOT
# closure-local inside register_rest_routes) so FastAPIs Pydantic v2
# TypeAdapter can fully resolve the ForwardRef. A closure-local class hits
# PydanticUserError: '... is not fully defined' at first request.
class AskBody(_BaseModel):
    question: str


class InitializeBody(_BaseModel):
    """Optional seed_question for portfolio_initialize. Empty string skips the seed ask."""
    seed_question: str = ""


class PerformanceWindowBody(_BaseModel):
    """Explicit time-window performance body."""
    period: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class KeysSetBody(_BaseModel):
    """Bulk-set body. Names must be in the allowlist (see /api/portfolio/keys/status)."""
    keys: dict[str, str]


class KeysDeleteBody(_BaseModel):
    """Single-name delete body."""
    name: str


class KeysRecommendBody(_BaseModel):
    """Optional explicit portfolio path for size-aware key recommendation.

    When omitted, the bridge picks the most-recently-modified CSV under
    /data/portfolios. Provide an explicit `portfolio_path` only when
    asking about a non-active portfolio (e.g. comparing two CSVs).
    """
    portfolio_path: str = ""


class VersionCheckBody(_BaseModel):
    """Optional registry/repository overrides for the version check.

    Defaults to ghcr.io/argonautsystems/ic-engine. Override only for
    forks or private mirrors.
    """
    registry: str = "ghcr.io"
    repository: str = "argonautsystems/ic-engine"


class ImportBody(_BaseModel):
    """Snapshot wrapper for portfolio_import.

    The body is `{"snapshot": <export-output>}` — wrapping keeps the
    surface symmetric with portfolio_keys_set's `{"keys": {...}}` shape
    and leaves room for future top-level options (e.g. dry_run).
    """
    snapshot: dict[str, Any]


class KeysBackupBody(_BaseModel):
    """Body for /api/portfolio/keys_backup.

    The passphrase encrypts /data/keys.env on disk; without it the
    backup is unrecoverable. Min 12 chars enforced server-side.
    """
    passphrase: str
    label: str = ""


class KeysRestoreBody(_BaseModel):
    """Body for /api/portfolio/keys_restore.

    `backup_path` is optional — if omitted the most-recently-modified
    keys-*.bak under /data/backups/ is auto-selected. Relative paths
    resolve under /data/backups/; traversal outside is rejected.
    """
    passphrase: str
    backup_path: str = ""


class ResponseGetBody(_BaseModel):
    """Lookup by run_id (serial number)."""
    run_id: str


class ResponseListBody(_BaseModel):
    """List most-recent stored responses."""
    limit: int = 10


class ResponseDeleteBody(_BaseModel):
    """Delete a stored response by run_id."""
    run_id: str


class ResponseFlagBadBody(_BaseModel):
    """Flag a stored response as bad with optional reason note."""
    run_id: str
    reason: str = ""


def register_rest_routes(app: Any) -> None:
    """Register REST wrappers on a FastAPI app for the same tools as MCP.

    See module docstring. Bridge exposes /api/portfolio/{ask,holdings,
    refresh,setup,tools}; agents that have shell+curl can hit these
    without going through the MCP handshake.
    """
    from fastapi import Body, HTTPException

    @app.post("/api/portfolio/ask")
    async def rest_portfolio_ask(body: AskBody = Body(...)) -> dict[str, Any]:
        if not body.question:
            raise HTTPException(status_code=400, detail="question is required")
        return await TOOL_REGISTRY["portfolio_ask"]["handler"](body.question)

    @app.post("/api/portfolio/holdings")
    async def rest_portfolio_holdings() -> dict[str, Any]:
        return await TOOL_REGISTRY["portfolio_holdings"]["handler"]()

    @app.post("/api/portfolio/performance_window")
    async def rest_portfolio_performance_window(
        body: PerformanceWindowBody = Body(default_factory=PerformanceWindowBody),
    ) -> dict[str, Any]:
        return await TOOL_REGISTRY["portfolio_performance_window"]["handler"](
            body.period, body.start_date, body.end_date
        )

    @app.post("/api/portfolio/refresh")
    async def rest_portfolio_refresh() -> dict[str, Any]:
        return await TOOL_REGISTRY["portfolio_refresh"]["handler"]()

    @app.post("/api/portfolio/setup")
    async def rest_portfolio_setup() -> dict[str, Any]:
        return await TOOL_REGISTRY["portfolio_setup"]["handler"]()

    @app.post("/api/portfolio/initialize")
    async def rest_portfolio_initialize(body: InitializeBody = Body(default=InitializeBody())) -> dict[str, Any]:
        """One-shot bootstrap. Body shape: `{"seed_question": "..."}` or `{}`."""
        return await TOOL_REGISTRY["portfolio_initialize"]["handler"](body.seed_question or None)

    @app.get("/api/portfolio/initialize/status")
    @app.post("/api/portfolio/initialize_status")
    async def rest_portfolio_initialize_status() -> dict[str, Any]:
        """Live init state. GET for browser/curl convenience; POST for tool-name parity."""
        return await TOOL_REGISTRY["portfolio_initialize_status"]["handler"]()

    @app.get("/api/portfolio/initialize/stream")
    async def rest_portfolio_initialize_stream():
        """Server-sent-events stream of init state — agents can subscribe to
        get push updates when the state changes (or every 2s as keepalive).
        Closes the connection once the state reaches `ready` or `failed`.
        """
        import asyncio as _asyncio
        import json as _json

        from fastapi.responses import StreamingResponse

        from .tools import get_init_state as _get_init_state

        async def _gen():
            last_payload = None
            while True:
                snap = _get_init_state()
                payload = _json.dumps(snap, default=str)
                if payload != last_payload:
                    yield f"event: init_state\ndata: {payload}\n\n"
                    last_payload = payload
                if snap["state"] in ("ready", "failed"):
                    yield "event: done\ndata: {}\n\n"
                    return
                await _asyncio.sleep(2.0)

        return StreamingResponse(_gen(), media_type="text/event-stream")

    # Key management — paths match tool names exactly (`portfolio_keys_*`)
    # so agents can derive URLs from the catalog's `path` field directly.
    # POST for both status and set keeps the surface uniform with the
    # other portfolio_* tools (some MCP-bridged HTTP clients only emit
    # POST for tool-call style verbs).
    @app.post("/api/portfolio/keys_status")
    async def rest_keys_status() -> dict[str, Any]:
        """Which API keys are configured (names only). Safe — never returns values."""
        return await TOOL_REGISTRY["portfolio_keys_status"]["handler"]()

    @app.post("/api/portfolio/keys_set")
    async def rest_keys_set(body: KeysSetBody = Body(...)) -> dict[str, Any]:
        """Set or delete API keys (allowlisted only).

        Body shape: `{"keys": {"FINNHUB_KEY": "...", "TOGETHER_API_KEY": "..."}}`.
        Empty value deletes the key. Names not in the allowlist are rejected
        with a structured 200 response (NOT 400 — agents read the `rejected`
        field). Bridge mirrors keys into os.environ live, so the next
        portfolio_ask sees them without restart.
        """
        return await TOOL_REGISTRY["portfolio_keys_set"]["handler"](body.keys)

    @app.post("/api/portfolio/keys_delete")
    async def rest_keys_delete(body: KeysDeleteBody = Body(...)) -> dict[str, Any]:
        """Delete a single configured key by name. Allowlisted names only."""
        return await TOOL_REGISTRY["portfolio_keys_delete"]["handler"](body.name)

    @app.post("/api/portfolio/keys_recommend")
    async def rest_keys_recommend(
        body: KeysRecommendBody = Body(default_factory=KeysRecommendBody),
    ) -> dict[str, Any]:
        """Size-aware API key recommendations for the active portfolio.

        Returns per-key priority (`strongly_recommended` /
        `recommended` / `optional`) + rationale + signup_url +
        whether each key is currently configured. The dashboard
        Settings tab and the agent setup-orchestrator surface this
        so users with large portfolios are told upfront they should
        configure MASSIVE_API_KEY.
        """
        portfolio_path = body.portfolio_path or None
        return await TOOL_REGISTRY["portfolio_keys_recommend"]["handler"](portfolio_path)

    # ──────────────────────────────────────────────────────────────────
    # Upgrade flow: version check + state export/import
    # ──────────────────────────────────────────────────────────────────

    @app.post("/api/portfolio/version_check")
    async def rest_version_check(
        body: VersionCheckBody = Body(default_factory=VersionCheckBody),
    ) -> dict[str, Any]:
        """Compare running container version to the latest published on
        ghcr.io. Returns running, latest, upgrade_available, and human-
        readable next_steps. Network failures return latest=null + a
        warning rather than 5xx — version check is advisory.
        """
        return await TOOL_REGISTRY["portfolio_version_check"]["handler"](
            registry=body.registry, repository=body.repository,
        )

    @app.post("/api/portfolio/export")
    async def rest_export() -> dict[str, Any]:
        """Return a JSON snapshot of /data state (portfolios + stonkmode +
        configured-key-NAMES). Does NOT include API key values. Use as
        the input to /api/portfolio/import for backup/restore or
        cross-host migration.
        """
        return await TOOL_REGISTRY["portfolio_export"]["handler"]()

    @app.post("/api/portfolio/import")
    async def rest_import(body: ImportBody = Body(...)) -> dict[str, Any]:
        """Restore a snapshot produced by /api/portfolio/export. Body
        shape: `{"snapshot": <export-output>}`. Existing files are
        overwritten. Re-set API keys via portfolio_keys_set after import.
        """
        return await TOOL_REGISTRY["portfolio_import"]["handler"](body.snapshot)

    # ──────────────────────────────────────────────────────────────────
    # Encrypted keys backup/restore (#96) — passphrase-encrypted blob
    # for cross-host migration. Plaintext NEVER traverses these
    # endpoints — only the passphrase (in the request body) and
    # filenames/key-names (in the response).
    # ──────────────────────────────────────────────────────────────────

    @app.post("/api/portfolio/keys_backup")
    async def rest_keys_backup(body: KeysBackupBody = Body(...)) -> dict[str, Any]:
        """Encrypt /data/keys.env with a passphrase, write to
        /data/backups/. Returns filename + key NAMES + KDF — never
        values."""
        return await TOOL_REGISTRY["portfolio_keys_backup"]["handler"](
            passphrase=body.passphrase, label=body.label,
        )

    @app.post("/api/portfolio/keys_restore")
    async def rest_keys_restore(body: KeysRestoreBody = Body(...)) -> dict[str, Any]:
        """Decrypt a backup and replace /data/keys.env. Decrypted
        values never return to the caller — only the list of restored
        key NAMES."""
        return await TOOL_REGISTRY["portfolio_keys_restore"]["handler"](
            passphrase=body.passphrase, backup_path=body.backup_path,
        )

    @app.post("/api/portfolio/keys_backups_list")
    async def rest_keys_backups_list() -> dict[str, Any]:
        """Enumerate available encrypted backups under /data/backups/.
        No passphrase required — listing only returns metadata."""
        return await TOOL_REGISTRY["portfolio_keys_backups_list"]["handler"]()

    # Convenience: a GET alias on /api/portfolio/keys/status for browser/dev
    # use. The canonical path remains /api/portfolio/keys_status to match
    # the tool name and catalog discovery.
    @app.get("/api/portfolio/keys/status")
    async def rest_keys_status_alias() -> dict[str, Any]:
        return await TOOL_REGISTRY["portfolio_keys_status"]["handler"]()

    # ──────────────────────────────────────────────────────────────────
    # Stored-response CRUD (paths match tool names — see catalog)
    # ──────────────────────────────────────────────────────────────────

    @app.post("/api/portfolio/response_get")
    async def rest_response_get(body: ResponseGetBody = Body(...)) -> dict[str, Any]:
        """Retrieve a stored response by run_id (serial number)."""
        return await TOOL_REGISTRY["portfolio_response_get"]["handler"](body.run_id)

    @app.post("/api/portfolio/response_list")
    async def rest_response_list(body: ResponseListBody = Body(...)) -> dict[str, Any]:
        """List recent stored responses."""
        return await TOOL_REGISTRY["portfolio_response_list"]["handler"](body.limit)

    @app.post("/api/portfolio/response_delete")
    async def rest_response_delete(body: ResponseDeleteBody = Body(...)) -> dict[str, Any]:
        """Delete a stored response by run_id."""
        return await TOOL_REGISTRY["portfolio_response_delete"]["handler"](body.run_id)

    @app.post("/api/portfolio/response_flag_bad")
    async def rest_response_flag_bad(body: ResponseFlagBadBody = Body(...)) -> dict[str, Any]:
        """Flag a stored response as bad without deleting."""
        return await TOOL_REGISTRY["portfolio_response_flag_bad"]["handler"](body.run_id, body.reason)

    @app.get("/api/portfolio/tools")
    async def rest_portfolio_tools() -> dict[str, Any]:
        """Self-describing tool catalog — agents can curl this to discover endpoints."""
        catalog = []
        for name, info in TOOL_REGISTRY.items():
            entry = {
                "name": name,
                "method": "POST",
                "path": f"/api/portfolio/{name.removeprefix('portfolio_')}",
                "body": (
                    {p: schema.get("type", "string") for p, schema in info["parameters"].items()}
                    if info["parameters"] else {}
                ),
                "description": info["description"],
            }
            catalog.append(entry)
        return {"tools": catalog}

    logger.info(
        "mcp.rest_routes.registered",
        endpoints=[f"/api/portfolio/{n.removeprefix('portfolio_')}" for n in TOOL_REGISTRY.keys()] + ["/api/portfolio/tools"],
    )
