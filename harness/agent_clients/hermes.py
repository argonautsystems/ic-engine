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
Hermes agent client — invokes the dockerized hermes runtime via
`docker exec <container> /opt/hermes/.venv/bin/hermes chat -q '<prompt>'`.

Refactored 2026-04-25 from the prior HTTP daemon client. Hermes is an
interactive CLI (per global CLAUDE.md gotcha "Hermes is not a daemon");
the older HTTP-on-localhost:8888 path was hitting Jupyter Server. The
canonical runtime is now `hermes-demo-typhon` Docker container.

Hermes provides `hermes chat -q QUERY` for non-interactive single
queries — equivalent to `claude -p`.

Overrides:
    HERMES_CONTAINER          — container name (default: hermes-demo-typhon)
    HERMES_BIN_IN_CONTAINER   — full path to hermes binary inside container
                                (default: /opt/hermes/.venv/bin/hermes)
"""

from __future__ import annotations

import os

try:
    from .cli_adapter import CLIAgentAdapter
except ImportError:  # pragma: no cover - exercised by pilot's sys.path bootstrap
    from cli_adapter import CLIAgentAdapter


class HermesClient(CLIAgentAdapter):
    """Async client for Hermes agent via dockerized CLI."""

    def __init__(
        self,
        endpoint: str | None = None,  # legacy kwarg, ignored (no HTTP path anymore)
        timeout_seconds: int = 120,
        debug_mode: bool = False,
    ) -> None:
        del endpoint  # back-compat: silently ignore the old HTTP endpoint kwarg
        container = os.getenv("HERMES_CONTAINER", "hermes-demo-typhon")
        hermes_bin = os.getenv("HERMES_BIN_IN_CONTAINER", "/opt/hermes/.venv/bin/hermes")
        super().__init__(
            agent_name="hermes",
            command_template=[
                "docker",
                "exec",
                container,
                hermes_bin,
                "chat",
                "-q",
                "{prompt}",
            ],
            timeout_seconds=timeout_seconds,
            debug_mode=debug_mode,
        )
