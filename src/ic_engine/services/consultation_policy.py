#!/usr/bin/env python3
# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
Consultation Policy — single source of truth for consultative LLM behavior.

This module centralises every decision about whether and how to invoke a
local Ollama consultation model.  All other modules (router, command builders,
enrichment client) import from here rather than reading env vars directly.

Environment variables consumed:
  INVESTORCLAW_CONSULTATION_ENABLED   "true" to activate (default: false)
  INVESTORCLAW_CONSULTATION_ENDPOINT  Ollama base URL (default: http://localhost:11434)
  INVESTORCLAW_CONSULTATION_MODEL     Model tag      (default: gemma4-consult)

gemma4-consult is a tuned Ollama derivative of gemma4:e4b (num_ctx=4096,
num_predict=1200, ~65 tok/s on RTX 4500 Ada 24 GB).

Tested models (others will likely work):
  gemma4-consult   — recommended; tuned gemma4:e4b, fast low-latency Q&A
  gemma4:e4b       — base model; 128K ctx, good quality/speed tradeoff
  nemotron-3-nano  — suitable for lower-VRAM setups
  qwen2.5:14b      — solid alternative
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request


def _is_http_url(url: str) -> bool:
    """Return True only if url uses http/https scheme."""
    try:
        return urllib.parse.urlparse(url).scheme in ("http", "https")
    except Exception:
        return False


from pathlib import Path

logger = logging.getLogger(__name__)

# Per-process session key used when INVESTORCLAW_CONSULTATION_HMAC_KEY is not
# set.  Non-forgeable (random per invocation) but consistent within a session.
_SESSION_HMAC_KEY: bytes = secrets.token_bytes(32)

# ---------------------------------------------------------------------------
# Commands that support --tier3 consultation injection
# ---------------------------------------------------------------------------
_TIER3_COMMANDS: frozenset[str] = frozenset({"analyst", "analysts", "ratings"})

# Maximum symbols passed to the consultation model per command invocation.
# Capped to avoid excessive inference latency on large portfolios.
CONSULTATION_SYMBOL_LIMIT: int = 20


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_consultation_enabled() -> bool:
    """Return True when the user has opted in to local LLM consultation."""
    return os.environ.get("INVESTORCLAW_CONSULTATION_ENABLED", "").lower() == "true"


def get_consultation_endpoint() -> str:
    """Return the Ollama endpoint URL (trailing slash stripped)."""
    return os.environ.get("INVESTORCLAW_CONSULTATION_ENDPOINT", "http://localhost:11434").rstrip(
        "/"
    )


def get_consultation_model() -> str:
    """Return the Ollama model tag to use for consultation."""
    return os.environ.get("INVESTORCLAW_CONSULTATION_MODEL", "gemma4-consult")


def should_inject_tier3(command: str) -> bool:
    """Return True when --tier3 should be appended to the command's script args.

    Decision logic (tool-agnostic):
      1. Command must be in tier3-eligible set (analyst, analysts, ratings)
      2. Either:
         a) INVESTORCLAW_CONSULTATION_ENABLED=true explicitly, OR
         b) Endpoint is probed and reachable (enables enrichment even if env var
            is not propagated to subprocess)

    This makes the system resilient to subprocess env var propagation issues
    while respecting explicit user choices.
    """
    if command not in _TIER3_COMMANDS:
        return False

    # Check explicit enable first
    if is_consultation_enabled():
        return True

    # Fall back to endpoint probe (tool-agnostic): if endpoint is reachable, enable
    endpoint = get_consultation_endpoint()
    if _probe_endpoint(endpoint):
        logger.debug("Enrichment enabled: endpoint %s is reachable (agnostic mode)", endpoint)
        return True

    return False


def get_consultation_limit(command: str) -> int:
    """Return the symbol cap for consultation on this command (0 = not applicable)."""
    return CONSULTATION_SYMBOL_LIMIT if command in _TIER3_COMMANDS else 0


def get_dynamic_consultation_limit(position_count: int) -> int:
    """Return enrichment symbol cap scaled to portfolio size."""
    if position_count <= 20:
        return position_count
    if position_count <= 50:
        return 30
    if position_count <= 150:
        return 40
    return CONSULTATION_SYMBOL_LIMIT  # 200+: cap at 20


def _probe_endpoint(endpoint: str, timeout: int = 2) -> bool:
    """Probe endpoint to check if it's reachable (Ollama or OpenAI-compatible).

    Tries:
      1. /api/tags (both Ollama and llama-server)
      2. /health (llama-server)
      3. /v1/models (OpenAI-compatible fallback)

    Returns True if any probe succeeds within timeout.
    """
    endpoint = endpoint.rstrip("/")
    paths = ["/api/tags", "/health", "/v1/models"]

    for path in paths:
        url = f"{endpoint}{path}"
        if not _is_http_url(url):
            continue
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, Exception):
            continue

    return False


