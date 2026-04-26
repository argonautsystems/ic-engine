"""P1a: Performance analysis stage with Phase 2 data pipeline integration."""

import asyncio
import json
import logging
import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from ic_engine.internal.stages import PipelineContext, PipelineStage, StageResult

logger = logging.getLogger(__name__)


class PerformanceStage(PipelineStage):
    """P1a: Performance analysis - runs in parallel with other P1 stages."""

    stage_name = "performance"
    depends_on = ["holdings"]
    parallel_group = "P1"

    async def execute(self, context: PipelineContext) -> StageResult:
        """Analyze portfolio performance using existing analyzer and Phase 2 data pipeline."""
        if not context.portfolio_data:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error="No portfolio_data from holdings stage",
            )

        try:
            portfolio = context.portfolio_data

            # Extract equity symbols
            symbols = await asyncio.to_thread(
                lambda: [p.symbol for p in portfolio.positions if p.asset_class == "equity"]
            )

            if not symbols:
                return StageResult(
                    stage_name=self.stage_name,
                    status="skipped",
                    data={"message": "No equities in portfolio"},
                    _metadata={"symbols_count": 0},
                )

            # Run performance analysis
            result = await asyncio.to_thread(
                self._run_performance_analysis, portfolio, symbols, context.cdm_version
            )

            return result

        except Exception as e:
            logger.error(f"Performance stage failed: {e}")
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )

    def _run_performance_analysis(self, portfolio, symbols: list, cdm_version: str) -> StageResult:
        """Run the actual performance analysis using existing infrastructure."""
        try:
            # Import here to avoid circular dependencies
            from ic_engine.commands.analyze_performance_polars import PerformanceAnalyzer

            # Run analyzer
            analyzer = PerformanceAnalyzer()

            # Create a temporary holdings file in CDM format
            import tempfile

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
                                        "assetClass": "Stocks",
                                        "securityType": "Equity",
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
                                        "costBasisPrice": {
                                            "amount": (p.cost_basis / (p.shares or 1.0))
                                            if p.cost_basis and p.shares
                                            else 0.0,
                                            "currency": "USD",
                                        },
                                    },
                                    "marketValue": p.market_value or 0.0,
                                    "costBasis": p.cost_basis
                                    if hasattr(p, "cost_basis")
                                    else p.market_value,
                                }
                                for p in portfolio.positions
                                if p.asset_class == "equity"
                            ]
                        }
                    },
                }
                json.dump(holdings_json, f)
                holdings_file = f.name

            try:
                # Run analysis with 12-month lookback
                report = analyzer.analyze_portfolio(holdings_file, None, "12m")

                # Extract metrics
                report_data = report.get("data", report) if isinstance(report, dict) else {}

                return StageResult(
                    stage_name=self.stage_name,
                    status="success",
                    data=report_data,
                    _metadata={
                        "symbols_analyzed": len(symbols),
                        "holdings_count": sum(
                            1 for p in portfolio.positions if p.asset_class == "equity"
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
            logger.error(f"Performance analysis failed: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )
