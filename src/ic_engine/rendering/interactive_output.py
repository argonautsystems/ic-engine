#!/usr/bin/env python3
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
Interactive terminal output helpers for commands.

Detects Claude Code and interactive terminals, providing:
1. ANSI color codes for formatted tables
2. Table formatting utilities
3. Interactive vs. piped output detection
"""

import os
import sys
from typing import Any, Dict, List, Optional


# ANSI color codes (inline, no dependencies)
class Colors:
    """ANSI color codes for terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Colors
    BLACK = "\033[30m"
    RED = "\033[91m"  # Bright red — losses, errors
    GREEN = "\033[92m"  # Bright green — gains, success
    YELLOW = "\033[93m"  # Yellow — warnings
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"  # Bright cyan — tickers, headers
    WHITE = "\033[97m"  # Bright white — values
    GREY = "\033[90m"  # Dark grey — metadata, separators

    @staticmethod
    def gain(value: float) -> str:
        """Return color for a gain/loss value."""
        if value > 0:
            return Colors.GREEN
        elif value < 0:
            return Colors.RED
        else:
            return Colors.WHITE


def is_interactive() -> bool:
    """Check if output is going to an interactive terminal (not piped/redirected).

    Returns True if:
    - stdout is a TTY (interactive terminal)
    - TERM env var is set (interactive shell)
    - Running in Claude Code
    """
    if sys.stdout.isatty():
        return True

    if os.environ.get("TERM"):
        return True

    # Check for Claude Code context
    if os.environ.get("CLAUDE_CODE"):
        return True

    return False


def format_currency(value: float, show_sign: bool = False) -> str:
    """Format a number as currency with ANSI color.

    Args:
        value: Number to format
        show_sign: Include + for positive values

    Returns:
        Formatted currency string with color
    """
    sign = "+" if show_sign and value > 0 else ""
    color = Colors.gain(value)
    return f"{color}{sign}${value:,.2f}{Colors.RESET}"


def format_percent(value: float, decimals: int = 2) -> str:
    """Format a percentage with ANSI color.

    Args:
        value: Percentage value (e.g., 5.25 for 5.25%)
        decimals: Number of decimal places

    Returns:
        Formatted percentage string with color
    """
    color = Colors.gain(value)
    return f"{color}{value:{decimals + 3}.{decimals}f}%{Colors.RESET}"


def format_row(
    columns: List[str], widths: Optional[List[int]] = None, separator: str = "│", padding: int = 1
) -> str:
    """Format a table row with proper alignment.

    Args:
        columns: List of column values
        widths: List of column widths (calculated if not provided)
        separator: Column separator character
        padding: Padding on each side of values

    Returns:
        Formatted row string
    """
    if widths is None:
        widths = [len(str(col)) for col in columns]

    pad = " " * padding
    formatted_cols = []
    for col, width in zip(columns, widths):
        col_str = str(col)
        formatted_cols.append(col_str.ljust(width))

    return f"{separator}{pad}{f'{pad}{separator}{pad}'.join(formatted_cols)}{pad}{separator}"


def format_header(title: str, width: int = 80) -> str:
    """Format a section header with ANSI colors.

    Args:
        title: Header text
        width: Total width of header

    Returns:
        Formatted header string
    """
    return f"\n{Colors.BOLD}{Colors.CYAN}{title}{Colors.RESET} {Colors.GREY}{'─' * (width - len(title) - 2)}{Colors.RESET}"


def print_summary(
    title: str, summary: Dict[str, Any], labels: Optional[Dict[str, str]] = None
) -> None:
    """Print a summary section with key-value pairs.

    Args:
        title: Section title
        summary: Dictionary of values to display
        labels: Optional mapping of keys to display labels
    """
    print(format_header(title))

    labels = labels or {}
    for key, value in summary.items():
        label = labels.get(key, key.replace("_", " ").title())

        if isinstance(value, (int, float)):
            if "percent" in key.lower() or "rate" in key.lower():
                formatted = format_percent(value)
            elif "price" in key.lower() or "value" in key.lower() or "cost" in key.lower():
                formatted = format_currency(value)
            else:
                formatted = f"{value:,.2f}"
        else:
            formatted = str(value)

        print(f"  {Colors.CYAN}{label}{Colors.RESET}: {Colors.WHITE}{formatted}{Colors.RESET}")


def should_output_json() -> bool:
    """Determine if JSON should be output to stdout.

    Returns False (don't output JSON) if output is interactive.
    Returns True (output JSON) if output is piped/redirected or explicitly requested.
    """
    return not is_interactive()
