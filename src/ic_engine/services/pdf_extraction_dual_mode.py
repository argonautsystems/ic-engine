#!/usr/bin/env python3
"""
Dual-mode PDF extraction: vision API for Claude Code, regex fallback for other platforms.

Automatically detects deployment mode and uses the best available extraction method:
1. Claude vision API (Claude Code only) - highest accuracy for complex/scanned PDFs
2. Regex-based extraction (all platforms) - reliable fallback using existing patterns

This module wraps the existing extract_pdf.py and provides intelligent mode selection.
"""

import logging
import os
from enum import Enum
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


class ExtractionMode(Enum):
    """PDF extraction strategy."""

    CLAUDE_VISION = "claude_vision"  # Claude Code with vision API
    REGEX = "regex"  # Universal regex-based fallback


def detect_extraction_mode() -> ExtractionMode:
    """
    Detect optimal PDF extraction mode based on deployment context.

    Returns CLAUDE_VISION if:
    - Running in Claude Code environment (INVESTORCLAW_DEPLOYMENT_MODE=claude_code)
    - Claude SDK is available (anthropic package installed)
    - API key available (ANTHROPIC_API_KEY)

    Falls back to REGEX for OpenClaw, Hermes, ZeroClaw, or standalone.
    """
    # Check explicit deployment mode env var
    deployment_mode = os.getenv("INVESTORCLAW_DEPLOYMENT_MODE", "").lower()

    if deployment_mode == "claude_code":
        try:
            import anthropic  # noqa: F401

            api_key = os.getenv("ANTHROPIC_API_KEY")
            if api_key:
                logger.info("PDF extraction: using Claude vision API (Claude Code mode)")
                return ExtractionMode.CLAUDE_VISION
        except ImportError:
            logger.debug("Claude SDK not available; falling back to regex extraction")

    # Default fallback
    logger.info("PDF extraction: using regex-based extraction (fallback mode)")
    return ExtractionMode.REGEX


class DualModePDFExtractor:
    """
    Intelligent PDF extractor that chooses extraction strategy based on environment.

    Public methods mirror existing extract_pdf.PDFExtractor for drop-in compatibility.
    """

    def __init__(self):
        self.mode = detect_extraction_mode()
        self._vision_client = None
        self._regex_extractor = None

        if self.mode == ExtractionMode.CLAUDE_VISION:
            try:
                import anthropic

                self._vision_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            except Exception as e:
                logger.warning(
                    f"Could not initialize Claude vision client: {e}. Falling back to regex."
                )
                self.mode = ExtractionMode.REGEX

        # Always have regex extractor as fallback
        if self.mode == ExtractionMode.REGEX:
            try:
                from services.extract_pdf import PDFExtractor

                self._regex_extractor = PDFExtractor()
            except ImportError:
                logger.error("Could not import PDFExtractor; PDF extraction will fail")

    def extract_holdings(self, pdf_path: str) -> Tuple[List[Dict], str]:
        """
        Extract portfolio holdings from PDF statement.

        Returns: (holdings_list, broker_platform) tuple
        Raises: ValueError if extraction fails
        """
        if self.mode == ExtractionMode.CLAUDE_VISION:
            try:
                return self._extract_holdings_vision(pdf_path)
            except Exception as e:
                logger.warning(f"Vision extraction failed: {e}. Falling back to regex.")
                self.mode = ExtractionMode.REGEX

        # Regex fallback
        if self._regex_extractor is None:
            raise RuntimeError("No PDF extraction method available")

        return self._regex_extractor.extract_holdings(pdf_path)

    def _extract_holdings_vision(self, pdf_path: str) -> Tuple[List[Dict], str]:
        """
        Extract holdings using Claude vision API.

        Sends PDF pages to Claude for analysis with a specialized prompt,
        returns structured portfolio data and detected broker platform.
        """
        import base64
        import json

        # Read PDF and convert to base64 for API
        try:
            import fitz  # PyMuPDF (already in requirements as pdfplumber dep)

            doc = fitz.open(pdf_path)
        except ImportError:
            logger.warning("PyMuPDF not available; cannot use vision extraction")
            raise

        # Send first 5 pages to Claude for analysis
        vision_results = []
        for page_num in range(min(5, len(doc))):
            pix = doc[page_num].get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x resolution
            img_data = base64.standard_b64encode(pix.tobytes("png")).decode()

            response = self._vision_client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=2048,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": img_data,
                                },
                            },
                            {
                                "type": "text",
                                "text": """Analyze this financial statement page and extract:
1. Broker/platform name (e.g., Fidelity, Schwab, Vanguard)
2. All securities holdings with: symbol, quantity, price, current_value
3. Account type (brokerage, IRA, 401k, etc.)

Return ONLY valid JSON with structure:
{
  "broker": "platform name",
  "holdings": [{"symbol": "...", "quantity": N, "price": N, "value": N}],
  "account_type": "..."
}

If no holdings on this page, return empty array for holdings.""",
                            },
                        ],
                    }
                ],
            )

            try:
                text = response.content[0].text
                # Extract JSON from response (Claude may wrap it in markdown)
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0]
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0]

                data = json.loads(text)
                if data.get("holdings"):
                    vision_results.append(data)
            except (json.JSONDecodeError, IndexError) as e:
                logger.debug(f"Could not parse Claude response for page {page_num}: {e}")
                continue

        doc.close()

        # Consolidate results from all pages
        if not vision_results:
            raise ValueError("No holdings extracted from PDF")

        broker = vision_results[0].get("broker", "Unknown")
        all_holdings = []

        for result in vision_results:
            all_holdings.extend(result.get("holdings", []))

        return (all_holdings, broker)

    def detect_broker(self, pdf_path: str) -> str:
        """Detect broker platform from PDF."""
        if self.mode == ExtractionMode.CLAUDE_VISION:
            try:
                _, broker = self._extract_holdings_vision(pdf_path)
                return broker
            except Exception as e:
                logger.warning(f"Vision broker detection failed: {e}. Falling back to regex.")
                self.mode = ExtractionMode.REGEX

        if self._regex_extractor is None:
            raise RuntimeError("No PDF extraction method available")

        return self._regex_extractor.detect_broker(pdf_path)


# Convenience function for drop-in replacement
def extract_holdings(pdf_path: str) -> Tuple[List[Dict], str]:
    """Extract holdings from PDF using appropriate method for deployment."""
    extractor = DualModePDFExtractor()
    return extractor.extract_holdings(pdf_path)


def detect_broker(pdf_path: str) -> str:
    """Detect broker platform using appropriate method for deployment."""
    extractor = DualModePDFExtractor()
    return extractor.detect_broker(pdf_path)
