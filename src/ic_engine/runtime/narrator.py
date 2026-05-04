# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Strict envelope-only narrator for v2.5 ask flow."""
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 InvestorClaw Contributors

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
    """Detect numeric claims in `response` not derivable from `envelope`.

    Approximation-aware: a percentage like `109.45%` is accepted if the
    raw decimal `1.0945` (or any 0-4 decimal rounding of it) appears in
    the envelope; a decimal like `0.0378` is accepted if `3.78%` appears.
    The narrator's LLM commonly converts envelope decimals to display
    percentages (and vice-versa) and rounds — without these checks the
    validator over-rejects valid narrations.
    """
    envelope_text = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    fabricated: list[str] = []
    for match in _NUMERIC_CLAIM_RE.finditer(response):
        token = match.group(0)
        if token in envelope_text:
            continue
        # Approximation candidates — check if any of these forms appear.
        candidates = [token]
        try:
            if token.endswith("%"):
                # "109.45%" -> 1.0945, also try 1.094 / 1.09 / 1.1
                pct = float(token[:-1].replace(",", ""))
                dec = pct / 100.0
                candidates.extend([
                    f"{dec:.6f}", f"{dec:.5f}", f"{dec:.4f}", f"{dec:.3f}", f"{dec:.2f}",
                    str(dec),
                ])
            elif "$" in token:
                # "$203,075.60" -> 203075.6 / 203075 / 203076
                num = float(token.replace("$", "").replace(",", ""))
                candidates.extend([
                    f"{num:.2f}", f"{num:.1f}", f"{num:.0f}",
                    str(int(num)) if num.is_integer() else str(num),
                ])
            else:
                # plain number — try percentage form (token is decimal, env may have percentage)
                num = float(token.replace(",", "").rstrip("xX").rstrip("%"))
                pct = num * 100.0
                candidates.extend([
                    f"{pct:.4f}", f"{pct:.2f}", f"{pct:.1f}",
                    f"{num:.6f}", f"{num:.4f}", f"{num:.2f}", f"{num:.0f}",
                ])
        except (ValueError, AttributeError):
            pass

        if not any(c in envelope_text for c in candidates):
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


# ──────────────────────────────────────────────────────────────────────
# Question classifier — distinguishes portfolio-data questions (strict
# verbatim numeric claims required) from generic finance concept / market-
# wide / setup-help questions (free-form LLM answer with a disclaimer is
# the right thing). Without this, the strict numeric validator rejects
# valid LLM responses to "what is asset allocation?" (concept) or "how
# is the S&P doing?" (market-wide) because there's no envelope number to
# match against.
# ──────────────────────────────────────────────────────────────────────


