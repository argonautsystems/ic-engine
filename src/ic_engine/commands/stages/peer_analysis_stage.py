"""P4: Peer and factor analysis stage."""

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


class PeerAnalysisStage(PipelineStage):
    """P4: Peer and factor exposure analysis."""

    stage_name = "peer"
    depends_on = ["holdings"]
    parallel_group = "P4"

    async def execute(self, context: PipelineContext) -> StageResult:
        """Analyze peer and factor exposure."""
        if not context.portfolio_data:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error="No portfolio_data from holdings stage",
            )

        try:
            result = await asyncio.to_thread(
                self._run_peer_analysis, context.portfolio_data, context.cdm_version
            )
            return result
        except Exception as e:
            logger.error(f"Peer analysis failed: {e}")
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )

    def _run_peer_analysis(self, portfolio, cdm_version: str) -> StageResult:
        """Run peer and factor analysis."""
        try:
            from ic_engine.commands.peer_analysis import run_peer_analysis

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
                                    },
                                    "marketValue": p.market_value or 0.0,
                                    "costBasis": p.cost_basis
                                    if hasattr(p, "cost_basis")
                                    else p.market_value,
                                }
                                for p in portfolio.positions
                                if p.asset_class == "equity"  # Peer analysis only for equities
                            ]
                        }
                    },
                }
                json.dump(holdings_json, f)
                holdings_file = f.name

            try:
                # Run peer analysis
                result = run_peer_analysis(
                    holdings_file,
                    performance_file=None,
                    benchmark="SPY",
                    compare=["QQQ", "IVV", "AGG"],
                )

                return StageResult(
                    stage_name=self.stage_name,
                    status="success",
                    data=result if isinstance(result, dict) else {"peer_analysis": result},
                    _metadata={
                        "equities_analyzed": sum(
                            1 for p in portfolio.positions if p.asset_class == "equity"
                        ),
                        "benchmarks": ["SPY", "QQQ", "IVV", "AGG"],
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
            logger.error(f"Peer analysis failed: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )
