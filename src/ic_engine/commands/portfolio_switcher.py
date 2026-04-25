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
Portfolio Switcher - Manage multiple named portfolios.

Commands:
  portfolio list              — List all portfolios
  portfolio create <name>     — Create new portfolio
  portfolio switch <name>     — Switch active portfolio
  portfolio view              — Show current portfolio
  portfolio meta              — Show consolidated view of all portfolios
  portfolio rename <slug> <new_name> — Rename portfolio
  portfolio delete <slug>     — Delete a portfolio
"""

import json
import logging
import sys
from pathlib import Path
from typing import Optional

# Ensure InvestorClaw root is in Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ic_engine.config.path_resolver import get_portfolio_dir
from ic_engine.config.portfolio_manager import PortfolioManager

logger = logging.getLogger(__name__)


class PortfolioSwitcher:
    """CLI handler for portfolio management."""

    def __init__(self, portfolio_root: Optional[Path] = None):
        """Initialize switcher with portfolio root."""
        if portfolio_root is None:
            portfolio_root = get_portfolio_dir(Path.cwd())

        self.manager = PortfolioManager(portfolio_root)

    def list(self) -> dict:
        """List all portfolios."""
        portfolios = self.manager.list_portfolios()
        active = self.manager.get_active_portfolio()

        result = {
            "portfolios": [],
            "active": active.slug if active else None,
            "count": len(portfolios),
        }

        for portfolio in portfolios:
            result["portfolios"].append(
                {
                    "name": portfolio.name,
                    "slug": portfolio.slug,
                    "description": portfolio.description,
                    "created": portfolio.created,
                    "is_active": portfolio.slug == active.slug if active else False,
                }
            )

        return result

    def create(self, name: str, description: Optional[str] = None) -> dict:
        """Create new portfolio."""
        try:
            portfolio_dir = self.manager.create_portfolio(name, description)
            return {
                "status": "created",
                "name": name,
                "path": str(portfolio_dir),
                "message": f"Portfolio '{name}' created successfully",
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    def switch(self, slug_or_name: str) -> dict:
        """Switch active portfolio."""
        try:
            portfolio = self.manager.set_active_portfolio(slug_or_name)
            return {
                "status": "switched",
                "name": portfolio.name,
                "slug": portfolio.slug,
                "message": f"Active portfolio: {portfolio.name}",
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    def view(self) -> dict:
        """Show current portfolio."""
        active = self.manager.get_active_portfolio()

        if not active:
            return {
                "status": "no_active",
                "message": "No portfolio currently selected. Use 'portfolio create' to create one.",
            }

        portfolio_dir = self.manager.get_portfolio_path(active.slug)
        holdings_file = portfolio_dir / "holdings.json"

        if not holdings_file.exists():
            return {
                "status": "no_holdings",
                "portfolio": active.name,
                "message": f"Portfolio '{active.name}' has no holdings yet",
            }

        try:
            with open(holdings_file) as f:
                holdings = json.load(f)

            return {
                "status": "ok",
                "portfolio": active.name,
                "description": active.description,
                "created": active.created,
                "holdings_count": len(holdings.get("holdings", [])),
                "data": holdings,
            }
        except (json.JSONDecodeError, IOError) as e:
            return {"status": "error", "message": f"Failed to read holdings: {e}"}

    def meta(self) -> dict:
        """Show consolidated view of all portfolios."""
        return self.manager.get_meta_portfolio()

    def rename(self, slug: str, new_name: str) -> dict:
        """Rename a portfolio."""
        try:
            portfolio = self.manager.rename_portfolio(slug, new_name)
            return {
                "status": "renamed",
                "name": portfolio.name,
                "slug": portfolio.slug,
                "message": f"Portfolio renamed to '{portfolio.name}'",
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    def delete(self, slug: str) -> dict:
        """Delete a portfolio."""
        try:
            self.manager.delete_portfolio(slug)
            return {
                "status": "deleted",
                "slug": slug,
                "message": f"Portfolio '{slug}' deleted",
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}


def main(action: str = "list", arg1: Optional[str] = None, arg2: Optional[str] = None):
    """
    CLI entry point for portfolio management.

    Usage:
      portfolio list
      portfolio create <name> [description]
      portfolio switch <name>
      portfolio view
      portfolio meta
      portfolio rename <slug> <new_name>
      portfolio delete <slug>
    """
    try:
        switcher = PortfolioSwitcher()

        if action == "list":
            result = switcher.list()
        elif action == "create":
            if not arg1:
                result = {"status": "error", "message": "Name required: portfolio create <name>"}
            else:
                result = switcher.create(arg1, arg2)
        elif action == "switch":
            if not arg1:
                result = {"status": "error", "message": "Name required: portfolio switch <name>"}
            else:
                result = switcher.switch(arg1)
        elif action == "view":
            result = switcher.view()
        elif action == "meta":
            result = switcher.meta()
        elif action == "rename":
            if not arg1 or not arg2:
                result = {"status": "error", "message": "Usage: portfolio rename <slug> <new_name>"}
            else:
                result = switcher.rename(arg1, arg2)
        elif action == "delete":
            if not arg1:
                result = {"status": "error", "message": "Slug required: portfolio delete <slug>"}
            else:
                result = switcher.delete(arg1)
        else:
            result = {"status": "error", "message": f"Unknown action: {action}"}

        # Output as JSON
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") != "error" else 1

    except Exception as e:
        logger.exception("Portfolio management failed")
        error_result = {
            "status": "error",
            "message": f"Portfolio management failed: {e}",
        }
        print(json.dumps(error_result, indent=2))
        return 1


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "list"
    arg1 = sys.argv[2] if len(sys.argv) > 2 else None
    arg2 = sys.argv[3] if len(sys.argv) > 3 else None

    sys.exit(main(action, arg1, arg2))
