# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Strict envelope-only narrator for v2.5 ask flow."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ic_engine.config.user_messages import NARRATOR_FABRICATION_REFUSAL
from ic_engine.runtime.envelope import Envelope, validate_envelope

OUT_OF_SCOPE_RESPONSE = "I don't have data on that — run /investorclaw:refresh to fetch it"

SYSTEM_PROMPT = """You are narrating financial data for the user.
CRITICAL RULES:
1. Use ONLY data from this JSON envelope. Quote ALL numbers VERBATIM.
2. If the user asks about something not in the envelope, respond:
   "I don't have data on that — run /investorclaw:refresh to fetch it"
3. NEVER infer, estimate, supplement, or substitute from training data.
4. NEVER round or rephrase numbers — quote them exactly as in the JSON.
5. Include the envelope's ic_result.hmac in your response footer.
"""

_NUMERIC_CLAIM_RE = re.compile(
    r"(?<![\w])\$-?\d[\d,]*(?:\.\d+)?(?:[kKmMbB])?\b"
    r"|(?<![\w])-?\d[\d,]*(?:\.\d+)?%"
    r"|(?<![\w])-?\d+(?:\.\d+)?x\b"
    r"|\b\d+(?:\.\d+)?:\d+(?:\.\d+)?\b"
)


@dataclass(frozen=True)
class NarratorResult:
    answer: str
    hmac: str
    model: str
    validation_passed: bool = True


class FabricationError(ValueError):
    """Raised when narration contains numbers absent from the envelope."""

    def __init__(self, tokens: list[str], response: str):
        super().__init__(f"Narrator fabricated numeric claims: {tokens}")
        self.tokens = tokens
        self.response = response


_PER_SECTION_TOP_N = 25
_MAX_ARTICLE_FIELDS = ("title", "source", "published", "summary")


def _compact_section(name: str, data: Any) -> Any:
    """Trim a section's payload so the whole envelope fits in a 192k-token
    context window. We keep:

    - Any `summary` / `meta` / `as_of` / scalar top-level fields verbatim
    - Top-N (default 25) for list-shaped fields (top_equity, top_bonds, news)
    - Truncate news article bodies to title+source+published+summary
    - Drop bulky raw-data fields (`output_file`, `_raw`, `raw`, `*_full`)

    Numeric-fabrication validation in `validate_narration` runs against the
    FULL envelope (not the compacted one), so dropping a position from the
    LLM's view doesn't let the LLM invent numbers — the validator still
    sees the original.
    """
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        # Drop bulky raw-data pointers
        if key in {"output_file", "_raw", "raw"} or key.endswith("_full"):
            continue
        if isinstance(value, list):
            # Cap list length per top-level field
            trimmed = value[:_PER_SECTION_TOP_N]
            # News articles: keep only display-relevant fields
            if name == "news" and trimmed and isinstance(trimmed[0], dict):
                trimmed = [
                    {k: v for k, v in item.items() if k in _MAX_ARTICLE_FIELDS}
                    for item in trimmed
                ]
            out[key] = trimmed
        elif isinstance(value, dict):
            # Keep dict but recursively cap any nested lists
            out[key] = _compact_section(name, value)
        else:
            out[key] = value
    return out


def compact_envelope(envelope: Envelope) -> dict:
    """Return a narrator-friendly view of the envelope.

    Mutates nothing. Caps each section to top-25 items per list field and
    drops bulky raw-data pointers. Keeps ic_result, schema_version,
    portfolio_id, generated_at, section_meta, failed_sections verbatim.
    """
    compacted: dict[str, Any] = {
        k: v for k, v in envelope.items() if k != "sections"
    }
    sections = envelope.get("sections", {}) or {}
    compacted["sections"] = {
        name: _compact_section(name, data) for name, data in sections.items()
    }
    return compacted


def _build_user_prompt(envelope: Envelope, question: str, focus: str | None = None) -> str:
    focus_line = f"\nFocus section: {focus}" if focus else ""
    compacted = compact_envelope(envelope)
    envelope_json = json.dumps(compacted, indent=2, sort_keys=True, ensure_ascii=False)
    return (
        f"User question: {question}{focus_line}\n\n"
        "JSON envelope follows. Do not use any source outside this envelope.\n"
        f"{envelope_json}"
    )


