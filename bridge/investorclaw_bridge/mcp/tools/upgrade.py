# SPDX-License-Identifier: Apache-2.0
"""Upgrade-flow tools — version check + portable state export/import.

Lets an agent drive an InvestorClaw container upgrade end-to-end:

  1. portfolio_version_check — query ghcr.io anonymously for the latest
     published ic-engine image and compare to the running version.
  2. portfolio_export — return a JSON snapshot of /data state (portfolios,
     stonkmode persona). Does NOT include API keys (security: keys are
     plaintext secrets and persist via the /data volume mount across a
     container replacement).
  3. portfolio_import — accept a snapshot JSON and restore portfolios +
     stonkmode into the active /data volume.

Standard upgrade flow (when /data is bind-mounted or a named volume):
  - portfolio_version_check → confirm new tag available
  - host shell: `docker compose pull && docker compose up -d`
  - poll /healthz until init_state=ready
  - state survives automatically via the volume mount

Cross-host migration (no shared volume):
  - portfolio_export on source → snapshot.json
  - copy snapshot.json to target host
  - start a fresh container on target
  - portfolio_import on target with snapshot.json
  - re-set API keys via portfolio_keys_set (NOT in the export — security)
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .._runtime import logger


_DEFAULT_REGISTRY = "ghcr.io"
_DEFAULT_REPOSITORY = "argonautsystems/ic-engine"
_VERSION_TAG_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-cpu$")


def _running_version() -> str:
    """Return the bridge's own image version.

    Reads `IC_ENGINE_VERSION` (set by the Dockerfile ENV directive,
    bumped per release). Falls back to `unknown` if the env var isn't
    set — that means the container was started without the standard
    image, which is a misconfiguration but shouldn't crash the endpoint.
    """
    return os.environ.get("IC_ENGINE_VERSION", "unknown")


def _parse_semver(tag: str) -> tuple[int, int, int] | None:
    """Parse `4.1.39-cpu` → (4, 1, 39). Return None if non-conforming."""
    m = _VERSION_TAG_RE.match(tag)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


async def _fetch_ghcr_anonymous_token(repository: str) -> str:
    """Fetch an anonymous pull token for a public ghcr.io repository.

    ghcr's auth flow: even for public packages the v2 registry API
    requires a Bearer token. The token endpoint is unauthenticated for
    public-pull scope. Returns "" on any failure — caller treats that
    as "version check unavailable".
    """
    import httpx
    url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repository}:pull"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json().get("token", "")
    except Exception as exc:
        logger.warning("upgrade.ghcr_token_failed", error=f"{type(exc).__name__}: {exc}")
        return ""


async def portfolio_version_check(
    registry: str = _DEFAULT_REGISTRY,
    repository: str = _DEFAULT_REPOSITORY,
) -> dict[str, Any]:
    """Compare the running version to the latest published on ghcr.io.

    Returns:
        {
            "running": "4.1.38",
            "latest": "4.1.39",
            "upgrade_available": true,
            "registry": "ghcr.io",
            "repository": "argonautsystems/ic-engine",
            "next_steps": [...],   # human-readable upgrade instructions
            "warnings": [...]      # advisory messages
        }

    Failure modes (network down, registry unreachable, anonymous token
    refused) return `latest: null` + a warning. The endpoint never
    raises — version check is advisory, not load-bearing.
    """
    import httpx
    running = _running_version()
    result: dict[str, Any] = {
        "running": running,
        "latest": None,
        "latest_tag": None,
        "upgrade_available": False,
        "registry": registry,
        "repository": repository,
        "warnings": [],
        "next_steps": [],
    }

    token = await _fetch_ghcr_anonymous_token(repository)
    if not token:
        result["warnings"].append(
            "Could not fetch ghcr.io anonymous pull token — registry "
            "unreachable or repository non-public."
        )
        return result

    tags_url = f"https://{registry}/v2/{repository}/tags/list"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(tags_url, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            tags = r.json().get("tags", []) or []
    except Exception as exc:
        result["warnings"].append(f"Tag list fetch failed: {type(exc).__name__}: {exc}")
        return result

    semvers = [(t, _parse_semver(t)) for t in tags]
    semvers = [(t, v) for t, v in semvers if v is not None]
    if not semvers:
        result["warnings"].append("No semver-tagged images found in registry.")
        return result
    semvers.sort(key=lambda tv: tv[1], reverse=True)
    latest_tag, latest_ver = semvers[0]
    result["latest_tag"] = latest_tag
    result["latest"] = ".".join(str(x) for x in latest_ver)

    running_ver = _parse_semver(f"{running}-cpu")
    if running_ver is None:
        result["warnings"].append(
            f"Running version `{running}` doesn't match the X.Y.Z-cpu pattern."
        )
        result["upgrade_available"] = False
    else:
        result["upgrade_available"] = latest_ver > running_ver

    if result["upgrade_available"]:
        result["next_steps"] = [
            (
                "Run `portfolio_export` to snapshot your current state to "
                "JSON (portfolios + stonkmode; keys persist via /data)."
            ),
            (
                "On the host: `docker compose pull && docker compose up -d` "
                "(or `podman` / quadlet equivalent — `systemctl restart "
                "ic-engine.service` for the NCZ pi-gen layout)."
            ),
            "Wait for /healthz init_state=ready (typically 30-60s).",
            (
                "Run `portfolio_version_check` again to confirm `running` "
                "matches `latest`."
            ),
            (
                "If your /data volume was lost, call `portfolio_import` with "
                "the snapshot JSON to restore portfolios + stonkmode, then "
                "re-set API keys via `portfolio_keys_set`."
            ),
        ]
    else:
        result["next_steps"] = ["You are on the latest published version. No action."]

    return result


def _portfolio_dir() -> Path:
    return Path(os.environ.get("IC_PORTFOLIO_DIR", "/data/portfolios"))


def _stonkmode_path() -> Path:
    return Path(os.environ.get("IC_STONKMODE_FILE", "/data/stonkmode.json"))


_EXPORT_SCHEMA = "ic-engine-export/v2"
# v4.3.0+ writers emit v2 (which adds provider_routing). v4.1.39 - v4.2.1
# emit v1. The importer below accepts either; v2-only fields are ignored
# when an older snapshot is loaded.
_ACCEPTED_SCHEMAS = frozenset({"ic-engine-export/v1", "ic-engine-export/v2"})
_MAX_PORTFOLIO_BYTES = 5 * 1024 * 1024  # 5 MB per file — sanity cap


async def portfolio_export() -> dict[str, Any]:
    """Return a JSON snapshot of the active /data state.

    Includes:
        - All CSVs under /data/portfolios/ (UTF-8 text, base64 if binary)
        - /data/stonkmode.json contents (if present)
        - List of currently-configured key NAMES (no values)

    Excludes:
        - API key VALUES (security — they're plaintext secrets and the
          /data volume already preserves them across a container
          replacement).
        - /data/reports/ (regeneratable via portfolio_refresh).
        - Envelope cache (regeneratable).

    The snapshot is the input shape for `portfolio_import`; both pin
    `schema_version` for forward-compat.
    """
    snapshot: dict[str, Any] = {
        "schema_version": _EXPORT_SCHEMA,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine_version": _running_version(),
        "portfolios": [],
        "stonkmode_state": None,
        "configured_keys": [],
        "provider_routing": None,
        "warnings": [
            (
                "API key VALUES are NOT included in this export (security). "
                "Keys persist via the /data volume across container "
                "replacement. For cross-host migration where the volume "
                "isn't shared, re-set keys via portfolio_keys_set after "
                "import."
            ),
            (
                "Section reports under /data/reports/ are NOT included. "
                "Run portfolio_refresh after import to regenerate."
            ),
        ],
    }

    pdir = _portfolio_dir()
    if pdir.is_dir():
        for csv_path in sorted(pdir.glob("*.csv")):
            try:
                stat = csv_path.stat()
                if stat.st_size > _MAX_PORTFOLIO_BYTES:
                    snapshot["warnings"].append(
                        f"Skipped {csv_path.name}: exceeds {_MAX_PORTFOLIO_BYTES} bytes."
                    )
                    continue
                raw = csv_path.read_bytes()
                # Try utf-8; fall back to base64 with marker.
                try:
                    content_text = raw.decode("utf-8")
                    encoding = "utf-8"
                except UnicodeDecodeError:
                    content_text = base64.b64encode(raw).decode("ascii")
                    encoding = "base64"
                snapshot["portfolios"].append({
                    "filename": csv_path.name,
                    "encoding": encoding,
                    "content": content_text,
                    "modified_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)
                    ),
                    "size_bytes": stat.st_size,
                })
            except Exception as exc:
                snapshot["warnings"].append(
                    f"Failed to read {csv_path.name}: {type(exc).__name__}: {exc}"
                )

    # Stonkmode persona state — small JSON file; embed verbatim if present.
    smpath = _stonkmode_path()
    if smpath.is_file():
        try:
            snapshot["stonkmode_state"] = json.loads(smpath.read_text())
        except Exception as exc:
            snapshot["warnings"].append(
                f"Failed to parse {smpath}: {type(exc).__name__}: {exc}"
            )

    # Configured key NAMES (no values)
    try:
        from .keys import portfolio_keys_status
        ks = await portfolio_keys_status()
        snapshot["configured_keys"] = ks.get("configured", [])
    except Exception as exc:
        snapshot["warnings"].append(
            f"keys_status query failed: {type(exc).__name__}: {exc}"
        )

    # Provider routing (v4.2.0+) — primary + fallback chain. Embed the
    # current effective config; the v1 schema didn't carry this field.
    try:
        from ... import provider_routing as _pr
        rinfo = _pr.load_routing()
        snapshot["provider_routing"] = {
            "primary": rinfo.get("primary"),
            "fallback_chain": list(rinfo.get("fallback_chain") or []),
        }
    except Exception as exc:
        snapshot["warnings"].append(
            f"provider_routing query failed: {type(exc).__name__}: {exc}"
        )

    logger.info(
        "upgrade.export",
        portfolios=len(snapshot["portfolios"]),
        stonkmode=snapshot["stonkmode_state"] is not None,
        configured_keys=len(snapshot["configured_keys"]),
    )
    return snapshot


async def portfolio_import(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Restore a snapshot produced by `portfolio_export` into /data.

    Writes:
        - portfolios → /data/portfolios/<filename>
        - stonkmode_state → /data/stonkmode.json (if present in snapshot)

    Does NOT touch /data/keys.env — keys must be re-set via
    portfolio_keys_set after import (the export never carries values).

    Validates `schema_version` strictly. Existing files are OVERWRITTEN
    (the user invoked import; idempotency wins over preservation).

    Returns:
        {
            "imported": {"portfolios": N, "stonkmode": bool},
            "warnings": [...],
            "configured_keys_in_snapshot": [...],
            "next_steps": [...]
        }
    """
    result: dict[str, Any] = {
        "imported": {"portfolios": 0, "stonkmode": False},
        "warnings": [],
        "configured_keys_in_snapshot": [],
        "next_steps": [],
    }

    if not isinstance(snapshot, dict):
        return {**result, "error": "snapshot_must_be_dict"}

    schema = snapshot.get("schema_version")
    if schema not in _ACCEPTED_SCHEMAS:
        return {
            **result,
            "error": "schema_version_mismatch",
            "expected": sorted(_ACCEPTED_SCHEMAS),
            "got": schema,
        }

    pdir = _portfolio_dir()
    pdir.mkdir(parents=True, exist_ok=True)

    for entry in snapshot.get("portfolios") or []:
        try:
            filename = entry["filename"]
            content = entry["content"]
            encoding = entry.get("encoding", "utf-8")
            # Sanitize filename — no path traversal, no hidden files.
            safe = Path(filename).name
            if not safe or safe.startswith("."):
                result["warnings"].append(f"Rejected filename `{filename}` (unsafe).")
                continue
            target = pdir / safe
            if encoding == "base64":
                target.write_bytes(base64.b64decode(content))
            else:
                target.write_text(content)
            result["imported"]["portfolios"] += 1
        except Exception as exc:
            result["warnings"].append(
                f"Portfolio entry import failed: {type(exc).__name__}: {exc}"
            )

    sm_state = snapshot.get("stonkmode_state")
    if sm_state is not None:
        try:
            _stonkmode_path().write_text(json.dumps(sm_state, indent=2))
            result["imported"]["stonkmode"] = True
        except Exception as exc:
            result["warnings"].append(
                f"stonkmode write failed: {type(exc).__name__}: {exc}"
            )

    # Provider routing (v4.2.0+) — only present in v2 snapshots.
    routing = snapshot.get("provider_routing")
    result["imported"]["provider_routing"] = False
    if isinstance(routing, dict):
        try:
            from ... import provider_routing as _pr
            primary = routing.get("primary")
            chain = routing.get("fallback_chain") or []
            save_result = _pr.save_routing(primary=primary, fallback_chain=chain)
            if "error" in save_result:
                result["warnings"].append(
                    f"provider_routing restore rejected: "
                    f"{save_result.get('detail') or save_result['error']}"
                )
            else:
                result["imported"]["provider_routing"] = True
        except Exception as exc:
            result["warnings"].append(
                f"provider_routing restore failed: {type(exc).__name__}: {exc}"
            )

    result["configured_keys_in_snapshot"] = list(snapshot.get("configured_keys") or [])
    if result["configured_keys_in_snapshot"]:
        result["next_steps"].append(
            "Configure keys via portfolio_keys_set — values are not in the "
            "snapshot. The names that were configured on the source are listed "
            "in `configured_keys_in_snapshot`."
        )
    result["next_steps"].append(
        "Run portfolio_refresh to regenerate section reports."
    )

    logger.info(
        "upgrade.import",
        portfolios=result["imported"]["portfolios"],
        stonkmode=result["imported"]["stonkmode"],
        warnings=len(result["warnings"]),
    )
    return result


