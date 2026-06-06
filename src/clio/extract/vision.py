# Copyright 2026 clio Contributors
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

"""clio.extract.vision — PDF/image to structured JSON via vision LLM.

A foundation primitive: rasterize a document, send pages to a vision-capable
LLM with a caller-supplied prompt, parse JSON out of the response, optionally
validate against a caller-supplied pydantic schema, return a structured result
envelope with confidence.

Domain knowledge stays in the caller:
    * The prompt is a parameter — clio doesn't know whether the document is a
      broker statement, a license filing, a deed of sale, or a menu.
    * The output schema is a parameter — caller passes a pydantic model
      describing the shape they expect; clio validates and returns.
    * Provider routing goes through litellm — caller passes a model string
      ("claude-sonnet-4-6", "openai/gpt-4o", "vertex_ai/gemini-2.5-pro", ...)
      and clio doesn't care which vendor backs it.

This module replaces the structural pattern from
ic-engine/src/ic_engine/services/pdf_extraction_dual_mode.py without lifting
its hardcoded portfolio prompt, hardcoded Anthropic-only client, or
hardcoded INVESTORCLAW_DEPLOYMENT_MODE env-var detection. The portfolio-
specific extraction strategies (Schwab format, UBS bonds, IRA account
sections) intentionally stay in ic-engine; they are domain heuristics, not
foundation primitives.

Usage:
    from clio.extract.vision import extract

    result = extract(
        pdf_path="/path/to/statement.pdf",
        prompt=\"\"\"Extract all securities holdings. Return JSON of shape:
            {"holdings": [{"symbol": str, "shares": float, "price": float}]}
        \"\"\",
        model="claude-sonnet-4-6",
        max_pages=5,
    )
    print(result.data["holdings"])
    print(result.confidence.value, result.confidence.passed)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Type, Union

from clio.extract.confidence import ConfidenceScore  # noqa: F401  (Protocol used for typing)

logger = logging.getLogger(__name__)


# Default vision model. Callers should pass an explicit model in production;
# this default exists so smoke tests and examples have a sane fallback.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Default image rasterization scale. fitz.Matrix(2, 2) produces ~2x DPI of the
# embedded PDF. Higher values = better OCR accuracy but more tokens.
DEFAULT_RASTER_SCALE = 2

# Default page cap. Most extraction workflows want the first few pages of a
# multi-page document; full document is rarely needed for vision extraction.
DEFAULT_MAX_PAGES = 5

# Default vision-LLM token budget per page. Calibrated for structured JSON
# replies that fit a few-thousand-token schema.
DEFAULT_MAX_TOKENS_PER_PAGE = 2048


@dataclass(frozen=True)
class VisionConfidence:
    """Structural confidence score for vision extraction.

    Conforms to the clio.extract.confidence.ConfidenceScore Protocol —
    runtime-checkable membership verified by isinstance() and the test
    suite. Frozen for immutability parity with CosineConfidence and
    EnsembleConfidence.

    Method: structural — derives confidence from how many pages returned
    parseable JSON, whether the parsed payload looks non-empty, and whether
    schema validation succeeded if a schema was provided.
    """

    value: float
    method: str = "structural"
    threshold: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.value >= self.threshold


@dataclass
class ExtractionResult:
    """Envelope returned by vision extraction.

    Composes with caller-side result envelopes. For example, an ic-engine
    adapter may carry an ic_result with a clio_fingerprint_id pointing to a
    clio.track.audit.AuditEnvelope row that references this extraction.
    """

    data: Any
    pages_processed: int
    model_used: str
    confidence: ConfidenceScore  # any Protocol-conforming score; defaults to VisionConfidence
    raw_responses: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.confidence.passed


class VisionExtractionError(Exception):
    """Raised when vision extraction fails before producing a result envelope.

    Most failures (parse errors, partial-page failures, schema-validation
    failures) are absorbed into ExtractionResult.error / confidence rather
    than raised. This exception is reserved for setup-time failures (missing
    PyMuPDF, missing litellm, malformed PDF that can't be opened at all).
    """


def _rasterize_pages(pdf_path: Path, max_pages: int, scale: int) -> list[bytes]:
    """Render PDF pages to PNG byte arrays via PyMuPDF.

    Returns a list of PNG bytes, one per page, capped at max_pages.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise VisionExtractionError(
            "PyMuPDF (fitz) is required for vision extraction. Install with: pip install pymupdf"
        ) from e

    images: list[bytes] = []
    matrix = fitz.Matrix(scale, scale)
    with fitz.open(str(pdf_path)) as doc:
        page_count = min(max_pages, len(doc))
        for page_num in range(page_count):
            pix = doc[page_num].get_pixmap(matrix=matrix)
            images.append(pix.tobytes("png"))
    return images


def _build_messages(prompt: str, image_b64: str) -> list[dict]:
    """Build a litellm-compatible message list for one page."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                },
            ],
        }
    ]


def _parse_json_from_response(text: str) -> Optional[dict]:
    """Extract JSON from a model response, tolerating markdown fences."""
    if "```json" in text:
        match = re.search(r"```json\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    if "```" in text:
        match = re.search(r"```\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def _validate_schema(data: Any, schema: Optional[Type]) -> tuple[Any, Optional[str]]:
    """Validate parsed JSON against a pydantic schema if provided.

    Returns (validated_data_or_original, error_message_or_None).
    """
    if schema is None:
        return data, None
    try:
        from pydantic import BaseModel, ValidationError
    except ImportError:
        return data, "pydantic not installed; schema validation skipped"

    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        return data, f"schema is not a pydantic BaseModel subclass: {schema!r}"

    try:
        validated = schema.model_validate(data)
        return validated, None
    except ValidationError as e:
        return data, f"schema validation failed: {e}"


class VisionExtractor:
    """Reusable vision extractor with provider-agnostic LLM routing via litellm.

    Use this when extracting from many PDFs in a session — it caches the
    model + api_key configuration so each call is a thin invocation.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        raster_scale: int = DEFAULT_RASTER_SCALE,
        max_tokens_per_page: int = DEFAULT_MAX_TOKENS_PER_PAGE,
    ):
        self.model = model
        self.api_key = api_key
        self.max_pages = max_pages
        self.raster_scale = raster_scale
        self.max_tokens_per_page = max_tokens_per_page

        try:
            import litellm  # noqa: F401
        except ImportError as e:
            raise VisionExtractionError(
                "litellm is required for vision extraction. Install with: pip install litellm"
            ) from e

    def extract(
        self,
        pdf_path: Union[str, Path],
        prompt: str,
        schema: Optional[Type] = None,
        max_pages: Optional[int] = None,
    ) -> ExtractionResult:
        """Run vision extraction on a PDF.

        Args:
            pdf_path: Path to the PDF.
            prompt: Caller-supplied extraction prompt. Must instruct the model
                to return JSON; clio does not edit the prompt or wrap it.
            schema: Optional pydantic BaseModel class for output validation.
            max_pages: Per-call override of the instance default.
        """
        import litellm

        path = Path(pdf_path)
        if not path.exists():
            raise VisionExtractionError(f"PDF not found: {path}")

        cap = max_pages if max_pages is not None else self.max_pages
        images = _rasterize_pages(path, max_pages=cap, scale=self.raster_scale)

        if not images:
            return ExtractionResult(
                data=None,
                pages_processed=0,
                model_used=self.model,
                confidence=VisionConfidence(value=0.0, metadata={"reason": "pdf_has_no_pages"}),
                error="PDF rasterization returned zero pages",
            )

        page_payloads: list[dict] = []
        raw_responses: list[str] = []
        api_kwargs: dict[str, Any] = {}
        if self.api_key:
            api_kwargs["api_key"] = self.api_key

        for page_idx, png_bytes in enumerate(images):
            img_b64 = base64.standard_b64encode(png_bytes).decode("ascii")
            messages = _build_messages(prompt, img_b64)
            try:
                response = litellm.completion(
                    model=self.model,
                    messages=messages,
                    max_tokens=self.max_tokens_per_page,
                    **api_kwargs,
                )
                text = response.choices[0].message.content or ""
            except Exception as e:  # litellm exposes many provider exceptions
                logger.warning("Vision call failed on page %d: %s", page_idx, e)
                raw_responses.append("")
                continue

            raw_responses.append(text)
            parsed = _parse_json_from_response(text)
            if parsed is not None:
                page_payloads.append(parsed)

        # Aggregate the per-page parses. Caller-side schema decides shape;
        # clio defaults to a list-merge if pages return list payloads, or
        # picks the first non-empty dict otherwise.
        aggregated: Any
        if not page_payloads:
            aggregated = None
        elif all(isinstance(p, list) for p in page_payloads):
            aggregated = [item for page in page_payloads for item in page]
        elif all(isinstance(p, dict) for p in page_payloads):
            aggregated = page_payloads[0] if len(page_payloads) == 1 else page_payloads
        else:
            aggregated = page_payloads

        validated, schema_err = _validate_schema(aggregated, schema)

        # Structural confidence: parseable-page ratio, dampened by schema
        # validation failure.
        parse_ratio = len(page_payloads) / len(images) if images else 0.0
        confidence_value = parse_ratio
        if schema_err:
            confidence_value *= 0.5

        confidence = VisionConfidence(
            value=round(confidence_value, 3),
            metadata={
                "pages_total": len(images),
                "pages_parsed": len(page_payloads),
                "schema_validated": schema is not None and schema_err is None,
                "schema_error": schema_err,
            },
        )

        return ExtractionResult(
            data=validated if aggregated is not None else None,
            pages_processed=len(images),
            model_used=self.model,
            confidence=confidence,
            raw_responses=raw_responses,
            error=None if aggregated is not None else "no pages returned parseable JSON",
        )


def extract(
    pdf_path: Union[str, Path],
    prompt: str,
    *,
    schema: Optional[Type] = None,
    model: str = DEFAULT_MODEL,
    max_pages: int = DEFAULT_MAX_PAGES,
    api_key: Optional[str] = None,
) -> ExtractionResult:
    """Functional convenience wrapper around VisionExtractor.

    Use this for one-shot extractions; for repeated calls in a session,
    instantiate VisionExtractor once and reuse it.
    """
    api_key = api_key or os.getenv("CLIO_VISION_API_KEY")
    extractor = VisionExtractor(model=model, api_key=api_key, max_pages=max_pages)
    return extractor.extract(pdf_path, prompt=prompt, schema=schema)
