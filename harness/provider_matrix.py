"""
LLM provider test matrix for InvestorClaw harness.

Defines provider configurations and constraints for:
- xAI (Grok-4.1, real-time reasoning)
- Google (Gemini-2.5-Flash, multimodal)
- Together (MiniMax-M2.7, lightweight)
- Gemma (local Ollama, CPU-only)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ProviderConfig:
    """Configuration for an LLM provider."""

    name: str  # Provider identifier
    endpoint: str  # API endpoint or local address
    model_id: str  # Model identifier
    supports_tool_use: bool  # Native function calling support
    supports_streaming: bool  # Streaming responses
    rate_limit_rps: int  # Requests per second limit
    cost_per_1k_tokens: float  # USD cost per 1000 tokens
    timeout_seconds: int  # API call timeout
    fallback_model: Optional[str]  # Fallback model if primary unavailable
    description: str  # Human-readable description


# Provider definitions
PROVIDER_MATRIX = {
    "xai": ProviderConfig(
        name="xai",
        endpoint="https://api.x.ai/v1",
        model_id="grok-4.1",
        supports_tool_use=True,
        supports_streaming=True,
        rate_limit_rps=10,
        cost_per_1k_tokens=0.005,
        timeout_seconds=30,
        fallback_model=None,
        description="xAI Grok-4.1 (real-time reasoning, external knowledge)",
    ),
    "google": ProviderConfig(
        name="google",
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model_id="gemini-2.5-flash",
        supports_tool_use=True,
        supports_streaming=True,
        rate_limit_rps=15,
        cost_per_1k_tokens=0.002,
        timeout_seconds=30,
        fallback_model="gemini-2.0-flash",
        description="Google Gemini-2.5-Flash (fast, multimodal)",
    ),
    "together": ProviderConfig(
        name="together",
        endpoint="https://api.together.xyz/v1",
        model_id="MiniMax-M2.7-8B",
        supports_tool_use=False,  # Requires prompt engineering
        supports_streaming=True,
        rate_limit_rps=20,
        cost_per_1k_tokens=0.0005,
        timeout_seconds=30,
        fallback_model=None,
        description="Together AI MiniMax-M2.7 (lightweight, cost-optimized)",
    ),
    "gemma": ProviderConfig(
        name="gemma",
        endpoint="http://192.168.207.96:11434",
        model_id="gemma4-consult",
        supports_tool_use=False,
        supports_streaming=True,
        rate_limit_rps=1,  # Local CPU, single request at a time
        cost_per_1k_tokens=0.0,  # Local
        timeout_seconds=60,  # Local inference slower
        fallback_model=None,
        description="Google Gemma-4 (local Ollama, CPU-only, private)",
    ),
}


def get_provider(provider_name: str) -> ProviderConfig:
    """Get provider configuration by name."""
    if provider_name not in PROVIDER_MATRIX:
        raise ValueError(
            f"Unknown provider: {provider_name}. Available: {', '.join(PROVIDER_MATRIX.keys())}"
        )
    return PROVIDER_MATRIX[provider_name]


def is_local_provider(provider_name: str) -> bool:
    """Check if provider is local (no API key required)."""
    provider = get_provider(provider_name)
    return provider.name == "gemma"


def requires_api_key(provider_name: str) -> bool:
    """Check if provider requires API key."""
    return not is_local_provider(provider_name)