# ──────────────────────────────────────────────────────────────────────
# Tool descriptors (registered via TOOL_REGISTRY in tools/__init__.py)
# ──────────────────────────────────────────────────────────────────────


def _tool(description: str, parameters: dict, required: list[str], handler) -> dict:
    return {
        "description": description,
        "parameters": parameters,
        "required": required,
        "handler": handler,
    }


UPGRADE_TOOLS: dict[str, dict[str, Any]] = {
    "portfolio_version_check": _tool(
        description=(
            "Check whether a newer ic-engine image is published on ghcr.io. "
            "Compares the running container's IC_ENGINE_VERSION against the "
            "latest semver tag of `argonautsystems/ic-engine` (or a custom "
            "registry/repository if provided). Returns running, latest, "
            "upgrade_available, and human-readable next_steps. Network/"
            "registry failures return latest=null + a warning rather than "
            "raising — version check is advisory."
        ),
        parameters={
            "registry": {
                "type": "string",
                "description": "Container registry host (default: ghcr.io).",
            },
            "repository": {
                "type": "string",
                "description": (
                    "Image repository under the registry (default: "
                    "argonautsystems/ic-engine). Override only for forks "
                    "or private registries."
                ),
            },
        },
        required=[],
        handler=portfolio_version_check,
    ),
    "portfolio_export": _tool(
        description=(
            "Return a JSON snapshot of the active /data state for backup or "
            "cross-host migration. Includes portfolio CSVs (UTF-8 inline; "
            "base64 if binary) and stonkmode persona state. EXCLUDES API key "
            "values (security — keys are plaintext secrets that persist via "
            "the /data volume mount across container replacements). The "
            "snapshot is the input shape for portfolio_import. Writers emit "
            "schema_version=ic-engine-export/v2; the importer accepts v1 and "
            "v2 snapshots for forward-compat."
        ),
        parameters={},
        required=[],
        handler=portfolio_export,
    ),
    "portfolio_import": _tool(
        description=(
            "Restore a snapshot produced by portfolio_export into the "
            "active /data volume. Writes portfolios → /data/portfolios/ and "
            "stonkmode_state → /data/stonkmode.json. Does NOT touch keys — "
            "re-set via portfolio_keys_set after import (export never "
            "carries values). Existing files are overwritten. Accepts "
            "schema_version=ic-engine-export/v1 and ic-engine-export/v2."
        ),
        parameters={
            "snapshot": {
                "type": "object",
                "description": (
                    "The JSON snapshot returned by portfolio_export. Current "
                    "exports use schema_version=ic-engine-export/v2; v1 "
                    "snapshots are also accepted."
                ),
            },
        },
        required=["snapshot"],
        handler=portfolio_import,
    ),
}
