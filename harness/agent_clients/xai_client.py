"""
xAI Grok-4.1 client.

Supports real-time reasoning and web search via xAI API.
Requires XAI_API_KEY environment variable or constructor argument.
"""

import logging
from typing import Any, Dict, Optional

from harness.agent_clients.provider_client import ProviderClient

logger = logging.getLogger(__name__)


class XAIClient(ProviderClient):
    """Client for xAI Grok-4.1."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_id: str = "grok-4.1",
        endpoint: str = "https://api.x.ai/v1",
        timeout_seconds: int = 30,
        debug_mode: bool = False,
    ):
        """
        Initialize xAI client.

        Args:
            api_key: xAI API key (env var XAI_API_KEY if not provided)
            model_id: Model to use (default grok-4.1)
            endpoint: API endpoint
            timeout_seconds: Request timeout
            debug_mode: If True, record full prompts
        """
        import os

        api_key = api_key or os.getenv("XAI_API_KEY")

        super().__init__(
            provider_name="xai",
            model_id=model_id,
            endpoint=endpoint,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            debug_mode=debug_mode,
        )
        self.rate_limit_rps = 10

    async def _test_connectivity(self) -> bool:
        """Check if xAI API is reachable."""
        try:
            import aiohttp

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.endpoint}/models",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status in (200, 401, 403)  # Any response is ok

        except Exception as e:
            logger.warning(f"xAI connectivity check failed: {e}")
            return False

    async def _test_credentials(self) -> bool:
        """Validate xAI API key."""
        if not self.api_key:
            logger.warning("xAI: API key not configured")
            return False

        try:
            import aiohttp

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.endpoint}/models",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status == 200

        except Exception as e:
            logger.warning(f"xAI credential validation failed: {e}")
            return False

    async def _call_provider(self, prompt: str) -> Dict[str, Any]:
        """Call xAI API with prompt."""
        try:
            import aiohttp

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            payload = {
                "model": self.model_id,
                "messages": [
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.endpoint}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"API returned {resp.status}: {error_text}")

                    result = await resp.json()

                    return {
                        "content": result["choices"][0]["message"]["content"],
                        "tokens_prompt": result.get("usage", {}).get("prompt_tokens"),
                        "tokens_completion": result.get("usage", {}).get("completion_tokens"),
                        "metadata": {
                            "model": self.model_id,
                            "provider": "xai",
                        },
                    }

        except ImportError:
            logger.error("aiohttp required for xAI client")
            raise

        except Exception as e:
            logger.error(f"xAI API error: {e}")
            raise