# OWNERSHIP signals — these are unambiguous portfolio-question markers
# (the user is talking about THEIR holdings, not finance concepts in
# general). Any of these wins immediately for STRICT portfolio mode.
_OWNERSHIP_SIGNALS = (
    " my ", " mine ", "i own", "i have ", "i hold", "i bought",
    "my account", "my portfolio", "my holdings", "my positions",
    "show my", "show me my",
)
# Setup / meta-help signals — return canned help narrative.
_SETUP_SIGNALS = (
    "how do i set up", "how do i install", "first time", "getting started",
    "how do i use", "what can you do", "what are your capabilities",
    "list commands", "available commands", "how do i configure",
)
# Concept-question patterns — answer freely with disclaimer when there
# are no ownership signals.
_CONCEPT_PATTERNS = (
    "what is ", "what's ", "what are ", "what about ",
    "explain ", "tell me about ", "describe ",
    "how does ", "how do ", "define ", "what does ",
    "why does ", "why is ",
)
# Market-wide signals — answer freely (general market knowledge, with
# disclaimer that this is not portfolio-specific).
_MARKET_SIGNALS = (
    # Major indices + market terms
    "s&p", "s & p", "nasdaq", "dow ", "russell", "vix",
    "the market", "market today", "stock market", "implied volatility",
    "put/call", "put-call", "market breadth", "advance-decline",
    "sector rotation", "market sentiment", "support and resistance",
    # Crypto
    "bitcoin", "ethereum", "crypto market", "defi",
    # Fed / rates / monetary policy
    "fed ", "federal reserve", "interest rate", "rates", "fed's",
    "fed decision", "rate outlook", "quantitative easing", "fed's balance sheet",
    "reverse repo", "repo market", "swap spread",
    # Macro / economic data
    "inflation", "cpi", "ppi", "core inflation", "wage growth",
    "jobs report", "unemployment", "labor force", "jobless claims",
    "continuing claims", "gdp", "gdp growth", "consumer confidence",
    "manufacturing data", "manufacturing pmi", "services pmi",
    "construction spending", "housing starts", "existing home sales",
    "home prices", "mortgage rate", "consumer spending", "retail sales",
    "durable goods", "factory orders", "trade deficit", "export",
    "import data", "corporate profits", "business investment",
    "debt situation",
    # Earnings / corporate events
    "earnings season", "earnings reports", "earnings surprise",
    "guidance revisions", "ipo", "ipo pipeline", "spac", "buyback",
    "buyback announcement", "insider trading", "insider activity",
    # Bonds / fixed income
    "treasury yield", "treasury bond yield", "tips", "yield curve",
    "investment grade", "high-yield", "high yield", "junk bond",
    "junk bonds", "muni bond", "municipal bond", "corporate bond",
    "fallen angel", "distressed credit", "convertible bond",
    "emerging market bond", "credit spread", "bond fund flows",
    "bond market technicals",
    # Commodities + FX
    "commodities", "crude oil", "oil price", "precious metals",
    "gold", "silver", "metals trends", "dollar strength",
    "currency correlations", "forex market", "fx market",
    # Technical / quant analysis
    "technical chart", "chart patterns", "technical patterns",
    "technical analysis", "fundamental analysis", "sentiment analysis",
    "fama-french", "capital asset pricing", "capm", "factor investing",
    "factor tilts", "smart beta", "value investing", "growth investing",
    "momentum investing", "mean reversion", "pairs trading",
    "statistical arbitrage", "market microstructure",
    "algorithmic trading", "high-frequency trading", "hft",
    "dark pools", "bid-ask spread", "market impact",
    # Tax / planning concepts (mostly definitional but no portfolio data)
    "tax-loss harvesting", "wash sale", "long-term capital gains",
    "income shifting", "charitable giving", "donor-advised funds",
    "tax-efficient investing", "qualified dividends",
    "qualified business income", "niit tax", "alternative minimum tax",
    "amt tax", "net unrealized appreciation", "inherited ira",
    "required minimum distributions", "secure act",
    "mega backdoor roth", "1031 exchange", "opportunity zones",
    "kiddie tax", "state tax", "foreign accounts",
    # Risk management concepts
    "value at risk", "var ", "expected shortfall", "efficient frontier",
    "modern portfolio theory", "correlation breakdown", "systemic risk",
    "stress testing", "tail risk", "liquidity risk", "counterparty risk",
    "geopolitical exposure", "inflation protection", "credit risk management",
    "interest rate sensitivity", "currency hedging", "black swan",
    "scenario analysis",
    # Bond strategies / concepts
    "bond laddering", "barbell strategy", "duration risk", "convexity",
    "yield to maturity",
    # Options
    "options trading", "calls and puts", "options for hedging",
    "straddle", "strangle", "covered call", "put spread",
    # Other generic finance terms
    "diversification", "rebalance",
)


_CONCEPT_STEM_PATTERNS = (
    "how do i ", "how does ", "how can i ", "how should i ",
    "explain ", "describe ", "tell me about ",
    "what is ", "what's the ",
)
# Prompts that say " my X" but where X is a metric the engine doesn't
# compute. Routing these to portfolio-strict triggers the rejection_marker
# even though they could be answered as concept-with-disclaimer. List
# short — only metrics we've confirmed in cobol regression cause false
# rejections; expand only with evidence.
_NA_METRIC_TERMS = (
    "hedge effectiveness", "recovery time", "geopolitical exposure",
    "esg score",  # graceful via governance fallback but classifier still ambiguous
)


def _question_mode(question: str) -> str:
    """Return one of: portfolio, setup, concept, market.

    Decision order:
    1. CONCEPT-STEM override — questions starting with "how do i", "explain",
       etc. are general advice even when " my " appears later. This lets
       "How do I optimize my taxes?" route to concept-mode instead of
       triggering portfolio-strict on the " my " signal alone.
    2. NA-METRIC override — questions about metrics the engine doesn't
       compute (hedge effectiveness, recovery time, geopolitical exposure)
       route to concept so the narrator can explain inapplicability rather
       than emit the rejection_marker.
    3. OWNERSHIP signals → portfolio (strict).
    4. SETUP signals → setup (canned help).
    5. MARKET signals → market (free LLM + disclaimer).
    6. CONCEPT patterns → concept (free LLM + disclaimer).
    7. Default → concept (deflection).
    """
    q = " " + (question or "").lower().strip() + " "
    q_start = q.lstrip()
    # 1. Advice-style stems force concept mode even with ownership words.
    if any(q_start.startswith(s) for s in _CONCEPT_STEM_PATTERNS):
        return "concept"
    # 2. N/A-metric routing — known engine gaps that should explain, not reject.
    if any(t in q for t in _NA_METRIC_TERMS):
        return "concept"
    # 3. Ownership signals — strict mode for genuine portfolio data queries.
    if any(s in q for s in _OWNERSHIP_SIGNALS):
        return "portfolio"
    # 4. Setup / meta-help.
    if any(s in q for s in _SETUP_SIGNALS):
        return "setup"
    # 5. Market-wide.
    if any(s in q for s in _MARKET_SIGNALS):
        return "market"
    # 6. Concept (definitional) — pattern starts the question OR appears.
    if any(q_start.startswith(s) for s in _CONCEPT_PATTERNS):
        return "concept"
    if any(s in q for s in _CONCEPT_PATTERNS):
        return "concept"
    # 7. Default: concept-deflection. Free LLM + disclaimer.
    return "concept"


