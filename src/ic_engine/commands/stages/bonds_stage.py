"""P1b: Bond analysis stage."""

import asyncio
import logging
import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from ic_engine.internal.stages import PipelineContext, PipelineStage, StageResult

logger = logging.getLogger(__name__)


class BondsStage(PipelineStage):
    """P1b: Bond analysis - runs in parallel with other P1 stages."""

    stage_name = "bonds"
    depends_on = ["holdings"]
    parallel_group = "P1"

    async def execute(self, context: PipelineContext) -> StageResult:
        """Analyze bonds in portfolio."""
        if not context.portfolio_data:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error="No portfolio_data from holdings stage",
            )

        # Check if portfolio has bonds
        bonds = [p for p in context.portfolio_data.positions if p.asset_class == "bond"]
        if not bonds:
            return StageResult(
                stage_name=self.stage_name,
                status="skipped",
                data={"message": "No bonds in portfolio"},
                _metadata={"bonds_count": 0},
            )

        try:
            result = await asyncio.to_thread(self._run_bond_analysis, bonds)
            return result
        except Exception as e:
            logger.error(f"Bond analysis failed: {e}")
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )

    def _run_bond_analysis(self, bonds: list) -> StageResult:
        """Run bond analyzer on portfolio bonds."""
        try:
            from commands.bond_analyzer import BondAnalyzer

            # Convert positions to bond format
            bond_list = []
            for bond_pos in bonds:
                bond_data = {
                    "symbol": bond_pos.symbol,
                    "name": getattr(bond_pos, "name", bond_pos.symbol),
                    "isin": getattr(bond_pos, "isin", None),
                    "coupon_rate": getattr(bond_pos, "coupon_rate", 0.0),
                    "maturity_date": getattr(bond_pos, "maturity_date", None),
                    "current_price": getattr(bond_pos, "current_price", 100.0),
                    "yield_to_maturity": getattr(bond_pos, "ytm", None),
                    "duration": getattr(bond_pos, "duration", None),
                    "convexity": getattr(bond_pos, "convexity", None),
                }
                bond_list.append(bond_data)

            # Run analyzer
            analyzer = BondAnalyzer()
            portfolio_metrics = analyzer.analyze_portfolio(bond_list)

            # Extract results
            analysis_data = {
                "bonds_analyzed": len(bond_list),
                "portfolio_metrics": portfolio_metrics.__dict__ if portfolio_metrics else {},
                "individual_bonds": [
                    {
                        "symbol": b["symbol"],
                        "metrics": analyzer.analyze_bond(b).__dict__
                        if analyzer.analyze_bond(b)
                        else None,
                    }
                    for b in bond_list
                ],
            }

            return StageResult(
                stage_name=self.stage_name,
                status="success",
                data=analysis_data,
                _metadata={"bonds_count": len(bond_list)},
            )

        except Exception as e:
            logger.error(f"Bond analysis failed: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )
