"""
Base agent client interface with sanitization and timeout handling.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentResponse:
    """Sanitized response from agent execution."""

    agent_name: str
    prompt: str
    response_content: str
    model_used: Optional[str] = None
    duration_ms: float = 0.0
    exit_code: int = 0
    error: Optional[str] = None
    tokens_prompt: Optional[int] = None
    tokens_completion: Optional[int] = None
    full_conversation: Optional[List[Dict]] = None
    device: Optional[str] = None
    memory_usage_mb: Optional[float] = None
    metadata: Optional[Dict] = None
    timestamp: Optional[str] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()

    def to_dict_sanitized(self) -> Dict[str, Any]:
        """Convert to dict with recording sanitization applied."""
        data = asdict(self)

        # Remove full prompts by default (requires explicit debug flag)
        data["prompt_sanitized"] = f"<{len(self.prompt)} chars>"

        # Remove full conversation unless explicitly enabled
        if self.full_conversation:
            data["conversation_sanitized"] = f"<{len(self.full_conversation)} messages>"

        return data


class AgentClient(ABC):
    """Base class for agent clients."""

    def __init__(
        self,
        agent_name: str,
        timeout_seconds: int = 60,
        debug_mode: bool = False,
    ):
        """
        Initialize agent client.

        Args:
            agent_name: Identifier for this agent (openclaw, zeroclaw, etc.)
            timeout_seconds: Request timeout (default 60s)
            debug_mode: If True, record full prompts and conversations
        """
        self.agent_name = agent_name
        self.timeout_seconds = timeout_seconds
        self.debug_mode = debug_mode

    @abstractmethod
    async def send_message(self, prompt: str) -> AgentResponse:
        """
        Send a message to the agent and get response.

        Args:
            prompt: User message/command

        Returns:
            Sanitized agent response

        Raises:
            TimeoutError: If request exceeds timeout_seconds
            ConnectionError: If agent is unreachable
        """
        pass

    def create_response(
        self,
        prompt: str,
        response_content: str,
        model_used: Optional[str] = None,
        duration_ms: float = 0.0,
        exit_code: int = 0,
        error: Optional[str] = None,
        tokens_prompt: Optional[int] = None,
        tokens_completion: Optional[int] = None,
        full_conversation: Optional[List[Dict]] = None,
        device: Optional[str] = None,
        memory_usage_mb: Optional[float] = None,
        metadata: Optional[Dict] = None,
    ) -> AgentResponse:
        """Factory method for creating sanitized responses."""
        return AgentResponse(
            agent_name=self.agent_name,
            prompt=prompt if self.debug_mode else f"<{len(prompt)} chars>",
            response_content=response_content,
            model_used=model_used,
            duration_ms=duration_ms,
            exit_code=exit_code,
            error=error,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            full_conversation=full_conversation if self.debug_mode else None,
            device=device,
            memory_usage_mb=memory_usage_mb,
            metadata=metadata,
        )