def _call_llm(system_prompt: str, user_prompt: str) -> tuple[str, str]:
    """Call the configured ic-engine LLM client.

    Failures here cause silent fall-through to the heuristic narrator, which
    emits the catalog blurb "Envelope sections available: ..." that is easy
    to mistake for a real answer in passing tests. Always log the exception
    so a missing dep, bad endpoint, or bad key surfaces in logs even when
    the heuristic fallback succeeds at returning *something*.
    """
    import logging
    import os
    log = logging.getLogger(__name__)
    try:
        from ic_engine.internal.litellm_consultation import LiteLLMConsultationClient

        # Narrator prefers the NARRATIVE_* config (long-context narrative
        # model, e.g. Together MiniMax-M2.7) over the CONSULTATION_* default
        # (local gemma4 with 32k context — the envelope can exceed 200k
        # tokens). Falls back to CONSULTATION_* if NARRATIVE_* is unset.
        client = LiteLLMConsultationClient(
            endpoint=os.environ.get("INVESTORCLAW_NARRATIVE_ENDPOINT") or None,
            model=os.environ.get("INVESTORCLAW_NARRATIVE_MODEL") or None,
            api_key=os.environ.get("INVESTORCLAW_NARRATIVE_API_KEY") or None,
        )
        result = client.complete(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout=120,
            temperature=0.0,
            top_p=1.0,
            max_tokens=1200,
        )
        if not result.response:
            log.warning(
                "narrator._call_llm: empty response from %s (heuristic fallback)",
                result.model,
            )
        return result.response, result.model
    except Exception as exc:
        log.warning("narrator._call_llm: %s: %s — falling back to heuristic",
                    type(exc).__name__, exc)
        return "", "heuristic"


def _find_first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _heuristic_narration(envelope: Envelope, question: str) -> str:
    """Envelope-only fallback used when no configured LLM is reachable."""
    del question
    holdings = envelope.get("sections", {}).get("holdings", {}) or {}
    portfolio = holdings.get("portfolio")
    summary = _find_first_dict(
        holdings.get("summary"),
        portfolio.get("summary") if isinstance(portfolio, dict) else {},
    )
    top_holdings = holdings.get("top_equity") or holdings.get("top_holdings") or []
    available = ", ".join(section for section, data in envelope.get("sections", {}).items() if data)

    lines = []
    if summary:
        lines.append("I have holdings summary data in the envelope.")
        for key, value in list(summary.items())[:8]:
            if isinstance(value, (str, int, float, bool)) or value is None:
                lines.append(f"- {key}: {value}")
    elif top_holdings:
        lines.append("I have top holdings data in the envelope.")
    elif available:
        lines.append(f"Envelope sections available: {available}.")
    else:
        lines.append(OUT_OF_SCOPE_RESPONSE)

    if isinstance(top_holdings, list) and top_holdings:
        symbols = [
            str(item.get("symbol"))
            for item in top_holdings[:10]
            if isinstance(item, dict) and item.get("symbol")
        ]
        if symbols:
            lines.append(f"Top holding symbols in the envelope: {', '.join(symbols)}.")
    return "\n".join(lines)


def _ensure_hmac_footer(response: str, hmac_value: str) -> str:
    if hmac_value in response:
        return response.strip()
    return f"{response.strip()}\n\nic_result.hmac: {hmac_value}"


def _fabricated_numeric_claims(response: str, envelope: Envelope) -> list[str]:
    envelope_text = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    fabricated: list[str] = []
    for match in _NUMERIC_CLAIM_RE.finditer(response):
        token = match.group(0)
        if token not in envelope_text:
            fabricated.append(token)
    return fabricated


def validate_narration(response: str, envelope: Envelope) -> None:
    """Ensure dollar amounts, percentages, and ratios are verbatim envelope values."""
    fabricated = _fabricated_numeric_claims(response, envelope)
    if fabricated:
        raise FabricationError(fabricated, response)


def fabrication_refusal(missing_data_class: str = "requested numeric data") -> str:
    """Return the canonical narrator fabrication refusal text."""
    return NARRATOR_FABRICATION_REFUSAL.format(missing_data_class=missing_data_class)


def narrate(envelope: Envelope, question: str, focus: str | None = None) -> NarratorResult:
    """Narrate an envelope answer while enforcing verbatim numeric claims."""
    validate_envelope(envelope)
    hmac_value = envelope["ic_result"]["hmac"]
    user_prompt = _build_user_prompt(envelope, question, focus)
    response, model = _call_llm(SYSTEM_PROMPT, user_prompt)
    if not response:
        response = _heuristic_narration(envelope, question)
    response = _ensure_hmac_footer(response, hmac_value)
    validate_narration(response, envelope)
    return NarratorResult(answer=response, hmac=hmac_value, model=model)
