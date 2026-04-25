"""
Test command matrix for InvestorClaw harness.

Organizes 22 public commands into tiers by expected execution speed.
Used to support incremental testing: Tier 1 for fast validation,
Tier 2 for medium-length tests, Tier 3 for full suite.
"""

from dataclasses import dataclass
from typing import List


@dataclass
class CommandConfig:
    """Configuration for a test command."""

    name: str  # Command name (e.g., "holdings")
    tier: int  # Test tier (1=fast, 2=medium, 3=slow)
    timeout_seconds: int  # Timeout for this command
    requires_portfolio: bool  # Whether command needs portfolio data
    description: str  # Human-readable description


# All public commands organized by tier
COMMAND_MATRIX = {
    # Tier 1: Fast commands (<1-2 seconds)
    "holdings": CommandConfig(
        name="holdings",
        tier=1,
        timeout_seconds=5,
        requires_portfolio=True,
        description="Portfolio snapshot with current prices",
    ),
    "performance": CommandConfig(
        name="performance",
        tier=1,
        timeout_seconds=5,
        requires_portfolio=True,
        description="Returns, Sharpe ratio, volatility analysis",
    ),
    "bonds": CommandConfig(
        name="bonds",
        tier=1,
        timeout_seconds=5,
        requires_portfolio=True,
        description="Bond analytics: YTM, duration, FRED comparison",
    ),
    "analyst": CommandConfig(
        name="analyst",
        tier=1,
        timeout_seconds=5,
        requires_portfolio=True,
        description="Wall Street ratings and consensus data",
    ),
    "news": CommandConfig(
        name="news",
        tier=1,
        timeout_seconds=5,
        requires_portfolio=True,
        description="Recent news and sentiment for holdings",
    ),
    "news-plan": CommandConfig(
        name="news-plan",
        tier=1,
        timeout_seconds=5,
        requires_portfolio=True,
        description="News correlation and impact analysis",
    ),
    "synthesize": CommandConfig(
        name="synthesize",
        tier=1,
        timeout_seconds=8,
        requires_portfolio=True,
        description="Multi-factor portfolio analysis synthesis",
    ),
    "fixed-income": CommandConfig(
        name="fixed-income",
        tier=1,
        timeout_seconds=5,
        requires_portfolio=True,
        description="Fixed-income focused analysis",
    ),
    "optimize": CommandConfig(
        name="optimize",
        tier=1,
        timeout_seconds=8,
        requires_portfolio=True,
        description="Modern Portfolio Theory optimization",
    ),
    "report": CommandConfig(
        name="report",
        tier=1,
        timeout_seconds=5,
        requires_portfolio=True,
        description="Export portfolio to CSV/Excel",
    ),
    # Tier 2: Medium commands (1-5 seconds)
    "eod-report": CommandConfig(
        name="eod-report",
        tier=2,
        timeout_seconds=8,
        requires_portfolio=True,
        description="End-of-day HTML report with email narrative",
    ),
    "session": CommandConfig(
        name="session",
        tier=2,
        timeout_seconds=5,
        requires_portfolio=True,
        description="Risk profiling and session calibration",
    ),
    "fa-topics": CommandConfig(
        name="fa-topics",
        tier=2,
        timeout_seconds=5,
        requires_portfolio=False,
        description="Fundamental analysis discussion topics",
    ),
    "lookup": CommandConfig(
        name="lookup",
        tier=2,
        timeout_seconds=5,
        requires_portfolio=False,
        description="Ticker symbol deep-dive analysis",
    ),
    "guardrails": CommandConfig(
        name="guardrails",
        tier=2,
        timeout_seconds=5,
        requires_portfolio=False,
        description="Financial literacy and risk education",
    ),
    "update-identity": CommandConfig(
        name="update-identity",
        tier=2,
        timeout_seconds=5,
        requires_portfolio=True,
        description="Update portfolio metadata and identity",
    ),
    "run": CommandConfig(
        name="run",
        tier=2,
        timeout_seconds=15,
        requires_portfolio=True,
        description="Full analysis pipeline execution",
    ),
    "stonkmode": CommandConfig(
        name="stonkmode",
        tier=2,
        timeout_seconds=8,
        requires_portfolio=True,
        description="Humorous narration mode for synthesis",
    ),
    "check-updates": CommandConfig(
        name="check-updates",
        tier=2,
        timeout_seconds=3,
        requires_portfolio=False,
        description="Version and update checking",
    ),
    "ollama-setup": CommandConfig(
        name="ollama-setup",
        tier=2,
        timeout_seconds=10,
        requires_portfolio=False,
        description="Local Ollama inference setup",
    ),
    # Tier 3: Slow commands (3-10+ seconds)
    "help": CommandConfig(
        name="help",
        tier=3,
        timeout_seconds=3,
        requires_portfolio=False,
        description="Command help and documentation",
    ),
    "setup": CommandConfig(
        name="setup",
        tier=3,
        timeout_seconds=15,
        requires_portfolio=False,
        description="Portfolio setup and initialization",
    ),
}


def get_command(command_name: str) -> CommandConfig:
    """Get command configuration by name."""
    if command_name not in COMMAND_MATRIX:
        raise ValueError(
            f"Unknown command: {command_name}. "
            f"Available: {', '.join(sorted(COMMAND_MATRIX.keys()))}"
        )
    return COMMAND_MATRIX[command_name]


def get_command_suite(tier: int) -> List[str]:
    """Get all commands for a given tier and below."""
    if tier < 1 or tier > 3:
        raise ValueError("Tier must be 1, 2, or 3")
    return sorted([cmd for cmd, cfg in COMMAND_MATRIX.items() if cfg.tier <= tier])


def get_commands_by_tier(tier: int) -> List[str]:
    """Get only commands for a specific tier (exact match)."""
    if tier < 1 or tier > 3:
        raise ValueError("Tier must be 1, 2, or 3")
    return sorted([cmd for cmd, cfg in COMMAND_MATRIX.items() if cfg.tier == tier])


def get_portfolio_commands() -> List[str]:
    """Get all commands that require portfolio data."""
    return sorted([cmd for cmd, cfg in COMMAND_MATRIX.items() if cfg.requires_portfolio])


def get_no_portfolio_commands() -> List[str]:
    """Get all commands that don't require portfolio data."""
    return sorted([cmd for cmd, cfg in COMMAND_MATRIX.items() if not cfg.requires_portfolio])


# Summary statistics
TOTAL_COMMANDS = len(COMMAND_MATRIX)
TIER_1_COMMANDS = len(get_commands_by_tier(1))
TIER_2_COMMANDS = len(get_commands_by_tier(2))
TIER_3_COMMANDS = len(get_commands_by_tier(3))
