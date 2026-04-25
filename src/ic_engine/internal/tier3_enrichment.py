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
Tier 3 Enrichment — consultative inference via local LLM.

Provides:
  ConsultationClient  — local inference wrapper (Ollama or OpenAI-compatible)
  Tier3Enricher       — enriches AnalystConsensus objects with LLM synthesis

Activated when INVESTORCLAW_CONSULTATION_ENABLED=true.
Environment variables:
  INVESTORCLAW_CONSULTATION_ENDPOINT  (default: http://localhost:11434)
  INVESTORCLAW_CONSULTATION_MODEL     (default: gemma4-consult)
  INVESTORCLAW_CARD_FORMAT            (default: both)
    Controls what artifact is written per enriched symbol:
      json  — write ~/.investorclaw/quotes/{SYMBOL}.quote.json only; no SVG.
              Safe for mobile/messaging channels (WhatsApp, Signal, Telegram)
              where SVG is unsupported or undesirable.
      svg   — write SVG card only (requires INVESTOR_CLAW_REPORTS_DIR).
              Legacy behaviour; no persistent text artifact.
      both  — write JSON quote file always + SVG if INVESTOR_CLAW_REPORTS_DIR
              is set. Default for desktop/web sessions.

Backend auto-detection:
  The client probes the endpoint on first use and selects the API format:
    Ollama  — /api/tags reachable → uses /api/generate (legacy)
    OpenAI  — /health or /v1/models reachable → uses /v1/chat/completions

  Recommended production backend: llama-server (llama.cpp build ≥ 1144)
    INVESTORCLAW_CONSULTATION_ENDPOINT=http://your-gpu-host:8080

  GPU Host (RTX 4500 Ada 24 GB or similar) — llama-server config:
    Model: google_gemma-4-E4B-it-Q6_K.gguf (5.9 GB)
    Context: 131072 tokens (128K) — 32× improvement over previous 4K Ollama config
    KV cache: q8_0 (halved size vs F16)
    Speed: ~64 tok/s
    Service: sudo systemctl start llama-gemma4

Tested models:
  gemma4-consult   — recommended; gemma4:e4b tuned, fast (~64 tok/s), 128K ctx
  gemma4:e4b       — base model (Ollama), 128K ctx
  nemotron-3-nano  — suitable for lower-VRAM setups
  qwen2.5:14b      — solid alternative
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# Optional: Use LiteLLM client for automatic backend detection (fallback to custom ConsultationClient)
try:
    from internal.litellm_consultation import LiteLLMConsultationClient as _LiteLLMClient

    _LITELLM_AVAILABLE = True
except ImportError:
    _LITELLM_AVAILABLE = False


def _validate_http_url(url: str) -> None:
    """Raise ValueError if url scheme is not http/https."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Refusing non-http(s) URL scheme: {parsed.scheme!r} in {url!r}")


from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Sentence splitter that avoids breaking on decimal numbers like $587.31
_SENT_RE = re.compile(r"(?<!\d)\.(?!\d)\s*")


def _get_hmac_key() -> bytes:
    key = os.environ.get("INVESTORCLAW_CONSULTATION_HMAC_KEY", "").strip()
    if key:
        return key.encode()
    # Check user-space config, never read or write the repo-local .env
    env_file = Path.home() / ".investorclaw" / ".env"
    if env_file.exists():
        existing = env_file.read_text()
        for line in existing.splitlines():
            if line.strip().startswith("INVESTORCLAW_CONSULTATION_HMAC_KEY="):
                found_key = line.strip().split("=", 1)[1].strip()
                if found_key:
                    os.environ["INVESTORCLAW_CONSULTATION_HMAC_KEY"] = found_key
                    return found_key.encode()
    generated = secrets.token_hex(32)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(env_file.parent, 0o700)
    except OSError:
        pass
    with open(env_file, "a") as f:
        f.write(f"\nINVESTORCLAW_CONSULTATION_HMAC_KEY={generated}\n")
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass
    os.environ["INVESTORCLAW_CONSULTATION_HMAC_KEY"] = generated
    return generated.encode()


def _compute_fingerprint(symbol: str, model: str, synthesis: str) -> str:
    key = _get_hmac_key()
    msg = f"{symbol}|{model}|{synthesis}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass
class ConsultationResult:
    """Result from a single local-inference inference call."""

    response: str
    model: str
    endpoint: str
    inference_ms: int
    is_heuristic: bool = False

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "endpoint": self.endpoint,
            "inference_ms": self.inference_ms,
            "is_heuristic": self.is_heuristic,
        }


@dataclass
class EnrichedAnalystConsensus:
    """AnalystConsensus enriched with LLM synthesis fields."""

    # Mirror of AnalystConsensus core fields
    symbol: str
    current_price: float
    analyst_count: int
    consensus: Optional[str]  # consensus_recommendation
    recommendation_mean: float

    # Enrichment fields
    sentiment_label: str = "neutral"
    sentiment_score: float = 0.0
    recommendation_strength: str = "moderate"
    synthesis: str = ""
    key_insights: List[str] = field(default_factory=list)
    risk_assessment: str = ""
    consultation: Optional[dict] = None
    fingerprint: str = ""
    quote: Optional[dict] = None


# ---------------------------------------------------------------------------
# ConsultationClient
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a financial data analyst providing educational information only. "
    "Your analysis is not investment advice. "
    "Answer in 3-5 sentences for simple questions, or up to 400 words for complex topics. "
    "Lead with the direct answer. Include specific figures, percentages, and named metrics "
    "where available. No preamble, no restating the question."
)

# API format constants
_API_OLLAMA = "ollama"
_API_OPENAI = "openai"


class ConsultationClient:
    """Local inference wrapper supporting both Ollama and OpenAI-compatible backends.

    Backend is auto-detected on first call: if /api/tags responds the client uses
    Ollama /api/generate format; otherwise it falls back to OpenAI /v1/chat/completions.
    """

    def __init__(self) -> None:
        self.endpoint = os.environ.get(
            "INVESTORCLAW_CONSULTATION_ENDPOINT", "http://localhost:11434"
        ).rstrip("/")
        self.model = os.environ.get("INVESTORCLAW_CONSULTATION_MODEL", "gemma4-consult")
        self._api_format: Optional[str] = None  # detected lazily

    def _detect_api_format(self) -> str:
        """Probe endpoint to determine API format (cached after first call).

        Inference engine detection order:
          1. /api/tags with "object":"list" → llama-server/vLLM (OpenAI-compatible)
          2. /api/tags (no "object") → Ollama
          3. /v1/models → OpenAI-compatible (LMStudio, Together.ai, etc.)
          4. /health → OpenAI-compatible health check
          5. Default → OpenAI-compatible (safest fallback)

        LMStudio-specific: May not expose /api/tags, relies on /v1/models + /v1/chat/completions.
        Better error messages guide users to verify:
          - Endpoint URL is correct
          - Local server is enabled (LMStudio: Settings > Developer)
          - Port matches (default: 8000 for LMStudio, 11434 for Ollama, 8080 for llama-server)
        """
        if self._api_format is not None:
            return self._api_format

        # Try Ollama/llama-server registry endpoint first
        try:
            url = f"{self.endpoint}/api/tags"
            _validate_http_url(url)
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read())
                    # Real Ollama never includes "object" key; llama-server does.
                    if body.get("object") == "list":
                        self._api_format = _API_OPENAI
                        logger.debug(
                            "local-inference: detected llama-server (OpenAI) at %s", self.endpoint
                        )
                    else:
                        self._api_format = _API_OLLAMA
                        logger.debug("local-inference: detected Ollama API at %s", self.endpoint)
                    return self._api_format
        except Exception:
            pass

        # Try OpenAI-compatible detection (LMStudio, vLLM, Together.ai, etc.)
        for endpoint_path, engine_hint in [
            ("/v1/models", "LMStudio/llama-server models endpoint"),
            ("/health", "health check"),
        ]:
            try:
                url = f"{self.endpoint}{endpoint_path}"
                _validate_http_url(url)
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        self._api_format = _API_OPENAI
                        logger.debug(
                            "local-inference: detected OpenAI-compatible engine (%s) at %s",
                            engine_hint,
                            self.endpoint,
                        )
                        return self._api_format
            except Exception:
                continue

        # Fall back to OpenAI-compatible (safest for LMStudio, vLLM, etc.)
        self._api_format = _API_OPENAI
        logger.debug(
            "local-inference: using OpenAI API format at %s (auto-detected)", self.endpoint
        )
        return _API_OPENAI

    def is_available(self) -> bool:
        """Probe endpoint — returns True if reachable (Ollama or OpenAI-compatible)."""
        try:
            fmt = self._detect_api_format()
            if fmt == _API_OLLAMA:
                return True  # already verified during detection
            # OpenAI: try /health (llama-server) then /v1/models
            for path in ("/health", "/v1/models"):
                try:
                    url = f"{self.endpoint}{path}"
                    _validate_http_url(url)
                    req = urllib.request.Request(url, method="GET")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        if resp.status == 200:
                            return True
                except Exception:
                    continue
            return False
        except Exception as exc:
            logger.debug("local-inference probe failed: %s", exc)
            return False

    def consult(self, prompt: str, timeout: int = 120) -> ConsultationResult:
        """POST prompt to local inference endpoint and return ConsultationResult.

        Retries once (1 s backoff) on empty response — gemma4-series models
        occasionally burn all num_predict tokens on non-visible formatting tokens
        under rapid sequential inference, returning an empty response with
        done_reason=length. One retry recovers reliably.
        """
        fmt = self._detect_api_format()
        t0 = time.time()
        for attempt in range(2):
            try:
                response = (
                    self._call_ollama(prompt, timeout)
                    if fmt == _API_OLLAMA
                    else self._call_openai(prompt, timeout)
                )
                inference_ms = int((time.time() - t0) * 1000)
                if response:
                    return ConsultationResult(
                        response=response,
                        model=self.model,
                        endpoint=self.endpoint,
                        inference_ms=inference_ms,
                        is_heuristic=False,
                    )
                if attempt == 0:
                    logger.warning(
                        "local-inference returned empty response for %s, retrying (attempt 1/2)",
                        self.model,
                    )
                    time.sleep(1.0)
            except Exception as exc:
                inference_ms = int((time.time() - t0) * 1000)
                logger.warning("local-inference inference failed: %s", exc)
                return ConsultationResult(
                    response="",
                    model=self.model,
                    endpoint=self.endpoint,
                    inference_ms=inference_ms,
                    is_heuristic=True,
                )
        inference_ms = int((time.time() - t0) * 1000)
        logger.warning(
            "local-inference returned empty response after retry, falling back to heuristic"
        )
        return ConsultationResult(
            response="",
            model=self.model,
            endpoint=self.endpoint,
            inference_ms=inference_ms,
            is_heuristic=True,
        )

    def _call_ollama(self, prompt: str, timeout: int) -> str:
        """Call Ollama /api/generate and return response text."""
        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
            }
        ).encode()
        url = f"{self.endpoint}/api/generate"
        _validate_http_url(url)
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        return body.get("response", "")

    def _call_openai(self, prompt: str, timeout: int) -> str:
        """Call OpenAI-compatible /v1/chat/completions and return response text.

        Supports: LMStudio, llama-server, vLLM, Together.ai, OpenAI, etc.

        LMStudio-specific notes:
          - Verify: Settings > Developer > Enable API server
          - Default port: 8000 (set INVESTORCLAW_CONSULTATION_ENDPOINT=http://localhost:8000)
          - Model name: must match loaded model in LMStudio UI
          - Windows/WSL: set network binding to 0.0.0.0 (not 127.0.0.1), enable CORS
          - Windows: add firewall exception for port 8000
          - Some versions may have issues with certain parameters; uses conservative settings.
        """
        # Conservative OpenAI-compatible request (works across more implementations)
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "temperature": 0.65,
                "top_p": 0.9,
                "max_tokens": 1200,
            }
        ).encode()
        url = f"{self.endpoint}/v1/chat/completions"
        _validate_http_url(url)
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            logger.error(
                "OpenAI-compatible endpoint returned HTTP %d. "
                "If using LMStudio, verify: "
                "(1) Settings > Developer > Enable API Server is ON, "
                "(2) endpoint matches actual port (default: http://localhost:8000), "
                "(3) model name matches loaded model in UI. "
                "Windows/WSL: (4) network binding is 0.0.0.0 (not 127.0.0.1), "
                "(5) CORS enabled in Settings > Developer, (6) firewall allows port.",
                e.code,
            )
            raise
        except urllib.error.URLError:
            logger.error(
                "Could not reach OpenAI-compatible endpoint at %s. "
                "If using LMStudio: verify local server is enabled. "
                "Default: http://localhost:8000 (not 8080 or 11434). "
                "Windows/WSL: check network binding is 0.0.0.0, CORS enabled, "
                "firewall allows port 8000, and (from WSL) using correct Windows host IP.",
                url,
            )
            raise
        choices = body.get("choices", [])
        if not choices:
            logger.warning(
                "OpenAI endpoint returned no choices. Response: %s. "
                "If using LMStudio: check model is loaded in UI. "
                "Windows/WSL: verify CORS is enabled in Settings > Developer.",
                body,
            )
            return ""
        return choices[0].get("message", {}).get("content", "")


# ---------------------------------------------------------------------------
# Tier3Enricher
# ---------------------------------------------------------------------------


def _create_consultation_client():
    """Factory function: prefer LiteLLM client for automatic backend detection."""
    if _LITELLM_AVAILABLE:
        try:
            return _LiteLLMClient()
        except Exception as e:
            logger.debug(
                f"LiteLLM client initialization failed, falling back to ConsultationClient: {e}"
            )
    return ConsultationClient()


class Tier3Enricher:
    """Enriches AnalystConsensus objects with local-inference LLM synthesis."""

    def __init__(self) -> None:
        self.client = _create_consultation_client()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sentiment_from_consensus(consensus: Optional[str]) -> tuple[str, float]:
        """Map consensus string to (label, score)."""
        if not consensus:
            return "neutral", 0.0
        c = consensus.lower()
        if "strong buy" in c:
            return "positive", 0.9
        if "buy" in c:
            return "positive", 0.7
        if "hold" in c or "neutral" in c:
            return "neutral", 0.0
        if "strong sell" in c:
            return "negative", -0.9
        if "sell" in c or "underperform" in c:
            return "negative", -0.7
        return "neutral", 0.0

    @staticmethod
    def _strength_from_mean(recommendation_mean: float) -> str:
        """Map 1-5 Finnhub/Yahoo recommendation_mean to strength label."""
        if recommendation_mean is None:
            return "neutral"
        if recommendation_mean <= 1.5:
            return "strong_buy"
        if recommendation_mean <= 2.5:
            return "buy"
        if recommendation_mean <= 3.5:
            return "hold"
        if recommendation_mean <= 4.5:
            return "sell"
        return "strong_sell"

    def _build_prompt(self, symbol: str, rec: Any) -> str:
        """Build a compact analyst synthesis prompt."""
        return (
            f"You are a financial data analyst. Summarize the analyst sentiment for {symbol}. "
            f"Consensus: {getattr(rec, 'consensus_recommendation', 'N/A')}. "
            f"Analysts: {getattr(rec, 'analyst_count', 0)}. "
            f"Buy/Hold/Sell: {getattr(rec, 'buy_count', 0)}/{getattr(rec, 'hold_count', 0)}/{getattr(rec, 'sell_count', 0)}. "
            f"Mean target: ${getattr(rec, 'target_price_mean', 0) or 0:.2f}. "
            f"Current: ${getattr(rec, 'current_price', 0):.2f}. "
            "In 2 sentences: (1) key analyst view, (2) main risk. "
            "Educational only — not investment advice."
        )

    def _enrich_single(self, symbol: str, rec: Any) -> EnrichedAnalystConsensus:
        """Enrich a single analyst consensus record. Callable in parallel."""
        sentiment_label, sentiment_score = self._sentiment_from_consensus(
            getattr(rec, "consensus_recommendation", None)
        )
        strength = self._strength_from_mean(getattr(rec, "recommendation_mean", 2.5))

        synthesis = ""
        key_insights: List[str] = []
        risk_assessment = ""
        consultation_meta: Optional[dict] = None
        fp = ""
        quote_block: Optional[dict] = None

        if self.client.is_available():
            prompt = self._build_prompt(symbol, rec)
            result = self.client.consult(prompt)
            if result.response:
                synthesis = result.response.strip()
                sentences = [s.strip() for s in _SENT_RE.split(synthesis) if s.strip()]
                key_insights = sentences[:2]
                risk_assessment = sentences[-1] if len(sentences) > 1 else synthesis
                fp = _compute_fingerprint(symbol, self.client.model, synthesis)
                attribution = f"{self.client.model} via local-inference ({result.inference_ms}ms)"
                quote_block = {
                    "text": synthesis,
                    "attribution": attribution,
                    "verbatim_required": True,
                    "fingerprint": fp,
                }
                _card_fmt = os.environ.get("INVESTORCLAW_CARD_FORMAT", "both").strip().lower()

                if _card_fmt != "svg":
                    try:
                        _quote_dir = Path.home() / ".investorclaw" / "quotes"
                        _quote_dir.mkdir(parents=True, exist_ok=True)
                        _quote_file = _quote_dir / f"{symbol}.quote.json"
                        _quote_file.write_text(
                            json.dumps(
                                {
                                    "symbol": symbol,
                                    "text": synthesis,
                                    "attribution": attribution,
                                    "fingerprint": fp,
                                    "verbatim_required": True,
                                    "timestamp": datetime.now().isoformat(),
                                },
                                indent=2,
                            )
                        )
                        quote_block["quote_path"] = str(_quote_file)
                    except Exception as _qe:
                        logger.warning("Quote JSON write failed for %s: %s", symbol, _qe)

                if _card_fmt != "json":
                    _rdir = os.environ.get("INVESTOR_CLAW_REPORTS_DIR", "")
                    if _rdir:
                        try:
                            from rendering.render_consultation_card import render_card

                            card_path = str(
                                render_card(
                                    symbol,
                                    synthesis,
                                    attribution,
                                    fp,
                                    datetime.now().isoformat(),
                                    Path(_rdir) / ".raw",
                                )
                            )
                            quote_block["card_path"] = card_path
                        except Exception as _e:
                            logger.debug("Card render failed for %s: %s", symbol, _e)
                consultation_meta = result.to_dict()
        else:
            consultation_meta = {
                "model": self.client.model,
                "endpoint": self.client.endpoint,
                "inference_ms": 0,
                "is_heuristic": True,
            }

        return EnrichedAnalystConsensus(
            symbol=symbol,
            current_price=getattr(rec, "current_price", 0.0),
            analyst_count=getattr(rec, "analyst_count", 0),
            consensus=getattr(rec, "consensus_recommendation", None),
            recommendation_mean=getattr(rec, "recommendation_mean", 2.5),
            sentiment_label=sentiment_label,
            sentiment_score=sentiment_score,
            recommendation_strength=strength,
            synthesis=synthesis,
            key_insights=key_insights,
            risk_assessment=risk_assessment,
            consultation=consultation_meta,
            fingerprint=fp,
            quote=quote_block,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enrich_batch(
        self,
        recommendations: Dict[str, Any],
        limit: Optional[int] = None,
    ) -> Dict[str, EnrichedAnalystConsensus]:
        """
        Enrich a dict of AnalystConsensus objects with LLM synthesis.

        Args:
            recommendations: {symbol: AnalystConsensus}
            limit: cap number of symbols enriched (None = all)

        Returns:
            {symbol: EnrichedAnalystConsensus}
        """
        symbols = list(recommendations.keys())
        if limit is not None:
            symbols = symbols[:limit]

        enriched: Dict[str, EnrichedAnalystConsensus] = {}

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(self._enrich_single, symbol, recommendations[symbol]): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    enriched[symbol] = future.result()
                except Exception as exc:
                    logger.warning("Enrichment failed for %s: %s", symbol, exc)

        return enriched
