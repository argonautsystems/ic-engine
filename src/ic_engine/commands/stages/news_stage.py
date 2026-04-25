"""P1d: News sentiment stage."""

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


class NewsStage(PipelineStage):
    """P1d: Portfolio news sentiment - runs in parallel with other P1 stages."""

    stage_name = "news"
    depends_on = ["holdings"]
    parallel_group = "P1"

    async def execute(self, context: PipelineContext) -> StageResult:
        """Fetch news and sentiment for portfolio."""
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

            # Fetch news
            result = await asyncio.to_thread(
                self._fetch_portfolio_news, context.portfolio_data, symbols, context.cdm_version
            )
            return result
        except Exception as e:
            logger.error(f"News fetch failed: {e}")
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )

    def _fetch_portfolio_news(self, portfolio, symbols, cdm_version: str) -> StageResult:
        """Fetch portfolio news using existing infrastructure."""
        try:
            from commands.fetch_portfolio_news import PortfolioNewsAnalyzer

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
                # Fetch news
                analyzer = PortfolioNewsAnalyzer()
                top_n = max(1, min(30, len(symbols)))  # Fetch top 30 or all symbols, min 1
                news_data = analyzer.fetch_all_news(
                    holdings_file, output_file=None, top_n=top_n, cache_file=None
                )

                return StageResult(
                    stage_name=self.stage_name,
                    status="success",
                    data=news_data if isinstance(news_data, dict) else {"news": news_data},
                    _metadata={"symbols_analyzed": len(symbols)},
                )
            finally:
                # Clean up temp file
                import os

                try:
                    os.unlink(holdings_file)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"News fetching failed: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )
