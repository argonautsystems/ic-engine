"""P2: Synthesis stage (depends on P1 completion)."""

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from ic_engine.internal.stages import PipelineContext, PipelineStage, StageResult

logger = logging.getLogger(__name__)


class SynthesisStage(PipelineStage):
    """P2: Multi-dimensional portfolio synthesis (depends on P1)."""

    stage_name = "synthesis"
    depends_on = ["performance", "bonds", "analyst", "news"]
    parallel_group = "P2"

    async def execute(self, context: PipelineContext) -> StageResult:
        """Synthesize P1 results into comprehensive portfolio analysis."""
        if not context.portfolio_data:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error="No portfolio_data from holdings stage",
            )

        try:
            # Collect P1 results
            p1_results = {
                name: context.get_result(name)
                for name in ["performance", "bonds", "analyst", "news"]
            }

            result = await asyncio.to_thread(
                self._synthesize, context.portfolio_data, p1_results, context.cdm_version
            )
            return result
        except Exception as e:
            logger.error(f"Synthesis failed: {e}")
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )

    def _synthesize(self, portfolio, p1_results, cdm_version: str) -> StageResult:
        """Generate portfolio synthesis report."""
        try:
            from commands.portfolio_analyzer import PortfolioAnalyzer

            # Create temporary holdings file in CDM format
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                holdings_json = {
                    "cdmVersion": cdm_version,
                    "portfolio": {
                        "portfolioState": {
                            "positions": [
                                {
                                    "product": {
                                        "productIdentifier": {
                                            "identifierType": "TICKER",
                                            "identifier": p.symbol,
                                        }
                                    },
                                    "asset": {
                                        "assetClass": "Stocks"
                                        if p.asset_class == "equity"
                                        else "Bonds"
                                        if p.asset_class == "bond"
                                        else p.asset_class,
                                        "securityType": "Equity"
                                        if p.asset_class == "equity"
                                        else "Bond"
                                        if p.asset_class == "bond"
                                        else p.asset_class,
                                    },
                                    "priceQuantity": {
                                        "quantity": {
                                            "amount": p.shares or 1.0,
                                            "unit": "shares",
                                        },
                                        "currentPrice": {
                                            "amount": p.current_price or 0.0,
                                            "currency": "USD",
                                        },
                                    },
                                    "marketValue": p.market_value or 0.0,
                                    "costBasis": p.cost_basis
                                    if hasattr(p, "cost_basis")
                                    else p.market_value,
                                }
                                for p in portfolio.positions
                            ]
                        }
                    },
                }
                json.dump(holdings_json, f)
                holdings_file = f.name

            try:
                # Generate synthesis report
                analyzer = PortfolioAnalyzer()
                report = analyzer.generate_report(holdings_file, None)

                # Extract data
                report_data = report.get("data", report) if isinstance(report, dict) else {}

                return StageResult(
                    stage_name=self.stage_name,
                    status="success",
                    data=report_data,
                    _metadata={
                        "positions_count": len(portfolio.positions),
                        "p1_stages_completed": sum(
                            1 for r in p1_results.values() if r and r.status == "success"
                        ),
                    },
                )
            finally:
                # Clean up temp file
                import os

                try:
                    os.unlink(holdings_file)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Synthesis report generation failed: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )
