"""Smoke tests for V13 orchestrator Path B executor."""

import asyncio
from unittest.mock import AsyncMock, patch

from harness.orchestrator import Agent, PathBExecutor, PathBResult, TestScenario


def make_scenario() -> TestScenario:
    return TestScenario(
        name="test-001",
        description="Path B smoke test",
        portfolio_file="test.csv",
        path_a_command="investorclaw portfolio",
        path_b_prompt="show portfolio via agent",
        path_b_agent=Agent.OPENCLAW,
        timeout_seconds=30,
    )


def test_execute_openclaw_returns_populated_result() -> None:
    fake_result = {
        "response_content": "Portfolio: AAPL 10%, MSFT 20%",
        "duration_ms": 1234,
        "model_used": "gpt-4o",
        "exit_code": 0,
        "error": None,
        "tokens_prompt": 50,
        "tokens_completion": 100,
        "full_conversation": [],
        "device": "typhon",
        "memory_usage_mb": 128.0,
        "metadata": {},
    }
    scenario = make_scenario()
    executor = PathBExecutor()

    with patch(
        "harness.agent_clients.openclaw.OpenClawClient.send_message",
        new_callable=AsyncMock,
        return_value=fake_result,
    ):
        result = asyncio.run(executor._execute_openclaw(scenario))

    assert isinstance(result, PathBResult)
    assert result.agent == Agent.OPENCLAW
    assert result.response_content == "Portfolio: AAPL 10%, MSFT 20%"
    assert result.agent_latency_ms == 1234
    assert result.model_used == "gpt-4o"