def update_session_fingerprint(prev_fp: str, symbol: str, synthesis: str) -> str:
    """Chain HMAC: HMAC-SHA256(key, prev_fp + symbol + synthesis)[:16]."""
    raw = os.environ.get("INVESTORCLAW_CONSULTATION_HMAC_KEY", "").encode()
    key = raw if raw else _SESSION_HMAC_KEY
    msg = f"{prev_fp}{symbol}{synthesis}".encode()
    return _hmac.new(key, msg, hashlib.sha256).hexdigest()[:16]


def get_enrichment_status(reports_dir: Path) -> dict:
    """Read enrichment_progress.json and return a status dict with liveness check."""
    progress_file = reports_dir / ".raw" / "enrichment_progress.json"
    defaults = {
        "enriched_count": 0,
        "total_symbols": 0,
        "enriched_pct": 0.0,
        "in_progress": False,
        "background_pid": None,
        "session_fingerprint": "0000000000000000",
        "bonds_covered": False,
        "stalled": False,
        "display": "⚠️ Enrichment status unknown",
    }
    if not progress_file.exists():
        return defaults

    try:
        with open(progress_file) as f:
            prog = json.load(f)
    except Exception:
        return defaults

    enriched_count = prog.get("enriched_count", 0)
    total_symbols = prog.get("total_symbols", 0)
    in_progress = prog.get("in_progress", False)
    background_pid = prog.get("background_pid")
    session_fp = prog.get("session_fingerprint", "0000000000000000")
    bonds_covered = prog.get("bonds_covered", False)
    stalled = False

    enriched_pct = round(enriched_count / total_symbols * 100, 1) if total_symbols else 0.0

    # Check PID liveness
    # Note: PID recycling could cause stale PIDs to appear alive.
    # This check is a liveness hint only, not a security control.
    if in_progress and background_pid:
        try:
            os.kill(background_pid, 0)
        except (ProcessLookupError, PermissionError):
            stalled = True
            in_progress = False

    fp_short = session_fp[:8] if session_fp else "00000000"
    if in_progress:
        display = f"⏳ Enrichment: {enriched_count}/{total_symbols} · {enriched_pct}% · {fp_short} · updating"
    elif stalled:
        display = f"⚠️ Enrichment: {enriched_count}/{total_symbols} · {enriched_pct}% · {fp_short} · stalled"
    elif enriched_count >= total_symbols and total_symbols > 0:
        display = f"✅ Enrichment: {enriched_count}/{total_symbols} · {enriched_pct}% · {fp_short} · complete"
    else:
        display = f"✅ Enrichment: {enriched_count}/{total_symbols} · {enriched_pct}% · {fp_short}"

    return {
        "enriched_count": enriched_count,
        "total_symbols": total_symbols,
        "enriched_pct": enriched_pct,
        "in_progress": in_progress,
        "background_pid": background_pid,
        "session_fingerprint": session_fp,
        "bonds_covered": bonds_covered,
        "stalled": stalled,
        "display": display,
    }
