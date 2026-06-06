"""P0: Holdings loading stage."""

import asyncio
import sys
from pathlib import Path
from typing import Optional

_root = str(Path(__file__).resolve().parent.parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from ic_engine.internal.holdings_loader import HoldingsLoader
from ic_engine.internal.stages import PipelineContext, PipelineStage, StageResult


class HoldingsStage(PipelineStage):
    """P0: Load holdings from portfolio file. Prerequisite for all analyses."""

    stage_name = "holdings"
    depends_on = []
    parallel_group = "P0"

    def __init__(self, holdings_file: Optional[str] = None):
        self.holdings_file = holdings_file
        self.loader = HoldingsLoader()

    async def execute(self, context: PipelineContext) -> StageResult:
        """Load holdings from file and return PortfolioData.

        StageResult.data is a structured envelope view: positions, totals,
        accounts, and basic per-symbol weights/sectors. The narrator uses
        this to answer questions about portfolio totals, top positions,
        sector breakdowns, and account hierarchy.
        """
        if not self.holdings_file:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error="No holdings_file provided",
            )

        try:
            portfolio_data = await asyncio.to_thread(self.loader.load, self.holdings_file)
            data = self._build_envelope_view(portfolio_data)
            return StageResult(
                stage_name=self.stage_name,
                status="success",
                data=data,
                _timing={"execution_ms": 0},  # Timing added by orchestrator
                _metadata={"positions_count": len(portfolio_data.positions)},
            )
        except Exception as e:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )

    @staticmethod
    def _build_envelope_view(portfolio_data) -> dict:
        """Build the dict that lands at envelope.sections.holdings.

        Mirrors the shape `investorclaw holdings` writes — summary +
        top_equity (top 25 by value) + accounts hierarchy. Narrator uses
        these fields to answer position/total/sector/account questions.
        """
        positions = portfolio_data.positions or []
        total_value = float(portfolio_data.total_value or 0.0)

        equities, bonds, cash, other = [], [], [], []
        accounts: dict = {}
        sector_weights: dict[str, float] = {}

        for p in positions:
            d = p.to_dict() if hasattr(p, "to_dict") else dict(p.__dict__)
            value = float(d.get("value") or d.get("market_value") or 0.0)
            if total_value > 0:
                d["weight_pct"] = round(value / total_value * 100, 4)
            else:
                d["weight_pct"] = 0.0
            asset_type = (d.get("type") or d.get("asset_class") or "equity").lower()
            if asset_type in ("bond", "fixed_income"):
                bonds.append(d)
            elif asset_type in ("cash", "cash_equivalent"):
                cash.append(d)
            elif asset_type in (
                "crypto",
                "futures",
                "metals",
                # Options ride the same non-equity/bond/cash "other" bucket —
                # all alias spellings accepted (mirrors schema.py CANONICAL_KEYS).
                "option",
                "options",
                "call",
                "put",
                "equity_option",
            ):
                other.append(d)
            else:
                equities.append(d)

            account = d.get("account") or "unspecified"
            entry = accounts.setdefault(account, {"value": 0.0, "position_count": 0})
            entry["value"] = round(entry["value"] + value, 2)
            entry["position_count"] += 1

            sector = d.get("sector") or "Unknown"
            sector_weights[sector] = round(sector_weights.get(sector, 0.0) + (d["weight_pct"]), 4)

        def _val(d):
            return float(d.get("value") or d.get("market_value") or 0.0)

        equities.sort(key=_val, reverse=True)
        bonds.sort(key=_val, reverse=True)

        equity_value = round(sum(_val(e) for e in equities), 2)
        bond_value = round(sum(_val(b) for b in bonds), 2)
        cash_value = round(sum(_val(c) for c in cash), 2)

        for acct in accounts.values():
            if total_value > 0:
                acct["weight_pct"] = round(acct["value"] / total_value * 100, 2)

        return {
            "summary": {
                "total_value": round(total_value, 2),
                "equity_value": equity_value,
                "bond_value": bond_value,
                "cash_value": cash_value,
                "equity_pct": round(equity_value / total_value * 100, 2) if total_value else 0.0,
                "bond_pct": round(bond_value / total_value * 100, 2) if total_value else 0.0,
                "cash_pct": round(cash_value / total_value * 100, 2) if total_value else 0.0,
                "position_count": {
                    "equity": len(equities),
                    "bond": len(bonds),
                    "cash": len(cash),
                    "other": len(other),
                },
                "as_of": portfolio_data.as_of_date,
                "cdm_version": portfolio_data.cdm_version,
            },
            "top_equity": equities[:25],
            "top_bonds": bonds[:10],
            "accounts": accounts,
            "sector_weights": sector_weights,
            "remaining_equity_count": max(0, len(equities) - 25),
        }
