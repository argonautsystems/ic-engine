"""
Provider-agnostic LLM client for multi-provider testing.

Supports: xAI (Grok), Google (Gemini), Together AI, Gemma (local Ollama)
Handles: authentication, reachability checks, fallback logic, rate limiting
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from harness.agent_clients.base import AgentClient, AgentResponse

logger = logging.getLogger(__name__)


class ProviderClient(AgentClient, ABC):
    """Base class for LLM provider clients."""

    def __init__(
        self,
        provider_name: str,
        model_id: str,
        endpoint: str,
        api_key: Optional[str] = None,
        timeout_seconds: int = 30,
        debug_mode: bool = False,
    ):
        """
        Initialize provider client.

        Args:
            provider_name: Provider identifier (xai, google, together, gemma)
            model_id: Model identifier (grok-4.1, gemini-2.5-flash, etc.)
            endpoint: API endpoint or local address
            api_key: API key (if required by provider)
            timeout_seconds: Request timeout
            debug_mode: If True, record full prompts
        """
        super().__init__(
            agent_name=provider_name,
            timeout_seconds=timeout_seconds,
            debug_mode=debug_mode,
        )
        self.provider_name = provider_name
        self.model_id = model_id
        self.endpoint = endpoint
        self.api_key = api_key
        self.rate_limit_rps = 10  # Default, overridden by subclasses
        self.rate_limiter = RateLimiter(rps=self.rate_limit_rps)

    async def check_reachability(self) -> bool:
        """Check if provider is reachable (T0 phase)."""
        try:
            await asyncio.wait_for(
                self._test_connectivity(),
                timeout=self.timeout_seconds,
            )
            logger.info(f"{self.provider_name}: Provider reachable")
            return True
        except asyncio.TimeoutError:
            logger.warning(f"{self.provider_name}: Timeout checking reachability")
            return False
        except Exception as e:
            logger.warning(f"{self.provider_name}: Unreachable - {e}")
            return False

    async def validate_credentials(self) -> bool:
        """Validate API credentials (T1 phase)."""
        if self.provider_name == "gemma":
            # Local provider, no credentials needed
            return True

        if not self.api_key:
            logger.warning(f"{self.provider_name}: API key not configured")
            return False

        try:
            result = await asyncio.wait_for(
                self._test_credentials(),
                timeout=self.timeout_seconds,
            )
            logger.info(f"{self.provider_name}: Credentials valid")
            return result
        except Exception as e:
            logger.warning(f"{self.provider_name}: Credential validation failed - {e}")
            return False

    async def send_message(self, prompt: str) -> AgentResponse:
        """
        Send message to LLM provider and get response.

        Handles:
        1. Rate limiting
        2. Reachability check
        3. Credential validation
        4. Request execution
        5. Error handling and fallback

        Returns:
            Sanitized agent response
        """
        start_time = time.time()

        # Rate limiting
        await self.rate_limiter.acquire()

        try:
            # Check reachability
            reachable = await self.check_reachability()
            if not reachable:
                duration_ms = (time.time() - start_time) * 1000
                return self.create_response(
                    prompt=prompt,
                    response_content="",
                    error=f"{self.provider_name} is unreachable",
                    duration_ms=duration_ms,
                    exit_code=1,
                    model_used=self.model_id,
                    metadata={"error_type": "provider_unreachable"},
                )

            # Check credentials (if required)
            if not await self.validate_credentials():
                duration_ms = (time.time() - start_time) * 1000
                return self.create_response(
                    prompt=prompt,
                    response_content="",
                    error=f"{self.provider_name} credentials invalid",
                    duration_ms=duration_ms,
                    exit_code=1,
                    model_used=self.model_id,
                    metadata={"error_type": "invalid_credentials"},
                )

            # Call provider API
            response = await asyncio.wait_for(
                self._call_provider(prompt),
                timeout=self.timeout_seconds,
            )

            duration_ms = (time.time() - start_time) * 1000

            return self.create_response(
                prompt=prompt,
                response_content=response.get("content", ""),
                model_used=self.model_id,
                duration_ms=duration_ms,
                exit_code=0,
                tokens_prompt=response.get("tokens_prompt"),
                tokens_completion=response.get("tokens_completion"),
                metadata=response.get("metadata"),
            )

        except asyncio.TimeoutError:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"{self.provider_name}: Timeout after {self.timeout_seconds}s")
            return self.create_response(
                prompt=prompt,
                response_content="",
                error=f"Timeout after {self.timeout_seconds}s",
                duration_ms=duration_ms,
                exit_code=124,
                model_used=self.model_id,
                metadata={"error_type": "timeout"},
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"{self.provider_name}: Error - {e}", exc_info=True)
            return self.create_response(
                prompt=prompt,
                response_content="",
                error=str(e),
                duration_ms=duration_ms,
                exit_code=1,
                model_used=self.model_id,
                metadata={"error_type": "unknown"},
            )

    @abstractmethod
    async def _test_connectivity(self) -> bool:
        """Provider-specific connectivity test."""
        pass

    @abstractmethod
    async def _test_credentials(self) -> bool:
        """Provider-specific credential validation."""
        pass

    @abstractmethod
    async def _call_provider(self, prompt: str) -> Dict[str, Any]:
        """
        Call provider API.

        Returns:
            {
                "content": "...",
                "tokens_prompt": int,
                "tokens_completion": int,
                "metadata": {...}
            }
        """
        pass


class RateLimiter:
    """Simple rate limiter for provider API calls."""

    def __init__(self, rps: int = 10):
        """
        Initialize rate limiter.

        Args:
            rps: Requests per second
        """
        self.rps = rps
        self.min_interval = 1.0 / rps if rps > 0 else 0
        self.last_request_time = 0

    async def acquire(self):
        """Wait until next request is allowed."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()
