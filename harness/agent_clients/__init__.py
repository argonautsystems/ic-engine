"""
Agent client implementations for dual-path harness.

Supports OpenClaw, ZeroClaw, Hermes, and other agent runtimes.
All endpoints are environment-configurable; no hardcoded IPs or hostnames.
"""

from .base import AgentClient, AgentResponse
from .hermes import HermesClient
from .openclaw import OpenClawClient
from .zeroclaw import ZeroClawClient

__all__ = [
    "AgentClient",
    "AgentResponse",
    "OpenClawClient",
    "ZeroClawClient",
    "HermesClient",
]
