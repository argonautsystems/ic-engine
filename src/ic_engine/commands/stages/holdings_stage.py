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
        """Load holdings from file and return PortfolioData."""
        if not self.holdings_file:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error="No holdings_file provided",
            )

        try:
            portfolio_data = await asyncio.to_thread(self.loader.load, self.holdings_file)
            return StageResult(
                stage_name=self.stage_name,
                status="success",
                data=portfolio_data.to_dict() if hasattr(portfolio_data, "to_dict") else None,
                _timing={"execution_ms": 0},  # Timing added by orchestrator
                _metadata={"positions_count": len(portfolio_data.positions)},
            )
        except Exception as e:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )
