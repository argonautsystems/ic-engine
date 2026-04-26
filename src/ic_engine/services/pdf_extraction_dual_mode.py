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

"""Dual-mode PDF extraction: vision LLM (via clio) for high accuracy on complex
or scanned PDFs, regex fallback (via the bundled PDFExtractor) for everywhere else.

Prior to ic-engine v2.4.0 this module embedded a hardcoded Anthropic vision
client and a portfolio-specific extraction prompt. Phase 2.5 of the
InvestorClaw decomposition (per IC_DECOMPOSITION_SPEC.md) lifted the
structural primitive into clio.extract.vision; this module is now a thin
adapter that supplies the portfolio-domain prompt and aggregates clio's
parameterized result into the legacy (holdings, broker) tuple shape that
downstream callers (InvestorClaw, InvestorClaude) expect.

Mode selection:
    IC_ENGINE_VISION env var has priority. Values:
        "vision"  → force vision mode (raise if clio or API key missing)
        "regex"   → force regex mode (skip vision entirely)
        "auto"    → autodetect (default if env unset)
    In auto mode, vision is enabled if clio.extract.vision is importable AND
    at least one supported litellm provider key is set in the environment.

The legacy `INVESTORCLAW_DEPLOYMENT_MODE=claude_code` knob is still honored
for back-compat with callers that haven't migrated to IC_ENGINE_VISION yet.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Domain-specific prompt for portfolio extraction. Lives here (ic-engine,
# domain substrate), not in clio (foundation). When the prompt evolves —
# additional asset types, better disambiguation rules, broker-specific
# hints — that work happens in this file.
PORTFOLIO_VISION_PROMPT = """Analyze this financial statement page and extract:
1. Broker/platform name (e.g., Fidelity, Schwab, Vanguard)
2. All securities holdings with: symbol, quantity, price, current_value
3. Account type (brokerage, IRA, 401k, etc.)

Return ONLY valid JSON with structure:
{
  "broker": "platform name",
  "holdings": [{"symbol": "...", "quantity": N, "price": N, "value": N}],
  "account_type": "..."
}

If no holdings on this page, return empty array for holdings."""


# Default vision model. litellm-routed; any vision-capable model string works
# (claude-sonnet-4-6, openai/gpt-4o, vertex_ai/gemini-2.5-pro, etc.).
DEFAULT_VISION_MODEL = "claude-sonnet-4-6"


# Provider keys litellm picks up for vision-capable models. Used by the
# autodetect path to decide whether vision mode is achievable.
_VISION_API_KEY_ENVS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "VERTEXAI_PROJECT",
    "AZURE_API_KEY",
)


class ExtractionMode(Enum):
    """PDF extraction strategy."""

    VISION = "vision"  # clio.extract.vision via litellm
    REGEX = "regex"  # bundled PDFExtractor with rule-based strategies


def _has_vision_api_key() -> bool:
    """Return True iff at least one supported vision-LLM provider key is set."""
    return any(os.getenv(name) for name in _VISION_API_KEY_ENVS)


def _is_clio_vision_importable() -> bool:
    """Best-effort import probe; doesn't raise."""
    try:
        from clio.extract import vision  # noqa: F401

        return True
    except ImportError:
        return False


def detect_extraction_mode() -> ExtractionMode:
    """Decide which extraction mode to run for this process.

    Resolution order:
        1. IC_ENGINE_VISION env (vision | regex | auto). Takes precedence.
        2. Legacy INVESTORCLAW_DEPLOYMENT_MODE=claude_code → vision (back-compat).
        3. Auto: vision if clio is importable AND any supported provider key
           is set; regex otherwise.
    """
    explicit = os.getenv("IC_ENGINE_VISION", "").strip().lower()
    if explicit == "vision":
        return ExtractionMode.VISION
    if explicit == "regex":
        return ExtractionMode.REGEX

    if os.getenv("INVESTORCLAW_DEPLOYMENT_MODE", "").lower() == "claude_code":
        if _is_clio_vision_importable() and _has_vision_api_key():
            logger.info(
                "PDF extraction: vision mode (legacy INVESTORCLAW_DEPLOYMENT_MODE=claude_code)"
            )
            return ExtractionMode.VISION
        logger.warning(
            "INVESTORCLAW_DEPLOYMENT_MODE=claude_code set but clio or API key missing; "
            "falling back to regex"
        )
        return ExtractionMode.REGEX

    if _is_clio_vision_importable() and _has_vision_api_key():
        logger.info("PDF extraction: vision mode (auto-detected)")
        return ExtractionMode.VISION

    logger.info("PDF extraction: regex mode (auto-detected)")
    return ExtractionMode.REGEX


class DualModePDFExtractor:
    """Portfolio-aware PDF extractor that routes between vision and regex.

    Public surface (extract_holdings, detect_broker) is preserved from the
    pre-v2.4.0 dual-mode wrapper for drop-in compatibility with InvestorClaw
    and InvestorClaude adapters.
    """

    def __init__(
        self,
        model: str = DEFAULT_VISION_MODEL,
        api_key: Optional[str] = None,
        max_pages: int = 5,
    ):
        self.mode = detect_extraction_mode()
        self.model = model
        self.api_key = api_key
        self.max_pages = max_pages
        self._vision_extractor = None
        self._regex_extractor: Optional[object] = None

        if self.mode == ExtractionMode.VISION:
            try:
                from clio.extract.vision import VisionExtractor

                self._vision_extractor = VisionExtractor(
                    model=self.model, api_key=self.api_key, max_pages=self.max_pages
                )
            except Exception as e:  # litellm/PyMuPDF setup failures
                logger.warning("Could not initialize clio vision extractor: %s; falling back", e)
                self.mode = ExtractionMode.REGEX

        if self.mode == ExtractionMode.REGEX:
            try:
                from ic_engine.services.extract_pdf import PDFExtractor

                self._regex_extractor_cls = PDFExtractor
            except ImportError as e:
                logger.error("Could not import PDFExtractor; PDF extraction will fail: %s", e)
                self._regex_extractor_cls = None

    def extract_holdings(self, pdf_path: str) -> Tuple[List[Dict], str]:
        """Extract portfolio holdings from a PDF statement.

        Returns:
            Tuple of (holdings_list, broker_platform). holdings_list is a list
            of dicts with at least 'symbol' set; downstream Holding construction
            happens in the caller.

        Raises:
            ValueError: if no holdings could be extracted.
            RuntimeError: if no extraction method is available at all.
        """
        if self.mode == ExtractionMode.VISION:
            try:
                return self._extract_holdings_vision(pdf_path)
            except Exception as e:
                logger.warning("Vision extraction failed: %s; falling back to regex", e)
                self.mode = ExtractionMode.REGEX

        if self._regex_extractor_cls is None:
            raise RuntimeError("No PDF extraction method available")

        # Regex extractor requires a per-PDF construction (tracks broker detection).
        from pathlib import Path

        regex = self._regex_extractor_cls(Path(pdf_path), timeout=30)
        result = regex.extract()
        holdings = result.get("holdings", []) if isinstance(result, dict) else []
        broker = regex.detected_broker or "Unknown"
        if not holdings:
            raise ValueError(f"No holdings extracted from {pdf_path}")
        return (holdings, broker)

    def _extract_holdings_vision(self, pdf_path: str) -> Tuple[List[Dict], str]:
        """Extract via clio.extract.vision and aggregate to (holdings, broker)."""
        if self._vision_extractor is None:
            raise RuntimeError("Vision extractor not initialized")

        result = self._vision_extractor.extract(
            pdf_path, prompt=PORTFOLIO_VISION_PROMPT, schema=None
        )

        if result.error or not result.data:
            raise ValueError(f"Vision extraction returned no data: {result.error}")

        # clio aggregates per-page parses. Two shapes possible:
        #   * Single dict (one page parsed, or list-merge degenerate case)
        #   * List of dicts (multiple pages each returned a payload)
        page_payloads = result.data if isinstance(result.data, list) else [result.data]

        broker = "Unknown"
        all_holdings: List[Dict] = []
        for payload in page_payloads:
            if not isinstance(payload, dict):
                continue
            if broker == "Unknown" and payload.get("broker"):
                broker = str(payload["broker"])
            page_holdings = payload.get("holdings") or []
            if isinstance(page_holdings, list):
                all_holdings.extend(h for h in page_holdings if isinstance(h, dict))

        if not all_holdings:
            raise ValueError(f"No holdings extracted from {pdf_path} (vision returned empty)")

        return (all_holdings, broker)

    def detect_broker(self, pdf_path: str) -> str:
        """Detect broker platform from a PDF."""
        if self.mode == ExtractionMode.VISION:
            try:
                _, broker = self._extract_holdings_vision(pdf_path)
                return broker
            except Exception as e:
                logger.warning("Vision broker detection failed: %s; falling back", e)
                self.mode = ExtractionMode.REGEX

        if self._regex_extractor_cls is None:
            raise RuntimeError("No PDF extraction method available")

        from pathlib import Path

        regex = self._regex_extractor_cls(Path(pdf_path), timeout=30)
        return regex.detected_broker or "Unknown"


def extract_holdings(pdf_path: str) -> Tuple[List[Dict], str]:
    """Convenience wrapper: instantiate a default-config extractor and extract."""
    extractor = DualModePDFExtractor()
    return extractor.extract_holdings(pdf_path)


def detect_broker(pdf_path: str) -> str:
    """Convenience wrapper: instantiate a default-config extractor and detect."""
    extractor = DualModePDFExtractor()
    return extractor.detect_broker(pdf_path)
