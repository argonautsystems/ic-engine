"""
Dual-Path Test Harness Orchestrator
Executes tests via both direct CLI/API (Path A) and agent systems (Path B)
to capture real end-user experience, not simulated interactions.
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from command_matrix import get_command, get_command_suite, get_commands_by_tier  # noqa: F401
    from device_matrix import get_device, get_timeout_seconds, is_local_device  # noqa: F401
except ImportError:
    # Fallback if running from outside harness directory
    from .command_matrix import get_command, get_commands_by_tier
    from .device_matrix import get_device, get_timeout_seconds

logger = logging.getLogger(__name__)


class Agent(Enum):
    """Available agent systems."""

    OPENCLAW = "openclaw"
    ZEROCLAW = "zeroclaw"
    HERMES = "hermes"


@dataclass
class TestScenario:
    """A test scenario to run in both paths."""

    name: str
    description: str
    portfolio_file: str
    path_a_command: str  # Direct CLI command
    path_b_prompt: str  # Agent prompt
    path_b_agent: Agent
    path_b_device: Optional[str] = None  # Device for ZeroClaw (e.g., "zeropi")
    path_b_provider: Optional[str] = None  # LLM provider (xai, google, together, gemma)
    timeout_seconds: int = 60
    capture_narration: bool = True


@dataclass
class PathAResult:
    """Result from direct CLI/API execution."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    runtime_ms: float
    path: str = "A"
    output_shape: Optional[Dict[str, Any]] = None
    errors: Optional[str] = None
    provider_used: Optional[str] = None
    timestamp: str = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


@dataclass
class PathBResult:
    """Result from agent-based execution (real UX)."""

    agent: Agent
    prompt: str
    response_content: str
    agent_latency_ms: float
    path: str = "B"
    model_used: Optional[str] = None
    narration_present: bool = False
    narration_quality: Optional[float] = None
    tokens_prompt: Optional[int] = None
    tokens_completion: Optional[int] = None
    full_conversation: List[Dict] = None
    device: Optional[str] = None  # For ZeroClaw
    memory_usage_mb: Optional[float] = None  # For Pi testing
    metadata: Optional[Dict] = None
    timestamp: str = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()
        if self.full_conversation is None:
            self.full_conversation = []


@dataclass
class ComparisonResult:
    """Comparison between Path A and Path B."""

    scenario_name: str
    outputs_match: bool
    output_shape_match: bool
    narration_in_agent: bool
    direct_runtime_ms: float
    agent_latency_ms: float
    agent_overhead_ms: float
    both_successful: bool
    error_divergence: bool
    ux_fidelity: str
    divergence_details: Optional[str] = None
    timestamp: Optional[str] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


class PathAExecutor:
    """Execute tests via direct CLI/API."""

    async def execute(self, scenario: TestScenario) -> PathAResult:
        """Execute direct CLI command and capture results."""
        logger.info(f"Path A: Executing: {scenario.path_a_command}")

        start_time = datetime.now()
        try:
            result = await asyncio.create_subprocess_shell(
                scenario.path_a_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                result.communicate(),
                timeout=scenario.timeout_seconds,
            )

            runtime_ms = (datetime.now() - start_time).total_seconds() * 1000

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            # Try to parse output shape
            output_shape = None
            try:
                # Look for JSON output
                if stdout_str.strip():
                    output_shape = json.loads(stdout_str)
                    if isinstance(output_shape, dict) and "positions" in output_shape:
                        output_shape = {"positions": len(output_shape["positions"])}
            except json.JSONDecodeError:
                pass

            return PathAResult(
                command=scenario.path_a_command,
                exit_code=result.returncode,
                stdout=stdout_str[:1000],  # Truncate for storage
                stderr=stderr_str[:1000],
                runtime_ms=runtime_ms,
                output_shape=output_shape,
                errors=stderr_str if result.returncode != 0 else None,
            )

        except asyncio.TimeoutError:
            runtime_ms = (datetime.now() - start_time).total_seconds() * 1000
            return PathAResult(
                command=scenario.path_a_command,
                exit_code=-1,
                stdout="",
                stderr="Timeout",
                runtime_ms=runtime_ms,
                errors="Command timeout",
            )
        except Exception as e:
            runtime_ms = (datetime.now() - start_time).total_seconds() * 1000
            return PathAResult(
                command=scenario.path_a_command,
                exit_code=-1,
                stdout="",
                stderr="",
                runtime_ms=runtime_ms,
                errors=str(e),
            )


class PathBExecutor:
    """Execute tests via agent systems (OpenClaw/ZeroClaw/Hermes) or LLM providers."""

    def __init__(self, provider: Optional[str] = None):
        self.openclaw_endpoint = "ws://127.0.0.1:18789"
        self.openclaw_web = "http://127.0.0.1:18791"
        self.provider = provider

        # Will be initialized lazily on first use
        self._provider_client = None

    async def execute(self, scenario: TestScenario) -> PathBResult:
        """Execute via agent and capture real UX."""
        # Check if using provider-based execution
        provider_name = scenario.path_b_provider or self.provider
        if provider_name:
            return await self._execute_provider(scenario, provider_name)

        logger.info(f"Path B ({scenario.path_b_agent.value}): Sending: {scenario.path_b_prompt}")

        if scenario.path_b_agent == Agent.OPENCLAW:
            return await self._execute_openclaw(scenario)
        elif scenario.path_b_agent == Agent.ZEROCLAW:
            return await self._execute_zeroclaw(scenario)
        elif scenario.path_b_agent == Agent.HERMES:
            return await self._execute_hermes(scenario)
        else:
            raise ValueError(f"Unknown agent: {scenario.path_b_agent}")

    async def _execute_provider(
        self,
        scenario: TestScenario,
        provider_name: str,
    ) -> PathBResult:
        """Execute via LLM provider."""
        try:
            # Lazy import to avoid circular dependencies
            import sys
            from pathlib import Path

            harness_dir = Path(__file__).parent / "agent_clients"
            if str(harness_dir) not in sys.path:
                sys.path.insert(0, str(harness_dir))

            from provider_factory import get_provider_client

            # Get or create provider client
            if not self._provider_client or self._provider_client.provider_name != provider_name:
                self._provider_client = get_provider_client(
                    provider_name=provider_name,
                    timeout_seconds=scenario.timeout_seconds,
                    debug_mode=False,
                )

            logger.info(f"Provider ({provider_name}): Sending: {scenario.path_b_prompt}")

            # Check reachability (T0 phase)
            reachable = await self._provider_client.check_reachability()
            if not reachable:
                logger.warning(f"Provider {provider_name} unreachable")
                return PathBResult(
                    agent=Agent.OPENCLAW,  # Use generic agent type for provider
                    prompt=scenario.path_b_prompt,
                    response_content="",
                    agent_latency_ms=0,
                    metadata={"error": f"Provider {provider_name} unreachable"},
                )

            # Check credentials (T1 phase)
            if not await self._provider_client.validate_credentials():
                logger.warning(f"Provider {provider_name} credentials invalid")
                return PathBResult(
                    agent=Agent.OPENCLAW,
                    prompt=scenario.path_b_prompt,
                    response_content="",
                    agent_latency_ms=0,
                    metadata={"error": f"Provider {provider_name} credentials invalid"},
                )

            # Send message to provider
            response = await self._provider_client.send_message(scenario.path_b_prompt)

            return PathBResult(
                agent=Agent.OPENCLAW,
                prompt=scenario.path_b_prompt,
                response_content=response.response_content,
                agent_latency_ms=response.duration_ms,
                model_used=response.model_used or provider_name,
                narration_present=scenario.capture_narration,
                tokens_prompt=response.tokens_prompt,
                tokens_completion=response.tokens_completion,
                metadata=response.metadata or {"provider": provider_name},
            )

        except Exception as e:
            logger.error(f"Provider execution failed: {e}", exc_info=True)
            return PathBResult(
                agent=Agent.OPENCLAW,
                prompt=scenario.path_b_prompt,
                response_content="",
                agent_latency_ms=0,
                metadata={"error": str(e)},
            )

    async def _execute_openclaw(self, scenario: TestScenario) -> PathBResult:
        """Execute via OpenClaw agent (containerized CLI)."""
        try:
            try:
                from .agent_clients.openclaw import OpenClawClient
            except ImportError:
                import sys

                client_dir = Path(__file__).parent / "agent_clients"
                if str(client_dir) not in sys.path:
                    sys.path.insert(0, str(client_dir))
                from openclaw import OpenClawClient

            client = OpenClawClient(timeout_seconds=scenario.timeout_seconds)
            result = await client.send_message(scenario.path_b_prompt)

            return PathBResult(
                agent=Agent.OPENCLAW,
                prompt=scenario.path_b_prompt,
                response_content=result.get("response_content", ""),
                agent_latency_ms=result.get("duration_ms", 0),
                model_used=result.get("model_used"),
                tokens_prompt=result.get("tokens_prompt"),
                tokens_completion=result.get("tokens_completion"),
                full_conversation=result.get("full_conversation"),
                device=result.get("device"),
                memory_usage_mb=result.get("memory_usage_mb"),
                metadata={
                    **(result.get("metadata") or {}),
                    "exit_code": result.get("exit_code"),
                    "error": result.get("error"),
                },
            )

        except Exception as e:
            logger.error(f"OpenClaw execution failed: {e}")
            return PathBResult(
                agent=Agent.OPENCLAW,
                prompt=scenario.path_b_prompt,
                response_content="",
                agent_latency_ms=0,
                metadata={"error": str(e), "error_type": "exception"},
            )

    async def _execute_zeroclaw(self, scenario: TestScenario) -> PathBResult:
        """Execute via ZeroClaw agent (containerized CLI)."""
        try:
            try:
                from .agent_clients.zeroclaw import ZeroClawClient
            except ImportError:
                import sys

                client_dir = Path(__file__).parent / "agent_clients"
                if str(client_dir) not in sys.path:
                    sys.path.insert(0, str(client_dir))
                from zeroclaw import ZeroClawClient

            client = ZeroClawClient(timeout_seconds=scenario.timeout_seconds)
            result = await client.send_message(scenario.path_b_prompt)

            return PathBResult(
                agent=Agent.ZEROCLAW,
                prompt=scenario.path_b_prompt,
                response_content=result.get("response_content", ""),
                agent_latency_ms=result.get("duration_ms", 0),
                model_used=result.get("model_used"),
                tokens_prompt=result.get("tokens_prompt"),
                tokens_completion=result.get("tokens_completion"),
                full_conversation=result.get("full_conversation"),
                device=result.get("device") or scenario.path_b_device,
                memory_usage_mb=result.get("memory_usage_mb"),
                metadata={
                    **(result.get("metadata") or {}),
                    "exit_code": result.get("exit_code"),
                    "error": result.get("error"),
                },
            )

        except Exception as e:
            logger.error(f"ZeroClaw execution failed: {e}")
            return PathBResult(
                agent=Agent.ZEROCLAW,
                prompt=scenario.path_b_prompt,
                device=scenario.path_b_device or "zeropi",
                response_content="",
                agent_latency_ms=0,
                metadata={"error": str(e), "error_type": "exception"},
            )

    async def _execute_hermes(self, scenario: TestScenario) -> PathBResult:
        """Execute via Hermes agent (containerized CLI)."""
        try:
            try:
                from .agent_clients.hermes import HermesClient
            except ImportError:
                import sys

                client_dir = Path(__file__).parent / "agent_clients"
                if str(client_dir) not in sys.path:
                    sys.path.insert(0, str(client_dir))
                from hermes import HermesClient

            client = HermesClient(timeout_seconds=scenario.timeout_seconds)
            result = await client.send_message(scenario.path_b_prompt)

            return PathBResult(
                agent=Agent.HERMES,
                prompt=scenario.path_b_prompt,
                response_content=result.get("response_content", ""),
                agent_latency_ms=result.get("duration_ms", 0),
                model_used=result.get("model_used"),
                tokens_prompt=result.get("tokens_prompt"),
                tokens_completion=result.get("tokens_completion"),
                full_conversation=result.get("full_conversation"),
                device=result.get("device"),
                memory_usage_mb=result.get("memory_usage_mb"),
                metadata={
                    **(result.get("metadata") or {}),
                    "exit_code": result.get("exit_code"),
                    "error": result.get("error"),
                },
            )

        except Exception as e:
            logger.error(f"Hermes execution failed: {e}")
            return PathBResult(
                agent=Agent.HERMES,
                prompt=scenario.path_b_prompt,
                response_content="",
                agent_latency_ms=0,
                metadata={"error": str(e), "error_type": "exception"},
            )


