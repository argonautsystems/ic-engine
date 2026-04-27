"""P1+: Income and cashflow projection stage."""

import asyncio
import logging
import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from ic_engine.internal.stages import PipelineContext, PipelineStage, StageResult

logger = logging.getLogger(__name__)


class CashflowStage(PipelineStage):
    """Forward-looking dividend and coupon cashflow projection."""

    stage_name = "cashflow"
    depends_on = ["holdings"]
    parallel_group = "P1"

    async def execute(self, context: PipelineContext) -> StageResult:
        """Run the existing cashflow analyzer against the holdings file."""
        holdings_file = context.config.get("holdings_file")
        if not holdings_file:
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error="No holdings_file configured for cashflow stage",
            )

        try:
            result = await asyncio.to_thread(self._run_cashflow, str(holdings_file))
            return result
        except Exception as e:
            logger.error("Cashflow stage failed: %s", e)
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )

    def _run_cashflow(self, holdings_file: str) -> StageResult:
        try:
            from ic_engine.commands.cashflow import run_cashflow

            data = run_cashflow(
                holdings_file=holdings_file,
                bond_analysis_file=None,
                months=12,
                annual_expenses=0.0,
                output_file=None,
            )
            return StageResult(
                stage_name=self.stage_name,
                status="success",
                data=data,
                _metadata=data.get("positions_analyzed", {}) if isinstance(data, dict) else {},
            )
        except Exception as e:
            logger.error("Cashflow analysis failed: %s", e)
            return StageResult(
                stage_name=self.stage_name,
                status="failed",
                error=str(e),
            )

