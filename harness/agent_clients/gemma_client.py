"""
Gemma-4 local client via Ollama.

Connects to local Ollama instance (typically CERBERUS at 192.168.207.96:11434)
No API key required (local execution)
"""

import asyncio
import logging
from typing import Any, Dict

from harness.agent_clients.provider_client import ProviderClient

logger = logging.getLogger(__name__)


class GemmaClient(ProviderClient):
    """Client for local Gemma-4 via Ollama."""

    def __init__(
        self,
        endpoint: str = "http://192.168.207.96:11434",
        model_id: str = "gemma4-consult",
        timeout_seconds: int = 60,  # Local inference slower
        debug_mode: bool = False,
    ):
        """
        Initialize Gemma client.

        Args:
            endpoint: Ollama endpoint (default CERBERUS)
            model_id: Model to use (gemma4-consult, gemma4:e4b, etc.)
            timeout_seconds: Request timeout (default 60s for CPU)
            debug_mode: If True, record full prompts
        """
        super().__init__(
            provider_name="gemma",
            model_id=model_id,
            endpoint=endpoint,
            api_key=None,  # No API key for local
            timeout_seconds=timeout_seconds,
            debug_mode=debug_mode,
        )
        self.rate_limit_rps = 1  # Local CPU, one request at a time

    async def _test_connectivity(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.endpoint}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status == 200

        except ImportError:
            # Fallback: try with curl/subprocess if aiohttp not available
            try:
                result = await asyncio.create_subprocess_exec(
                    "curl",
                    "-s",
                    "-f",
                    f"{self.endpoint}/api/tags",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(result.wait(), timeout=5)
                return result.returncode == 0
            except (asyncio.TimeoutError, FileNotFoundError):
                return False

        except Exception as e:
            logger.warning(f"Connectivity check failed: {e}")
            return False

    async def _test_credentials(self) -> bool:
        """No credentials for local Ollama."""
        return True

    async def _call_provider(self, prompt: str) -> Dict[str, Any]:
        """Call Ollama API with prompt."""
        try:
            import aiohttp

            payload = {
                "model": self.model_id,
                "prompt": prompt,
                "stream": False,
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.endpoint}/api/generate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"API returned {resp.status}")

                    result = await resp.json()

                    return {
                        "content": result.get("response", ""),
                        "tokens_prompt": result.get("prompt_eval_count"),
                        "tokens_completion": result.get("eval_count"),
                        "metadata": {
                            "model": self.model_id,
                            "endpoint": self.endpoint,
                        },
                    }

        except ImportError:
            # Fallback: use curl if aiohttp not available
            import json

            payload = json.dumps(
                {
                    "model": self.model_id,
                    "prompt": prompt,
                    "stream": False,
                }
            )

            try:
                result = await asyncio.create_subprocess_exec(
                    "curl",
                    "-s",
                    "-X",
                    "POST",
                    f"{self.endpoint}/api/generate",
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    payload,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                stdout, stderr = await asyncio.wait_for(
                    result.communicate(),
                    timeout=self.timeout_seconds,
                )

                if result.returncode != 0:
                    raise RuntimeError(f"curl failed: {stderr.decode()}")

                response_data = json.loads(stdout.decode())

                return {
                    "content": response_data.get("response", ""),
                    "tokens_prompt": response_data.get("prompt_eval_count"),
                    "tokens_completion": response_data.get("eval_count"),
                    "metadata": {
                        "model": self.model_id,
                        "endpoint": self.endpoint,
                    },
                }

            except Exception as e:
                logger.error(f"Ollama call failed: {e}")
                raise

        except Exception as e:
            logger.error(f"Ollama API error: {e}")
            raise