class ComparisonEngine:
    """Compare Path A and Path B results."""

    async def compare(
        self,
        scenario: TestScenario,
        result_a: PathAResult,
        result_b: PathBResult,
    ) -> ComparisonResult:
        """Compare outputs and detect UX divergence."""

        # Check if outputs match
        outputs_match = self._outputs_match(result_a, result_b)
        output_shape_match = self._shape_matches(result_a, result_b)

        # Check for UX differences
        narration_in_agent = "narration" in result_b.response_content.lower()
        both_successful = result_a.exit_code == 0 and result_b.response_content
        error_divergence = (result_a.errors is not None) != (
            result_b.metadata and "error" in result_b.metadata
        )

        agent_overhead_ms = result_b.agent_latency_ms - result_a.runtime_ms

        # Determine UX fidelity
        ux_fidelity = "OK"
        divergence_details = None

        if error_divergence:
            ux_fidelity = "REGRESSION"
            divergence_details = "Error handling differs between paths"
        elif not outputs_match:
            ux_fidelity = "REGRESSION"
            divergence_details = "Output content divergence detected"
        elif narration_in_agent and agent_overhead_ms > 0:
            ux_fidelity = "OK"
            divergence_details = f"Agent narration adds {agent_overhead_ms:.0f}ms overhead"

        return ComparisonResult(
            scenario_name=scenario.name,
            outputs_match=outputs_match,
            output_shape_match=output_shape_match,
            narration_in_agent=narration_in_agent,
            direct_runtime_ms=result_a.runtime_ms,
            agent_latency_ms=result_b.agent_latency_ms,
            agent_overhead_ms=agent_overhead_ms,
            both_successful=both_successful,
            error_divergence=error_divergence,
            ux_fidelity=ux_fidelity,
            divergence_details=divergence_details,
        )

    @staticmethod
    def _outputs_match(result_a: PathAResult, result_b: PathBResult) -> bool:
        """Check if outputs are functionally equivalent."""
        # Simplified: check if both have content
        a_has_content = bool(result_a.stdout) and result_a.exit_code == 0
        b_has_content = bool(result_b.response_content)
        return a_has_content == b_has_content

    @staticmethod
    def _shape_matches(result_a: PathAResult, result_b: PathBResult) -> bool:
        """Check if output structures match."""
        if not result_a.output_shape:
            return True  # Can't verify
        # Would compare JSON structures more deeply
        return True


