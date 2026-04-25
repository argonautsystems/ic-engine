"""P1c: Analyst recommendations stage."""

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


class AnalystStage(PipelineStage):
    """P1c: Analyst consensus - runs in parallel with other P1 stages."""

    stage_name = "analyst"
    depends_on = ["holdings"]
    parallel_group = "P1"

    async def execute(self, context: PipelineContext) -> StageResult:
        """Fetch analyst recommendations for portfolio."""
        if not context.portfolio_data:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error="No portfolio_data from holdings stage",
            )

        try:
            symbols = await asyncio.to_thread(
                lambda: [
                    p.symbol for p in context.portfolio_data.positions if p.asset_class == "equity"
                ]
            )

            if not symbols:
                return StageResult(
                    stage_name=self.stage_name,
                    status="skipped",
                    data={"message": "No equities in portfolio"},
                    _metadata={"symbols_count": 0},
                )

            # Fetch analyst recommendations
            result = await asyncio.to_thread(
                self._fetch_analyst_recommendations,
                context.portfolio_data,
                symbols,
                context.cdm_version,
            )
            return result
        except Exception as e:
            logger.error(f"Analyst fetch failed: {e}")
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )

    def _fetch_analyst_recommendations(self, portfolio, symbols, cdm_version: str) -> StageResult:
        """Fetch analyst recommendations using existing infrastructure."""
        try:
            from commands.fetch_analyst_recommendations_parallel import fetch_analyst_for_holdings

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
                # Fetch recommendations
                recommendations = fetch_analyst_for_holdings(holdings_file, verbose=False)

                # Convert to dict format
                analyst_data = {}
                for symbol, consensus in recommendations.items():
                    analyst_data[symbol] = {
                        "symbol": symbol,
                        "consensus": consensus.__dict__
                        if hasattr(consensus, "__dict__")
                        else consensus,
                    }

                return StageResult(
                    stage_name=self.stage_name,
                    status="success",
                    data=analyst_data,
                    _metadata={"symbols_analyzed": len(recommendations)},
                )
            finally:
                # Clean up temp file
                import os

                try:
                    os.unlink(holdings_file)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Analyst recommendations failed: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )
