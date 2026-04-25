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
OpenClaw agent client — invokes the dockerized openclaw runtime via
`docker exec <container> openclaw agent --agent <id> -m '<prompt>' --json --local`.

Refactored 2026-04-25 from the prior WebSocket-based client. The fleet
consolidated to TYPHON with `openclaw-demo-typhon` Docker container as
the canonical runtime; the older ws://localhost:18789 path was a web UI
endpoint, not the agent message endpoint we expected.

OpenClaw 2026.4.24 requires `--agent <id>`, `--session-id <id>`, or
`--to <E.164>` to disambiguate which agent session a one-shot message
targets. We use `--agent main` (the default agent name across the fleet)
unless overridden by OPENCLAW_AGENT_ID env var.

Overrides:
    OPENCLAW_CONTAINER   — container name (default: openclaw-demo-typhon)
    OPENCLAW_AGENT_ID    — agent ID to target (default: main)
"""

from __future__ import annotations

import os

try:
    from .cli_adapter import CLIAgentAdapter
except ImportError:  # pragma: no cover - exercised by pilot's sys.path bootstrap
    from cli_adapter import CLIAgentAdapter


class OpenClawClient(CLIAgentAdapter):
    """Async client for OpenClaw agent via dockerized CLI."""

    def __init__(
        self,
        endpoint: str | None = None,  # legacy kwarg, ignored (CLI doesn't need an endpoint)
        timeout_seconds: int = 120,
        debug_mode: bool = False,
    ) -> None:
        del endpoint  # back-compat: silently ignore the old WS endpoint kwarg
        container = os.getenv("OPENCLAW_CONTAINER", "openclaw-demo-typhon")
        agent_id = os.getenv("OPENCLAW_AGENT_ID", "main")
        super().__init__(
            agent_name="openclaw",
            command_template=[
                "docker",
                "exec",
                container,
                "openclaw",
                "agent",
                "--agent",
                agent_id,
                "-m",
                "{prompt}",
                "--json",
                "--local",
            ],
            timeout_seconds=timeout_seconds,
            debug_mode=debug_mode,
        )
