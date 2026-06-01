# SPDX-License-Identifier: Apache-2.0
"""Provider routing config — primary + fallback chain for ic-engine.

ic-engine's PriceProvider facade reads two env vars to override its
default routing:

    INVESTORCLAW_PRICE_PROVIDER=auto|finnhub|yfinance|massive|alpha_vantage
    INVESTORCLAW_FALLBACK_CHAIN=yfinance,massive  (comma-separated)

This module persists those env vars to ``/data/provider_routing.env`` so
the bridge's dashboard "Provider routing" panel can change them without
rebuilding the container or editing keys.env. Subsequent ic-engine
subprocess spawns inherit the override via ``os.environ`` (the bridge
already does ``sub_env = dict(os.environ)`` in ``_runtime._run_ic_engine``).

Persistence:
  - File: ``/data/provider_routing.env`` (under the same /data volume
    as keys.env / portfolios / backups).
  - Format: ``KEY=VALUE\n`` shell-env shape (same as keys.env).
  - Mode: 0644 (these are not secrets — they are routing choices).
  - Atomic write: ``tempfile.NamedTemporaryFile`` (unique sibling
    name) + ``os.replace`` (POSIX atomic). Concurrent writes don't
    collide because each gets its own temp path.

Allowlist:
  Provider names are validated against a frozen allowlist mirroring
  ic-engine's PROVIDER_CLASSES registry. ``"auto"`` is also accepted
  for ``PRIMARY`` to signal the default routing-table behavior.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import structlog


logger = structlog.get_logger("investorclaw_bridge.provider_routing")

# Mirrors ic-engine's PROVIDER_CLASSES at v4.1.x. Keep in sync with
# argonautsystems/ic-engine src/ic_engine/providers/price_provider.py.
_VALID_PROVIDERS: frozenset[str] = frozenset({
    "finnhub",
    "yfinance",
    "newsapi",
    "massive",
    "alpha_vantage",
    "frankfurter",
    "treasury_fiscaldata",
    "marketaux",
})

# Env-var names ic-engine reads (PriceProvider class docstring).
_PRIMARY_ENV = "INVESTORCLAW_PRICE_PROVIDER"
_FALLBACK_ENV = "INVESTORCLAW_FALLBACK_CHAIN"


def _routing_path() -> Path:
    return Path(os.environ.get("IC_PROVIDER_ROUTING_FILE", "/data/provider_routing.env"))


def valid_providers() -> list[str]:
    """Return the sorted allowlist (excluding the special control word
    'auto' which is only valid for the primary slot, not the chain).
    """
    return sorted(_VALID_PROVIDERS)


def _parse_chain(raw: str) -> list[str]:
    """Split a comma-separated chain string into normalized parts."""
    if not raw:
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _parse_env_file(path: Path) -> dict[str, str]:
    """Same KEY=VALUE parser used for keys.env. Skips comments / blanks."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _log_invalid_persisted_value(path: Path, key: str, value: str) -> None:
    logger.warning(
        "provider_routing.invalid_persisted_value",
        path=str(path),
        key=key,
        value=value,
    )


def load_routing() -> dict[str, Any]:
    """Read the persisted routing + return current effective config.

    Effective values fall back to ``os.environ`` if the file is missing
    a key — this lets compose / quadlets override via Environment=
    even when no dashboard write has happened.

    Returns:
        ``{"primary": str | None, "fallback_chain": list[str], "valid_providers": list[str]}``

        ``primary`` is ``None`` (or ``"auto"``) when the user hasn't set
        an override; the engine's default ``_OP_ROUTING`` table applies.
    """
    path = _routing_path()
    persisted = _parse_env_file(path)

    primary = None
    if _PRIMARY_ENV in persisted:
        primary, err = _validate_primary(persisted[_PRIMARY_ENV])
        if err is not None:
            _log_invalid_persisted_value(path, _PRIMARY_ENV, persisted[_PRIMARY_ENV])

    if primary is None:
        primary = (
            os.environ.get(_PRIMARY_ENV)
            or "auto"
        ).strip().lower() or "auto"

    fallback_chain = None
    if _FALLBACK_ENV in persisted:
        fallback_chain, err = _validate_chain(_parse_chain(persisted[_FALLBACK_ENV]))
        if err is not None:
            _log_invalid_persisted_value(path, _FALLBACK_ENV, persisted[_FALLBACK_ENV])
            fallback_chain = None

    if fallback_chain is None:
        fallback_chain = _parse_chain(os.environ.get(_FALLBACK_ENV) or "")

    return {
        "primary": primary,
        "fallback_chain": fallback_chain,
        "valid_providers": valid_providers(),
        "routing_file": str(path),
    }


def _validate_primary(name: str | None) -> tuple[str | None, str | None]:
    """Return (normalized_name, error_or_none)."""
    if name is None:
        return None, None
    n = name.strip().lower()
    if not n or n == "auto":
        return "auto", None
    if n not in _VALID_PROVIDERS:
        return None, (
            f"Unknown provider {name!r}. Valid: "
            f"{['auto'] + valid_providers()}"
        )
    return n, None


def _validate_chain(chain: list[str] | None) -> tuple[list[str], str | None]:
    """Return (normalized_chain, error_or_none). Empty list is valid (== unset)."""
    if not chain:
        return [], None
    out = []
    for raw_name in chain:
        n = (raw_name or "").strip().lower()
        if not n:
            continue
        if n not in _VALID_PROVIDERS:
            return [], (
                f"Unknown provider in fallback chain: {raw_name!r}. Valid: "
                f"{valid_providers()}"
            )
        out.append(n)
    return out, None


def _push_into_environ(primary: str, fallback_chain: list[str]) -> None:
    """Mirror routing into ``os.environ`` so subsequent subprocess spawns
    inherit it via ``dict(os.environ)`` in ``_run_ic_engine``.

    Special case: ``primary == "auto"`` removes the override so the
    engine's default routing table applies. Empty chain removes the
    fallback override.
    """
    if primary and primary != "auto":
        os.environ[_PRIMARY_ENV] = primary
    else:
        os.environ.pop(_PRIMARY_ENV, None)

    if fallback_chain:
        os.environ[_FALLBACK_ENV] = ",".join(fallback_chain)
    else:
        os.environ.pop(_FALLBACK_ENV, None)


def _atomic_write(path: Path, lines: list[str]) -> None:
    """Write lines to path atomically via tmp + os.replace.

    The tmp file is in the same directory as the destination so
    ``os.replace`` is a same-filesystem operation (POSIX-atomic).
    On any exception during write the tmp file is unlinked and
    any pre-existing destination is preserved unchanged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            delete=False,
            suffix=".tmp",
            encoding="utf-8",
        ) as tmp:
            tmp_name = tmp.name
            tmp.write("\n".join(lines) + "\n")
        try:
            os.chmod(tmp_name, 0o644)
        except OSError:
            pass
        os.replace(tmp_name, path)
    except Exception:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass
        raise


def save_routing(
    primary: str | None = None,
    fallback_chain: list[str] | None = None,
) -> dict[str, Any]:
    """Persist routing config + mirror into ``os.environ``.

    Args:
        primary: provider name or ``"auto"`` (case-insensitive). ``None``
            preserves the current persisted value.
        fallback_chain: ordered list of provider names. ``None`` preserves
            the current persisted value. Empty list (``[]``) clears the
            override.

    Returns:
        On success: ``{"saved": True, "primary", "fallback_chain", "routing_file"}``
        On error:   ``{"error": "...", "detail": "..."}``
    """
    current = load_routing()

    if primary is None:
        norm_primary = current["primary"]
    else:
        norm_primary, err = _validate_primary(primary)
        if err is not None:
            return {"error": "invalid_primary", "detail": err}

    if fallback_chain is None:
        norm_chain = list(current["fallback_chain"])
    else:
        norm_chain, err = _validate_chain(fallback_chain)
        if err is not None:
            return {"error": "invalid_fallback_chain", "detail": err}

    # Build the new file body.
    lines = [
        "# InvestorClaw provider routing — written by the bridge dashboard.",
        "# Read by ic-engine's PriceProvider facade. Changes take effect on",
        "# the next portfolio_ask / regenerate run.",
    ]
    if norm_primary and norm_primary != "auto":
        lines.append(f"{_PRIMARY_ENV}={norm_primary}")
    if norm_chain:
        lines.append(f"{_FALLBACK_ENV}={','.join(norm_chain)}")

    try:
        _atomic_write(_routing_path(), lines)
    except OSError as e:
        return {"error": "routing_write_failed", "detail": str(e)}

    # Mirror into the bridge's env so the next subprocess inherits it.
    _push_into_environ(norm_primary or "auto", norm_chain)

    return {
        "saved": True,
        "primary": norm_primary or "auto",
        "fallback_chain": norm_chain,
        "routing_file": str(_routing_path()),
    }


def hydrate_environ_from_file() -> None:
    """Called once at bridge startup to populate ``os.environ`` from the
    persisted routing file. No-op if the file is missing or empty.

    This means: the dashboard write path is the source of truth, and
    bridge restart preserves user choice. Compose/quadlet ``Environment=``
    entries still win at the OS level (they are loaded before this
    process starts).
    """
    path = _routing_path()
    persisted = _parse_env_file(path)
    primary = persisted.get(_PRIMARY_ENV)
    chain = persisted.get(_FALLBACK_ENV)
    if primary:
        primary, err = _validate_primary(primary)
        if err is not None:
            _log_invalid_persisted_value(path, _PRIMARY_ENV, persisted[_PRIMARY_ENV])
            primary = None
    if chain:
        chain_values, err = _validate_chain(_parse_chain(chain))
        if err is not None:
            _log_invalid_persisted_value(path, _FALLBACK_ENV, persisted[_FALLBACK_ENV])
            chain = None
        else:
            chain = ",".join(chain_values)
    if primary and primary != "auto":
        os.environ.setdefault(_PRIMARY_ENV, primary)
    if chain:
        os.environ.setdefault(_FALLBACK_ENV, chain)
