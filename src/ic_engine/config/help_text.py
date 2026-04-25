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
Help text and command documentation for InvestorClaw.
"""


def show_help():
    """Display help message."""
    print(
        """
InvestorClaw - Portfolio & Bond Analysis

Usage: /portfolio <command>

Holdings & Prices
  holdings / snapshot / prices       - Portfolio snapshot with current prices

Performance Analysis
  performance / analyze / returns    - Returns, risk metrics, asset allocation

Bond Analysis
  bonds / bond-analysis              - Bond analysis (YTM, duration, tax yield)

Reports & Exports
  report / export / csv / excel      - Generate CSV/Excel reports

News & Sentiment
  news / sentiment                   - News correlated to holdings

Analyst Data
  analyst / analysts / ratings       - Analyst ratings and price targets

Portfolio Analysis
  analysis / portfolio-analysis      - Educational portfolio analysis
  synthesize / multi-factor / recommend - Multi-factor synthesis

Fixed Income Analysis
  fixed-income / bond-strategy       - Fixed income strategy

Risk Calibration
  session / risk-profile / calibrate - Set risk profile (heat + macro concerns)

Guardrails
  guardrails [--prime] [--query "..."] [--status] - Model compliance enforcement

Portfolio Management
  portfolio / portfolios             - List and manage portfolios
  portfolio-list / portfolio-switch  - Switch between portfolios

Setup & Configuration
  setup / init                       - Auto-discover and consolidate portfolios
  session / risk-profile             - Configure risk profile and preferences
  llm-config                         - Configure LLM provider

Utilities
  update-identity                    - Update agent IDENTITY.md with rules
  check-updates / update             - Check for updates
  help                               - Show this help message

Key Workflows
  investorclaw complete              - Full analysis (all dimensions)
  investorclaw portfolio-overview    - Portfolio summary + allocation
  investorclaw optimization-plan     - Rebalancing & tax strategies
  investorclaw intelligence          - Synthesis + attribution + peer

Reports saved to: ~/portfolio_reports/
Add --verbose to any command for detailed output.

Dashboard: Use separate InvestorClaw Dashboard application for visualization.
    """.strip()
    )
