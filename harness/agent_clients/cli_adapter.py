# Copyright 2026 InvestorClaw Contributors
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

"""
CLI subprocess adapter for harness agent clients.

Replaces HTTP/WebSocket/SSH plumbing with `subprocess.create_subprocess_exec`
calls. Each runtime (OpenClaw / ZeroClaw / Hermes) is now reachable via
`docker exec <container> <cli> ...` since the fleet consolidated to TYPHON
with Docker containers (2026-04-23). Claude Code stays manual via
harness/CLAUDE_CODE_TEXT_HARNESS.md per user 2026-04-25.

The dict shape returned by send_message() matches what
harness/run_cross_runtime_pilot.py already consumes (run_cross_runtime_pilot
treats this as the canonical interface).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

logger = logging.getLogger(__name__)


class CLIAgentAdapter:
    """Adapter that runs an agent CLI via subprocess and returns the
    sanitized response dict expected by the cross-runtime pilot.

    `command_template` is an argv list (NOT a shell string) — we use
    create_subprocess_exec so prompt content with quotes/special chars is
    passed safely. The placeholder `{prompt}` in any element of
    command_template is substituted with the prompt at call time. If
    `stdin_prompt=True`, the prompt is fed via stdin instead.
    """

    def __init__(
        self,
        agent_name: str,
        command_template: List[str],
        timeout_seconds: int = 120,
        stdin_prompt: bool = False,
        debug_mode: bool = False,
    ) -> None:
        self.agent_name = agent_name
        self.command_template = list(command_template)
        self.timeout_seconds = timeout_seconds
        self.stdin_prompt = stdin_prompt
        self.debug_mode = debug_mode

    def _build_argv(self, prompt: str) -> List[str]:
        """Substitute {prompt} placeholder in the argv template."""
        if self.stdin_prompt:
            return list(self.command_template)
        return [tok.replace("{prompt}", prompt) for tok in self.command_template]

    def _sanitize_argv_for_metadata(self, argv: List[str], prompt: str) -> List[str]:
        """Drop the prompt from the argv we record in metadata unless debug_mode.
        Keeps us from logging user content in production telemetry.
        """
        if self.debug_mode:
            return argv
        prompt_placeholder = f"<{len(prompt)} chars>"
        return [tok.replace(prompt, prompt_placeholder) for tok in argv]

    async def send_message(self, prompt: str) -> dict:
        """Run the configured CLI with the given prompt; return result dict.

        Returns:
            Dict with keys: agent_name, prompt, response_content, model_used,
            duration_ms, exit_code, error, tokens_prompt, tokens_completion,
            full_conversation, device, memory_usage_mb, metadata.
        """
        start = time.time()
        argv = self._build_argv(prompt)
        sanitized_argv = self._sanitize_argv_for_metadata(argv, prompt)

        prompt_repr = prompt if self.debug_mode else f"<{len(prompt)} chars>"

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if self.stdin_prompt else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdin_bytes = (prompt + "\n").encode("utf-8") if self.stdin_prompt else None

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                duration_ms = (time.time() - start) * 1000
                logger.error(
                    "%s timed out after %ss running %s",
                    self.agent_name,
                    self.timeout_seconds,
                    sanitized_argv[0:3],
                )
                return self._error_dict(
                    prompt=prompt_repr,
                    duration_ms=duration_ms,
                    exit_code=124,
                    error=f"Timeout after {self.timeout_seconds}s",
                    error_type="timeout",
                    sanitized_argv=sanitized_argv,
                )

            duration_ms = (time.time() - start) * 1000
            stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
            exit_code = proc.returncode if proc.returncode is not None else 1

            if exit_code != 0:
                logger.warning(
                    "%s exited %d running %s",
                    self.agent_name,
                    exit_code,
                    sanitized_argv[0:3],
                )

            return {
                "agent_name": self.agent_name,
                "prompt": prompt_repr,
                "response_content": stdout,
                "model_used": None,
                "duration_ms": duration_ms,
                "exit_code": exit_code,
                "error": stderr[:500] if exit_code != 0 and stderr else None,
                "tokens_prompt": None,
                "tokens_completion": None,
                "full_conversation": None,
                "device": None,
                "memory_usage_mb": None,
                "metadata": {
                    "command": sanitized_argv,
                    "stderr_truncated": stderr[:500] if stderr else None,
                },
            }

        except FileNotFoundError as exc:
            duration_ms = (time.time() - start) * 1000
            logger.error("%s CLI not found: %s", self.agent_name, exc)
            return self._error_dict(
                prompt=prompt_repr,
                duration_ms=duration_ms,
                exit_code=127,
                error=f"CLI not found: {exc}",
                error_type="cli_not_found",
                sanitized_argv=sanitized_argv,
            )

        except Exception as exc:  # noqa: BLE001 — bubble up unexpected failures as a result dict
            duration_ms = (time.time() - start) * 1000
            logger.exception("%s unexpected error", self.agent_name)
            return self._error_dict(
                prompt=prompt_repr,
                duration_ms=duration_ms,
                exit_code=1,
                error=str(exc),
                error_type="unknown",
                sanitized_argv=sanitized_argv,
            )

    def _error_dict(
        self,
        prompt: str,
        duration_ms: float,
        exit_code: int,
        error: str,
        error_type: str,
        sanitized_argv: List[str],
    ) -> dict:
        return {
            "agent_name": self.agent_name,
            "prompt": prompt,
            "response_content": "",
            "model_used": None,
            "duration_ms": duration_ms,
            "exit_code": exit_code,
            "error": error,
            "tokens_prompt": None,
            "tokens_completion": None,
            "full_conversation": None,
            "device": None,
            "memory_usage_mb": None,
            "metadata": {
                "command": sanitized_argv,
                "error_type": error_type,
            },
        }

    # Synchronous convenience wrapper (matches the legacy clients' interface)
    def sync_send_message(self, prompt: str) -> dict:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # In an async context already; caller should use send_message directly
                raise RuntimeError(
                    "sync_send_message called from a running event loop; "
                    "use `await send_message(...)` instead."
                )
        except RuntimeError:
            loop = asyncio.new_event_loop()
        return loop.run_until_complete(self.send_message(prompt))

    # Optional configurable endpoint (kept for legacy callers; ignored here
    # since CLI invocation doesn't need an endpoint URL)
    @property
    def endpoint(self) -> Optional[str]:
        return None
