"""
Factory for creating provider clients.

Instantiates the appropriate LLM provider client based on provider name.
Supports: xAI, Google, Together, Gemma (local)
"""

import logging
from typing import Optional

from harness.agent_clients.provider_client import ProviderClient

logger = logging.getLogger(__name__)


def get_provider_client(
    provider_name: str,
    api_key: Optional[str] = None,
    model_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    timeout_seconds: int = 30,
    debug_mode: bool = False,
) -> ProviderClient:
    """
    Get provider client instance.

    Args:
        provider_name: Provider identifier (xai, google, together, gemma)
        api_key: API key (if required)
        model_id: Model identifier (uses provider default if not specified)
        endpoint: API endpoint (uses provider default if not specified)
        timeout_seconds: Request timeout
        debug_mode: If True, record full prompts

    Returns:
        ProviderClient instance

    Raises:
        ValueError: If provider not recognized
    """
    provider_name = provider_name.lower()

    if provider_name == "xai":
        from harness.agent_clients.xai_client import XAIClient

        return XAIClient(
            api_key=api_key,
            model_id=model_id or "grok-4.1",
            endpoint=endpoint or "https://api.x.ai/v1",
            timeout_seconds=timeout_seconds,
            debug_mode=debug_mode,
        )

    elif provider_name == "google":
        from harness.agent_clients.google_client import GoogleClient

        return GoogleClient(
            api_key=api_key,
            model_id=model_id or "gemini-2.5-flash",
            endpoint=endpoint or "https://generativelanguage.googleapis.com/v1beta",
            timeout_seconds=timeout_seconds,
            debug_mode=debug_mode,
        )

    elif provider_name == "together":
        from harness.agent_clients.together_client import TogetherClient

        return TogetherClient(
            api_key=api_key,
            model_id=model_id or "MiniMax-M2.7-8B",
            endpoint=endpoint or "https://api.together.xyz/v1",
            timeout_seconds=timeout_seconds,
            debug_mode=debug_mode,
        )

    elif provider_name == "gemma":
        from harness.agent_clients.gemma_client import GemmaClient

        return GemmaClient(
            endpoint=endpoint or "http://192.168.207.96:11434",
            model_id=model_id or "gemma4-consult",
            timeout_seconds=timeout_seconds,
            debug_mode=debug_mode,
        )

    else:
        raise ValueError(
            f"Unknown provider: {provider_name}. Available: xai, google, together, gemma"
        )


def get_available_providers() -> list:
    """Get list of available providers."""
    return ["xai", "google", "together", "gemma"]
