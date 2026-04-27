# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Canonical v2.5 deterministic-first result envelope."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

from ic_engine import __version__
from ic_engine.internal.tier3_enrichment import _get_hmac_key

SCHEMA_VERSION = "v2.5.0"

CANONICAL_SECTIONS: tuple[str, ...] = (
    "holdings",
    "performance",
    "bonds",
    "analyst",
    "news",
    "synthesize",
    "optimize",
    "cashflow",
    "peer",
)


class IcResult(TypedDict):
    hmac: str
    engine_version: str
    command: str
    run_id: str


class SectionMeta(TypedDict, total=False):
    computed_at: str
    ttl_seconds: int
    source: str
    error: str
    status: str


class Envelope(TypedDict):
    schema_version: str
    generated_at: str
    portfolio_id: str
    ic_result: IcResult
    sections: dict[str, dict[str, Any]]
    section_meta: dict[str, SectionMeta]
    failed_sections: list[str]


Freshness = Literal["fresh", "stale", "missing"]


class EnvelopeValidationError(ValueError):
    """Raised when an envelope is malformed or has invalid provenance."""


def utc_now_iso() -> str:
    """Return a stable UTC ISO timestamp for envelope fields."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def portfolio_id_for_holdings(holdings_path: str | Path) -> str:
    """Return the SHA256 hash of the holdings file content."""
    path = Path(holdings_path).expanduser()
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def envelope_path(reports_dir: str | Path, portfolio_id: str) -> Path:
    """Return the canonical cache path for a portfolio envelope."""
    cache_dir = Path(reports_dir).expanduser() / ".cache"
    return cache_dir / f"envelope.{portfolio_id}.json"


def _parse_iso_timestamp(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_payload(envelope: Envelope) -> str:
    payload = copy.deepcopy(envelope)
    payload.setdefault("ic_result", {})["hmac"] = ""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign_envelope(envelope: Envelope) -> str:
    """Compute the HMAC over the envelope with ``ic_result.hmac`` blanked."""
    return hmac.new(
        _get_hmac_key(),
        _canonical_payload(envelope).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def attach_hmac(envelope: Envelope) -> Envelope:
    """Attach a fresh HMAC to an envelope and return it."""
    envelope["ic_result"]["hmac"] = sign_envelope(envelope)
    return envelope


def validate_envelope(envelope: Envelope) -> None:
    """Validate required schema fields and HMAC provenance."""
    if not isinstance(envelope, dict):
        raise EnvelopeValidationError("Envelope must be a dict")

    required = {
        "schema_version",
        "generated_at",
        "portfolio_id",
        "ic_result",
        "sections",
        "section_meta",
        "failed_sections",
    }
    missing = sorted(required.difference(envelope))
    if missing:
        raise EnvelopeValidationError(f"Envelope missing required fields: {missing}")

    if envelope["schema_version"] != SCHEMA_VERSION:
        raise EnvelopeValidationError(
            f"Unsupported envelope schema_version: {envelope['schema_version']}"
        )

    if not isinstance(envelope["sections"], dict):
        raise EnvelopeValidationError("Envelope sections must be a dict")
    if not isinstance(envelope["section_meta"], dict):
        raise EnvelopeValidationError("Envelope section_meta must be a dict")
    if not isinstance(envelope["failed_sections"], list):
        raise EnvelopeValidationError("Envelope failed_sections must be a list")

    ic_result = envelope["ic_result"]
    if not isinstance(ic_result, dict):
        raise EnvelopeValidationError("Envelope ic_result must be a dict")
    for key in ("hmac", "engine_version", "command", "run_id"):
        if not ic_result.get(key):
            raise EnvelopeValidationError(f"Envelope ic_result missing {key}")

    expected = sign_envelope(envelope)
    if not hmac.compare_digest(str(ic_result["hmac"]), expected):
        raise EnvelopeValidationError("Envelope HMAC validation failed")


def envelope_freshness(envelope: Envelope, section: str) -> Freshness:
    """Return freshness state for a section based on its per-section TTL."""
    if section in envelope.get("failed_sections", []):
        return "missing"
    if section not in envelope.get("sections", {}):
        return "missing"

    meta = envelope.get("section_meta", {}).get(section)
    if not meta:
        return "missing"

    computed_at = meta.get("computed_at")
    ttl_seconds = meta.get("ttl_seconds")
    if not computed_at or ttl_seconds is None:
        return "missing"

    try:
        computed = _parse_iso_timestamp(str(computed_at))
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        return "missing"

    age = (datetime.now(timezone.utc) - computed).total_seconds()
    return "fresh" if age <= ttl else "stale"


def new_ic_result(command: str, run_id: str) -> IcResult:
    """Build an unsigned ic_result block for a new envelope."""
    return {
        "hmac": "",
        "engine_version": __version__,
        "command": command,
        "run_id": run_id,
    }

