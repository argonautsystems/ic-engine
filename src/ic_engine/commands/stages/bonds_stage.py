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
            from ic_engine.commands.bond_analyzer import BondAnalyzer

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

            # Attach a Treasury yield curve so the narrator can answer
            # bond-strategy / yield-curve / Fed-policy questions even when
            # FRED_API_KEY is missing. Uses public treasury_fiscaldata as
            # the no-key fallback. Best-effort.
            analysis_data["treasury_yield_curve"] = self._fetch_yield_curve()

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

    @staticmethod
    def _fetch_yield_curve() -> dict:
        """Fetch US Treasury yield curve via PriceProvider chain
        (treasury_fiscaldata; FRED if FRED_API_KEY is wired in future).
        Best-effort: returns {} on any failure."""
        try:
            from ic_engine.providers.price_provider import PriceProvider
            curve = PriceProvider().get_treasury_yields()
            return curve or {}
        except Exception as e:
            logger.debug(f"yield_curve fetch skipped: {e}")
            return {}
