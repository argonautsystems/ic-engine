"""
Google Gemini-2.5-Flash client.

Supports multimodal inputs via Google GenerativeAI API.
Requires GOOGLE_API_KEY environment variable or constructor argument.
"""

import logging
from typing import Any, Dict, Optional

from harness.agent_clients.provider_client import ProviderClient

logger = logging.getLogger(__name__)


class GoogleClient(ProviderClient):
    """Client for Google Gemini-2.5-Flash."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_id: str = "gemini-2.5-flash",
        endpoint: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_seconds: int = 30,
        debug_mode: bool = False,
    ):
        """
        Initialize Google client.

        Args:
            api_key: Google API key (env var GOOGLE_API_KEY if not provided)
            model_id: Model to use (default gemini-2.5-flash)
            endpoint: API endpoint
            timeout_seconds: Request timeout
            debug_mode: If True, record full prompts
        """
        import os

        api_key = api_key or os.getenv("GOOGLE_API_KEY")

        super().__init__(
            provider_name="google",
            model_id=model_id,
            endpoint=endpoint,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            debug_mode=debug_mode,
        )
        self.rate_limit_rps = 15

    async def _test_connectivity(self) -> bool:
        """Check if Google API is reachable."""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.endpoint}/models",
                    params={"key": self.api_key},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status in (200, 400, 401)  # Any response means reachable

        except Exception as e:
            logger.warning(f"Google connectivity check failed: {e}")
            return False

    async def _test_credentials(self) -> bool:
        """Validate Google API key."""
        if not self.api_key:
            logger.warning("Google: API key not configured")
            return False

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.endpoint}/models",
                    params={"key": self.api_key},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status == 200

        except Exception as e:
            logger.warning(f"Google credential validation failed: {e}")
            return False

    async def _call_provider(self, prompt: str) -> Dict[str, Any]:
        """Call Google API with prompt."""
        try:
            import aiohttp

            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt},
                        ],
                    },
                ],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 2048,
                },
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.endpoint}/models/{self.model_id}:generateContent",
                    params={"key": self.api_key},
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"API returned {resp.status}: {error_text}")

                    result = await resp.json()

                    # Extract content from Google's response format
                    content = ""
                    if "candidates" in result and result["candidates"]:
                        candidate = result["candidates"][0]
                        if "content" in candidate and "parts" in candidate["content"]:
                            parts = candidate["content"]["parts"]
                            content = "".join(p.get("text", "") for p in parts)

                    return {
                        "content": content,
                        "tokens_prompt": result.get("usageMetadata", {}).get("promptTokenCount"),
                        "tokens_completion": result.get("usageMetadata", {}).get(
                            "candidatesTokenCount"
                        ),
                        "metadata": {
                            "model": self.model_id,
                            "provider": "google",
                        },
                    }

        except ImportError:
            logger.error("aiohttp required for Google client")
            raise

        except Exception as e:
            logger.error(f"Google API error: {e}")
            raise