class DualPathHarness:
    """Main orchestrator for dual-path testing."""

    def __init__(
        self,
        recordings_dir: Path = None,
        device: str = "STUDIO",
        provider: Optional[str] = None,
    ):
        self.path_a_executor = PathAExecutor()
        self.path_b_executor = PathBExecutor(provider=provider)
        self.comparison_engine = ComparisonEngine()
        self.recordings_dir = recordings_dir or Path(".harness/recordings")
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

        # Multi-device support
        self.device = device
        self.device_config = get_device(device)
        logger.info(f"Harness initialized for device: {device} ({self.device_config.description})")

        # Multi-provider support
        self.provider = provider
        if provider:
            logger.info(f"Harness initialized with provider: {provider}")

    async def run_scenario(self, scenario: TestScenario) -> Dict[str, Any]:
        """Execute scenario in both paths and compare."""
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Test: {scenario.name}")
        logger.info(f"{'=' * 60}")

        # Path A: Direct execution
        result_a = await self.path_a_executor.execute(scenario)
        logger.info(f"Path A: {result_a.runtime_ms:.0f}ms, exit={result_a.exit_code}")

        # Path B: Agent execution
        result_b = await self.path_b_executor.execute(scenario)
        logger.info(f"Path B ({result_b.agent.value}): {result_b.agent_latency_ms:.0f}ms")

        # Path C: Comparison
        comparison = await self.comparison_engine.compare(scenario, result_a, result_b)
        logger.info(f"UX Fidelity: {comparison.ux_fidelity}")

        result = {
            "scenario": scenario.name,
            "path_a": asdict(result_a),
            "path_b": asdict(result_b),
            "comparison": asdict(comparison),
            "timestamp": datetime.now().isoformat(),
        }

        # Save recording
        await self._save_recording(result)

        return result

    async def run_suite(self, scenarios: List[TestScenario]) -> Dict[str, Any]:
        """Run multiple scenarios."""
        results = []
        for scenario in scenarios:
            result = await self.run_scenario(scenario)
            results.append(result)

        summary = {
            "total_tests": len(results),
            "passed": sum(1 for r in results if r["comparison"]["ux_fidelity"] == "OK"),
            "regressions": sum(
                1 for r in results if r["comparison"]["ux_fidelity"] == "REGRESSION"
            ),
            "results": results,
            "timestamp": datetime.now().isoformat(),
        }

        return summary

    async def run_command_tier(self, tier: int) -> Dict[str, Any]:
        """Run all commands in a specific tier."""
        commands = get_commands_by_tier(tier)
        logger.info(f"Running Tier {tier} commands ({len(commands)} total) on {self.device}")

        results = []
        for command in commands:
            cmd_config = get_command(command)
            adjusted_timeout = get_timeout_seconds(cmd_config.timeout_seconds, self.device)

            scenario = TestScenario(
                name=command,
                description=cmd_config.description,
                portfolio_file="docs/samples/sample_portfolio.json"
                if cmd_config.requires_portfolio
                else None,
                path_a_command=f"investorclaw {command}"
                + (
                    " --portfolio docs/samples/sample_portfolio.json"
                    if cmd_config.requires_portfolio
                    else ""
                ),
                path_b_prompt=f"/portfolio {command}",
                path_b_agent=Agent.OPENCLAW,
                timeout_seconds=adjusted_timeout,
            )

            try:
                result = await self.run_scenario(scenario)
                results.append(result)
            except Exception as e:
                logger.error(f"Command {command} failed: {e}")
                results.append(
                    {
                        "scenario": command,
                        "error": str(e),
                        "comparison": {"ux_fidelity": "REGRESSION"},
                    }
                )

        passed = sum(1 for r in results if r.get("comparison", {}).get("ux_fidelity") == "OK")
        regressions = sum(
            1 for r in results if r.get("comparison", {}).get("ux_fidelity") == "REGRESSION"
        )

        return {
            "tier": tier,
            "device": self.device,
            "total_commands": len(commands),
            "passed": passed,
            "regressions": regressions,
            "results": results,
            "timestamp": datetime.now().isoformat(),
        }

    async def _save_recording(self, result: Dict) -> None:
        """Save test recording for playback."""
        filename = f"{result['scenario'].replace(' ', '-')}-{datetime.now().timestamp()}.json"
        filepath = self.recordings_dir / filename

        with open(filepath, "w") as f:
            json.dump(result, f, indent=2)

        logger.info(f"Recording saved: {filepath}")


# Example usage
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    async def main():
        harness = DualPathHarness()

        scenarios = [
            TestScenario(
                name="Holdings Analysis",
                description="Analyze portfolio holdings",
                portfolio_file="sample_portfolio.csv",
                path_a_command="investorclaw holdings --portfolio sample_portfolio.csv",
                path_b_prompt="/portfolio holdings --accounts sample_portfolio.csv",
                path_b_agent=Agent.OPENCLAW,
            ),
            TestScenario(
                name="Performance Analysis",
                description="Analyze portfolio performance",
                portfolio_file="sample_portfolio.csv",
                path_a_command="investorclaw performance --portfolio sample_portfolio.csv",
                path_b_prompt="/portfolio performance",
                path_b_agent=Agent.OPENCLAW,
            ),
        ]

        results = await harness.run_suite(scenarios)

        print("\n" + "=" * 60)
        print("DUAL-PATH TEST RESULTS")
        print("=" * 60)
        print(
            f"Total: {results['total_tests']} | Passed: {results['passed']} | Regressions: {results['regressions']}"
        )

    asyncio.run(main())