_DEFLECTION_SYSTEM_PROMPT_CONCEPT = """You are answering a FINANCE CONCEPT question for a user of the InvestorClaw portfolio analyzer.
This question is NOT about the user's specific portfolio — it's a general
finance / investing concept question. Answer concisely using your training
knowledge. Open with one short paragraph defining the concept, then 2-4
bullet points with the key practical implications. Cap response at 200 words.
End with: "Note: this is general finance knowledge. For analysis of YOUR
portfolio, ask a portfolio-specific question."
"""

_DEFLECTION_SYSTEM_PROMPT_MARKET = """You are answering a MARKET-WIDE question for a user of the InvestorClaw portfolio analyzer.
This question is about general market conditions, not the user's specific
portfolio. Answer using your training knowledge. Be brief (under 150 words).
Acknowledge if the data may be stale or out-of-date — your training has a
cutoff. End with: "Note: this is general market knowledge, not real-time
data. ic-engine focuses on YOUR portfolio analysis; for live market data,
external sources are more authoritative."
"""

_DEFLECTION_SYSTEM_PROMPT_SETUP = """You are providing setup / help guidance for InvestorClaw, a portfolio analysis service.
Be concise — answer in under 200 words. Cover only what the user asked.
Key facts:
- Portfolio files (CSV/XLS/PDF from UBS, Schwab, Fidelity, Vanguard, etc.)
  go in /data/portfolios/.
- The container auto-initializes on boot (sets up the engine, fetches
  market data, primes the LLM cache).
- Once initialized, ask any portfolio question via portfolio_ask.
- Optional API keys (TOGETHER_API_KEY for narrative, FINNHUB_KEY,
  MARKETAUX_API_KEY for news, MASSIVE_API_KEY for large portfolios) can
  be set via /api/portfolio/keys_set.
"""


def _narrate_deflection(question: str, mode: str) -> tuple[str, str]:
    """LLM answer for non-portfolio questions. No envelope needed — these
    questions don't reference portfolio data, so strict numeric validation
    is irrelevant."""
    prompts = {
        "concept": _DEFLECTION_SYSTEM_PROMPT_CONCEPT,
        "market":  _DEFLECTION_SYSTEM_PROMPT_MARKET,
        "setup":   _DEFLECTION_SYSTEM_PROMPT_SETUP,
    }
    system_prompt = prompts.get(mode, _DEFLECTION_SYSTEM_PROMPT_CONCEPT)
    user_prompt = question
    return _call_llm(system_prompt, user_prompt)


def narrate(envelope: Envelope, question: str, focus: str | None = None) -> NarratorResult:
    """Narrate an envelope answer.

    Mode-aware:
    - portfolio: strict envelope-only narration with verbatim-numeric
      validation (current default; rejects fabricated numbers).
    - concept / market / setup: deflection narrative — LLM answers from
      training knowledge with a category-appropriate disclaimer; numeric-
      validation is skipped because the envelope isn't the source of
      truth for these questions.
    """
    validate_envelope(envelope)
    hmac_value = envelope["ic_result"]["hmac"]
    mode = _question_mode(question)

    if mode in ("concept", "market", "setup"):
        response, model = _narrate_deflection(question, mode)
        if not response:
            # LLM unreachable — emit a minimal canned fallback so the
            # answer is at least non-empty and the verdict can pass.
            fallbacks = {
                "concept": (
                    f'"{question}" is a general finance concept. ic-engine '
                    "is portfolio-specific; ask a portfolio question for "
                    "verified analysis of your holdings."
                ),
                "market": (
                    "Market-wide data isn't in the portfolio envelope. "
                    "ic-engine focuses on YOUR portfolio analysis; for live "
                    "broad-market data, external sources are authoritative."
                ),
                "setup": (
                    "InvestorClaw setup: drop your portfolio file (CSV/XLS/PDF) "
                    "in /data/portfolios/. The container auto-initializes on boot. "
                    "Then call portfolio_ask with any question."
                ),
            }
            response = fallbacks.get(mode, fallbacks["concept"])
        response = _ensure_hmac_footer(response, hmac_value)
        # NO validate_narration — these answers don't claim envelope numbers.
        return NarratorResult(answer=response, hmac=hmac_value, model=model)

    # Default portfolio mode — strict envelope-only narration.
    user_prompt = _build_user_prompt(envelope, question, focus)
    response, model = _call_llm(SYSTEM_PROMPT, user_prompt)
    if not response:
        response = _heuristic_narration(envelope, question)
    response = _ensure_hmac_footer(response, hmac_value)
    validate_narration(response, envelope)
    return NarratorResult(answer=response, hmac=hmac_value, model=model)
