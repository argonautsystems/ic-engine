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
Portfolio Manager - Manages multiple named portfolios.

Allows users to:
- Create multiple portfolios with friendly names (e.g., "My wife's portfolio", "Broker 1")
- Switch between active portfolios
- View consolidated (meta) portfolio combining all others
- Persist portfolio metadata and active selection

Portfolio Directory Structure:
  ~/.portfolios/
    ├── portfolios.json          (metadata: names, descriptions, active selection)
    ├── My_wifes_portfolio/
    │   └── holdings.json
    ├── Broker_1/
    │   └── holdings.json
    └── Broker_2/
        └── holdings.json

Each portfolio directory contains holdings.json (CDM format).
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PortfolioMetadata:
    """Metadata for a single portfolio."""

    name: str  # Display name (e.g., "My wife's portfolio")
    slug: str  # URL-safe slug (e.g., "my_wifes_portfolio")
    description: Optional[str] = None
    created: Optional[str] = None
    last_updated: Optional[str] = None
    is_meta: bool = False  # True if this is a consolidated/meta portfolio

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "PortfolioMetadata":
        return PortfolioMetadata(**data)


class PortfolioManager:
    """Manages multiple named portfolios."""

    METADATA_FILE = "portfolios.json"

    def __init__(self, portfolio_root: Path):
        """
        Initialize portfolio manager.

        Args:
            portfolio_root: Root directory for all portfolios (e.g., ~/.portfolios/)
        """
        self.root = Path(portfolio_root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.root / self.METADATA_FILE

    def _load_metadata(self) -> Dict[str, Any]:
        """Load portfolio metadata from file."""
        if not self.metadata_path.exists():
            return {
                "active": None,
                "portfolios": {},
                "version": "1.0",
                "created": datetime.now().isoformat(),
            }

        try:
            with open(self.metadata_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load portfolio metadata: {e}")
            return {"active": None, "portfolios": {}, "version": "1.0"}

    def _save_metadata(self, metadata: Dict[str, Any]) -> None:
        """Save portfolio metadata to file."""
        metadata["last_modified"] = datetime.now().isoformat()
        with open(self.metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)
        logger.debug(f"Saved portfolio metadata to {self.metadata_path}")

    def _slugify(self, name: str) -> str:
        """Convert display name to URL-safe slug."""
        return name.lower().replace(" ", "_").replace("-", "_").replace("'", "")

    def create_portfolio(
        self,
        name: str,
        description: Optional[str] = None,
        set_active: bool = True,
    ) -> Path:
        """
        Create a new portfolio with display name.

        Args:
            name: Display name (e.g., "My wife's portfolio")
            description: Optional description
            set_active: If True, set as active portfolio

        Returns:
            Path to portfolio directory (e.g., ~/.portfolios/my_wifes_portfolio/)
        """
        slug = self._slugify(name)
        portfolio_dir = self.root / slug

        if portfolio_dir.exists():
            raise ValueError(f"Portfolio '{name}' already exists")

        portfolio_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created portfolio: {name} at {portfolio_dir}")

        # Update metadata
        metadata = self._load_metadata()
        metadata["portfolios"][slug] = PortfolioMetadata(
            name=name,
            slug=slug,
            description=description,
            created=datetime.now().isoformat(),
        ).to_dict()

        if set_active or metadata["active"] is None:
            metadata["active"] = slug

        self._save_metadata(metadata)

        return portfolio_dir

    def list_portfolios(self) -> List[PortfolioMetadata]:
        """List all available portfolios."""
        metadata = self._load_metadata()
        portfolios = []

        for slug, portfolio_data in metadata.get("portfolios", {}).items():
            portfolios.append(PortfolioMetadata.from_dict(portfolio_data))

        return sorted(portfolios, key=lambda p: p.name)

    def get_active_portfolio(self) -> Optional[PortfolioMetadata]:
        """Get currently active portfolio."""
        metadata = self._load_metadata()
        active_slug = metadata.get("active")

        if not active_slug:
            return None

        portfolio_data = metadata.get("portfolios", {}).get(active_slug)
        if portfolio_data:
            return PortfolioMetadata.from_dict(portfolio_data)

        return None

    def set_active_portfolio(self, slug_or_name: str) -> PortfolioMetadata:
        """
        Set active portfolio by slug or display name.

        Args:
            slug_or_name: Slug (e.g., "my_wifes_portfolio") or name (e.g., "My wife's portfolio")

        Returns:
            The activated portfolio metadata
        """
        metadata = self._load_metadata()
        portfolios = metadata.get("portfolios", {})

        # Try exact slug match first
        if slug_or_name in portfolios:
            slug = slug_or_name
        else:
            # Try matching by display name
            slug = None
            for s, p_data in portfolios.items():
                if p_data.get("name", "").lower() == slug_or_name.lower():
                    slug = s
                    break

        if not slug:
            raise ValueError(f"Portfolio not found: {slug_or_name}")

        metadata["active"] = slug
        self._save_metadata(metadata)

        portfolio = PortfolioMetadata.from_dict(portfolios[slug])
        logger.info(f"Active portfolio: {portfolio.name}")
        return portfolio

    def get_portfolio_path(self, slug: Optional[str] = None) -> Path:
        """
        Get path to portfolio directory.

        Args:
            slug: Portfolio slug. If None, uses active portfolio.

        Returns:
            Path to portfolio directory
        """
        if slug is None:
            active = self.get_active_portfolio()
            if not active:
                raise ValueError("No active portfolio set")
            slug = active.slug

        portfolio_dir = self.root / slug
        if not portfolio_dir.exists():
            raise ValueError(f"Portfolio directory not found: {slug}")

        return portfolio_dir

    def get_meta_portfolio(self) -> Dict[str, Any]:
        """
        Get consolidated (meta) portfolio combining all others.

        Returns:
            Combined portfolio data with all holdings from all portfolios
        """
        portfolios = self.list_portfolios()
        combined = {
            "meta_portfolio": True,
            "portfolios": [],
            "total_holdings": 0,
            "by_portfolio": {},
        }

        for portfolio in portfolios:
            portfolio_dir = self.root / portfolio.slug
            holdings_file = portfolio_dir / "holdings.json"

            if not holdings_file.exists():
                logger.warning(f"Holdings file not found for {portfolio.name}")
                continue

            try:
                with open(holdings_file) as f:
                    holdings_data = json.load(f)

                combined["by_portfolio"][portfolio.name] = holdings_data
                combined["portfolios"].append(
                    {
                        "name": portfolio.name,
                        "slug": portfolio.slug,
                        "holdings_count": len(holdings_data.get("holdings", [])),
                    }
                )
                combined["total_holdings"] += len(holdings_data.get("holdings", []))
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Failed to read holdings for {portfolio.name}: {e}")

        return combined

    def delete_portfolio(self, slug: str) -> None:
        """
        Delete a portfolio.

        Args:
            slug: Portfolio slug to delete
        """
        portfolio_dir = self.root / slug
        if not portfolio_dir.exists():
            raise ValueError(f"Portfolio not found: {slug}")

        # Update metadata
        metadata = self._load_metadata()
        if slug in metadata.get("portfolios", {}):
            del metadata["portfolios"][slug]

        # If this was active, clear active selection
        if metadata.get("active") == slug:
            metadata["active"] = None

        self._save_metadata(metadata)

        # Remove directory
        import shutil

        shutil.rmtree(portfolio_dir)
        logger.info(f"Deleted portfolio: {slug}")

    def rename_portfolio(self, slug: str, new_name: str) -> PortfolioMetadata:
        """
        Rename a portfolio (updates display name only, not slug).

        Args:
            slug: Portfolio slug
            new_name: New display name

        Returns:
            Updated portfolio metadata
        """
        metadata = self._load_metadata()
        if slug not in metadata.get("portfolios", {}):
            raise ValueError(f"Portfolio not found: {slug}")

        metadata["portfolios"][slug]["name"] = new_name
        metadata["portfolios"][slug]["last_updated"] = datetime.now().isoformat()
        self._save_metadata(metadata)

        return PortfolioMetadata.from_dict(metadata["portfolios"][slug])

    def export_portfolio_list(self) -> str:
        """
        Export portfolio list as formatted string for CLI/UI.

        Returns:
            Formatted string showing all portfolios and active selection
        """
        portfolios = self.list_portfolios()
        active = self.get_active_portfolio()

        if not portfolios:
            return "No portfolios created yet."

        lines = ["Portfolio List:", ""]
        for portfolio in portfolios:
            marker = "→ " if portfolio.slug == active.slug else "  " if active else "  "
            desc = f" ({portfolio.description})" if portfolio.description else ""
            lines.append(f"{marker}{portfolio.name}{desc}")

        return "\n".join(lines)
