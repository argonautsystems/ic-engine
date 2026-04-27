# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Canonical user-facing messages for ic-engine.

Single source of truth so every adapter (InvestorClaw, InvestorClaude,
OpenClaw, ZeroClaw, Hermes, standalone) shows identical wait/error
text. Imported from this module rather than duplicated per-adapter.

Introduced 2026-04-27 as part of the v2.5 deterministic-first
architecture: the ic-engine pre-runs the full pipeline before the
narrator LLM ever sees the user's prompt, which means there's a 30-60s
wait the first time. Users need to see consistent, honest messaging
during that wait — not a hung session that looks broken.
"""

from __future__ import annotations

# ─── First-run wait (cache miss, full pipeline kicks off) ────────────
# Verbose form (variant C) — chosen as canonical because users seeing a
# 30-60s wait need to understand WHY (deterministic pipeline, multiple
# authoritative sources) so the wait reads as discipline, not hang.
WAIT_FIRST_RUN = (
    "Running your portfolio analysis through ic-engine. The deterministic "
    "pipeline is computing holdings, performance, bonds, analyst data, "
    "news, and risk synthesis from authoritative sources — this takes "
    "30-60 seconds the first time you ask. Subsequent questions in this "
    "session will use the cached data and respond instantly. Please wait."
)

# ─── Refresh requested (user explicitly forced a new run) ────────────
WAIT_REFRESH = (
    "Refreshing your portfolio analysis. 30-60 seconds. Please wait."
)

# ─── Partial refresh (only one section stale, e.g. news every 30s) ──
WAIT_PARTIAL_REFRESH = (
    "{section} data was stale — refreshing ({eta_seconds}s). "
    "Other portfolio data is current."
)

# ─── Cache hit (instant; optional confirmation of provenance) ───────
CACHE_HIT_BANNER = (
    "Using cached portfolio analysis from {age_seconds}s ago "
    "(envelope hash: {envelope_hmac_short}). "
    "Run /investorclaw:refresh to force a new run."
)

# ─── Partial pipeline failure (one or more sections couldn't run) ───
PIPELINE_PARTIAL_FAILURE = (
    "ic-engine completed your portfolio analysis with [{failed_sections}] "
    "unavailable: {failure_reasons}. Other sections are current and you "
    "can ask about them. Run /investorclaw:refresh to retry."
)

# ─── Total pipeline failure (no usable envelope produced) ───────────
PIPELINE_TOTAL_FAILURE = (
    "ic-engine could not produce a portfolio analysis: {error}. "
    "No data is available — please verify your portfolio file at "
    "{portfolio_path} and try /investorclaw:refresh."
)

# ─── Refusal mode (narrator: question outside envelope scope) ───────
NARRATOR_OUT_OF_SCOPE = (
    "I don't have data on that — it requires running [{required_command}]. "
    "Run /investorclaw:refresh first, or ask about portfolio sections "
    "I do have data for: {available_sections}."
)

# ─── Refusal mode (narrator: prompt would require fabrication) ──────
NARRATOR_FABRICATION_REFUSAL = (
    "I cannot answer that without making up numbers. ic-engine only "
    "returns data computed from your authoritative sources. The data "
    "you're asking about ({missing_data_class}) isn't in this run's "
    "envelope. Run /investorclaw:refresh to fetch it, or ask a different "
    "question."
)


__all__ = [
    "WAIT_FIRST_RUN",
    "WAIT_REFRESH",
    "WAIT_PARTIAL_REFRESH",
    "CACHE_HIT_BANNER",
    "PIPELINE_PARTIAL_FAILURE",
    "PIPELINE_TOTAL_FAILURE",
    "NARRATOR_OUT_OF_SCOPE",
    "NARRATOR_FABRICATION_REFUSAL",
]
