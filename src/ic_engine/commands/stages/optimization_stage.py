"""P3: Portfolio optimization stage."""

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


class OptimizationStage(PipelineStage):
    """P3: Rebalancing and optimization scenarios."""

    stage_name = "optimization"
    depends_on = ["holdings"]
    parallel_group = "P3"

    async def execute(self, context: PipelineContext) -> StageResult:
        """Generate optimization scenarios."""
        if not context.portfolio_data:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error="No portfolio_data from holdings stage",
            )

        try:
            result = await asyncio.to_thread(
                self._run_optimization, context.portfolio_data, context.cdm_version
            )
            return result
        except Exception as e:
            logger.error(f"Optimization failed: {e}")
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )

    def _run_optimization(self, portfolio, cdm_version: str) -> StageResult:
        """Run portfolio optimization via optimize.py functions."""
        try:
            from commands.optimize import (
                fetch_historical_returns,
                load_holdings,
                optimize_sharpe_ratio,
            )

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
                                if p.asset_class == "equity"
                            ]
                        }
                    },
                }
                json.dump(holdings_json, f)
                holdings_file = f.name

            try:
                # Load holdings and fetch returns (temp JSON is already filtered to equities)
                equity_holdings, total_value = load_holdings(holdings_file)

                if len(equity_holdings) == 0:
                    return StageResult(
                        stage_name=self.stage_name,
                        status="skipped",
                        data={"message": "No equities in portfolio"},
                        _metadata={"equities_count": 0},
                    )

                symbols = equity_holdings["symbol"].tolist()
                returns = fetch_historical_returns(symbols)

                # Run optimization
                result = optimize_sharpe_ratio(equity_holdings, returns)

                return StageResult(
                    stage_name=self.stage_name,
                    status="success",
                    data=result if isinstance(result, dict) else {"optimization": result},
                    _metadata={
                        "equities_analyzed": len(symbols),
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
            logger.error(f"Portfolio optimization failed: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )
