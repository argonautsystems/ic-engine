# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Cache management for v2.5 full-run envelopes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from ic_engine.config.path_resolver import get_reports_dir, secure_file_permissions
from ic_engine.runtime.envelope import (
    CANONICAL_SECTIONS,
    Envelope,
    EnvelopeValidationError,
    envelope_freshness,
    envelope_path,
    portfolio_id_for_holdings,
    validate_envelope,
)
from ic_engine.runtime.full_run import DEFAULT_SECTION_TTLS, run_full


class CacheStatus(TypedDict):
    portfolio_id: str
    path: Path
    exists: bool
    valid: bool
    needs_run: bool
    missing_sections: list[str]
    stale_sections: list[str]
    error: str


def _reports_dir(reports_dir: str | Path | None = None) -> Path:
    return Path(reports_dir).expanduser() if reports_dir is not None else get_reports_dir()


def _cache_path(
    holdings_path: str | Path,
    reports_dir: str | Path | None = None,
) -> tuple[str, Path]:
    portfolio_id = portfolio_id_for_holdings(holdings_path)
    return portfolio_id, envelope_path(_reports_dir(reports_dir), portfolio_id)


def load_cached_envelope(
    holdings_path: str | Path,
    *,
    reports_dir: str | Path | None = None,
    validate: bool = True,
) -> Envelope | None:
    """Load a cached envelope if present and valid."""
    _portfolio_id, path = _cache_path(holdings_path, reports_dir)
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if validate:
            validate_envelope(envelope)
        return envelope
    except (OSError, json.JSONDecodeError, EnvelopeValidationError, KeyError, TypeError):
        return None


def stale_sections(envelope: Envelope) -> list[str]:
    """Return canonical sections that are stale or missing."""
    return [
        section
        for section in CANONICAL_SECTIONS
        if envelope_freshness(envelope, section) != "fresh"
    ]


def cache_status(
    holdings_path: str | Path,
    *,
    reports_dir: str | Path | None = None,
) -> CacheStatus:
    """Inspect the envelope cache without running the pipeline."""
    portfolio_id, path = _cache_path(holdings_path, reports_dir)
    status: CacheStatus = {
        "portfolio_id": portfolio_id,
        "path": path,
        "exists": path.exists(),
        "valid": False,
        "needs_run": True,
        "missing_sections": [],
        "stale_sections": [],
        "error": "",
    }
    if not path.exists():
        status["missing_sections"] = list(CANONICAL_SECTIONS)
        return status

    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        validate_envelope(envelope)
    except (OSError, json.JSONDecodeError, EnvelopeValidationError, KeyError, TypeError) as exc:
        status["error"] = str(exc)
        status["missing_sections"] = list(CANONICAL_SECTIONS)
        return status

    status["valid"] = True
    for section in CANONICAL_SECTIONS:
        freshness = envelope_freshness(envelope, section)
        if freshness == "missing":
            status["missing_sections"].append(section)
        elif freshness == "stale":
            status["stale_sections"].append(section)
    status["needs_run"] = bool(status["missing_sections"] or status["stale_sections"])
    return status


def save_envelope(
    envelope: Envelope,
    *,
    reports_dir: str | Path | None = None,
) -> Path:
    """Persist a signed envelope to the canonical cache path."""
    validate_envelope(envelope)
    path = envelope_path(_reports_dir(reports_dir), envelope["portfolio_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    secure_file_permissions(path)
    return path


def get_or_run(
    holdings_path: str | Path,
    force_refresh: bool = False,
    *,
    reports_dir: str | Path | None = None,
    ttl_by_section: dict[str, int] | None = None,
) -> Envelope:
    """Return a fresh envelope from cache, or run the full pipeline."""
    ttl_map = {**DEFAULT_SECTION_TTLS, **(ttl_by_section or {})}
    cached = load_cached_envelope(holdings_path, reports_dir=reports_dir)
    if cached is not None and not force_refresh and not stale_sections(cached):
        return cached

    envelope = run_full(holdings_path, ttl_by_section=ttl_map)
    save_envelope(envelope, reports_dir=reports_dir)
    return envelope


def cache_summary(status: CacheStatus) -> dict[str, Any]:
    """Return a JSON-serializable summary for command output/tests."""
    return {
        **status,
        "path": str(status["path"]),
    }
