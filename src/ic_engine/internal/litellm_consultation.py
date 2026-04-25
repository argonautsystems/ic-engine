#!/usr/bin/env python3
"""
LLM consultation client using litellm for automatic backend detection.

Replaces manual backend detection logic with litellm's automatic provider selection.
Supports: Ollama, OpenAI-compatible (LMStudio, llama-server, vLLM), OpenAI, Anthropic, etc.

This is a drop-in replacement for tier3_enrichment.ConsultationClient.
"""

import logging
import os
import time
from typing import NamedTuple

try:
    from litellm import completion

    LITELLM_AVAILABLE = True
except ImportError:
    LITELLM_AVAILABLE = False

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a financial analysis assistant helping analyze investment portfolios.
Provide concise, actionable insights focused on risk, diversification, and performance.
Always cite specific holdings or metrics when making recommendations."""


class ConsultationResult(NamedTuple):
    """Result from LLM consultation."""

    response: str
    model: str
    endpoint: str
    inference_ms: int
    is_heuristic: bool


class LiteLLMConsultationClient:
    """
    Unified LLM client using litellm for automatic backend detection.

    Automatically handles:
    - Ollama (local, 11434)
    - OpenAI-compatible (LMStudio, llama-server, vLLM, Together.ai)
    - OpenAI (API key)
    - Anthropic (API key)
    - Any provider litellm supports

    Configuration via environment:
      INVESTORCLAW_CONSULTATION_ENDPOINT  — Local inference URL (e.g., http://localhost:11434)
      INVESTORCLAW_CONSULTATION_MODEL     — Model name (e.g., gemma4-consult)
    """

    def __init__(self) -> None:
        if not LITELLM_AVAILABLE:
            raise ImportError("litellm not installed. Install with: pip install litellm")

        self.endpoint = os.environ.get(
            "INVESTORCLAW_CONSULTATION_ENDPOINT", "http://localhost:11434"
        ).rstrip("/")
        self.model = os.environ.get("INVESTORCLAW_CONSULTATION_MODEL", "gemma4-consult")
        self._available = None  # Lazy probe result

    def is_available(self) -> bool:
        """Probe endpoint — returns True if reachable."""
        if self._available is not None:
            return self._available

        try:
            # Try a very short timeout call to check availability
            result = completion(
                model=self._build_model_string(),
                messages=[{"role": "user", "content": "test"}],
                timeout=5,
            )
            self._available = bool(result)
            return self._available
        except Exception as e:
            logger.debug(f"Endpoint availability check failed: {e}")
            self._available = False
            return False

    def _build_model_string(self) -> str:
        """
        Build model string for litellm based on endpoint.

        litellm model format:
          - "openai/<model>" for OpenAI API
          - "ollama/<model>" for Ollama
          - "openai/<model>" for OpenAI-compatible (LMStudio, llama-server)
          - etc.

        We detect based on endpoint and use appropriate format.
        """
        if "ollama" in self.endpoint.lower() or "11434" in self.endpoint:
            return f"ollama/{self.model}"
        elif "lmstudio" in self.endpoint.lower() or "8000" in self.endpoint:
            # LMStudio is OpenAI-compatible; need custom_api_base
            return f"openai/{self.model}"
        elif "localhost" in self.endpoint or "127.0.0.1" in self.endpoint:
            # Local inference is likely OpenAI-compatible
            return f"openai/{self.model}"
        else:
            # Assume OpenAI-compatible for remote endpoints
            return f"openai/{self.model}"

    def consult(self, prompt: str, timeout: int = 120) -> ConsultationResult:
        """
        POST prompt to LLM endpoint and return ConsultationResult.

        Retries once on empty response (1s backoff).
        """
        t0 = time.time()

        for attempt in range(2):
            try:
                model_str = self._build_model_string()

                # Set custom API base if using local inference
                api_base = None
                if (
                    "localhost" in self.endpoint
                    or "127.0.0.1" in self.endpoint
                    or "ollama" in self.endpoint.lower()
                ):
                    api_base = self.endpoint

                response = completion(
                    model=model_str,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    api_base=api_base,
                    timeout=timeout,
                    temperature=0.65,
                    top_p=0.9,
                    max_tokens=1200,
                )

                # Extract text from response
                if isinstance(response, str):
                    text = response
                elif hasattr(response, "choices") and response.choices:
                    text = response.choices[0].message.content
                else:
                    text = ""

                inference_ms = int((time.time() - t0) * 1000)

                if text:
                    return ConsultationResult(
                        response=text,
                        model=self.model,
                        endpoint=self.endpoint,
                        inference_ms=inference_ms,
                        is_heuristic=False,
                    )

                # Empty response — retry once
                if attempt == 0:
                    logger.warning(
                        "local-inference returned empty response for %s, retrying (attempt 1/2)",
                        self.model,
                    )
                    time.sleep(1.0)

            except Exception as exc:
                inference_ms = int((time.time() - t0) * 1000)
                logger.warning(f"litellm inference failed: {exc}")
                return ConsultationResult(
                    response="",
                    model=self.model,
                    endpoint=self.endpoint,
                    inference_ms=inference_ms,
                    is_heuristic=True,
                )

        # Exhausted retries
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


# Convenience aliases for drop-in compatibility
ConsultationClient = LiteLLMConsultationClient
