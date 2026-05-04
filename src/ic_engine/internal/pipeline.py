"""
Phase 4: Async pipeline orchestrator with error handling and observability.
Coordinates execution of portfolio analysis stages with parallelization,
retries, and detailed metrics collection.
"""
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 InvestorClaw Contributors

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from ic_engine.commands.stages import (
    AnalystStage,
    BondsStage,
    CashflowStage,
    HoldingsStage,
    NewsStage,
    OptimizationStage,
    PeerAnalysisStage,
    PerformanceStage,
    SynthesisStage,
)
from ic_engine.internal.stages import PipelineContext, PipelineResult, PipelineStage, StageResult


@dataclass
class PipelineMetrics:
    """Detailed metrics for pipeline execution."""

    total_duration_ms: float = 0.0
    p0_duration_ms: float = 0.0
    p1_duration_ms: float = 0.0
    p2_duration_ms: float = 0.0
    p3_duration_ms: float = 0.0
    p4_duration_ms: float = 0.0
    stages_completed: int = 0
    stages_failed: int = 0
    stages_skipped: int = 0
    total_retries: int = 0
    stage_metrics: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            "total_duration_ms": self.total_duration_ms,
            "phase_durations": {
                "p0_ms": self.p0_duration_ms,
                "p1_ms": self.p1_duration_ms,
                "p2_ms": self.p2_duration_ms,
                "p3_ms": self.p3_duration_ms,
                "p4_ms": self.p4_duration_ms,
            },
            "stage_summary": {
                "completed": self.stages_completed,
                "failed": self.stages_failed,
                "skipped": self.stages_skipped,
            },
            "total_retries": self.total_retries,
            "stage_details": self.stage_metrics,
        }


class PortfolioPipeline:
    """Async orchestrator for portfolio analysis stages."""

    def __init__(self, config_path: Optional[Path] = None, cache_dir: Optional[Path] = None):
        """Initialize pipeline with stages."""
        self.cache_dir = cache_dir or Path(".cache/portfolio")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.stages: Dict[str, PipelineStage] = {
            # P0: Input
            "holdings": HoldingsStage(),
            # P1: Parallel analysis
            "performance": PerformanceStage(),
            "bonds": BondsStage(),
            "analyst": AnalystStage(),
            "news": NewsStage(),
            # P2: Synthesis
            "synthesis": SynthesisStage(),
            # P3: Optimization
            "optimization": OptimizationStage(),
            # P3b: Cashflow
            "cashflow": CashflowStage(),
            # P4: Peer analysis
            "peer": PeerAnalysisStage(),
        }

        # Load config
        self.config = self._load_config(config_path)
        self.metrics = PipelineMetrics()

    def _load_config(self, config_path: Optional[Path]) -> Dict:
        """Load pipeline configuration."""
        if config_path and config_path.exists():
            import json

            with open(config_path) as f:
                return json.load(f)
        return {
            "cache_dir": str(self.cache_dir),
            # P1 parallel-stage timeout. Was 60s; bumped 2026-05-03 because
            # 200+ position portfolios overflowed it — asyncio.gather raised
            # TimeoutError, ALL P1 results were lost, the envelope marked
            # every section "did not run" even though individual stages
            # (e.g. analyst at 17s) had completed. The narrator then refused
            # to answer because the envelope was sparse, which masqueraded
            # as a routing bug for weeks. Per-stage retry handles transient
            # failures within this budget.
            "parallel_timeout_seconds": 600,
            "max_workers": 4,
            "max_retries": 2,
            "retry_delay_seconds": 1,
        }

    async def _execute_with_retry(
        self,
        stage_name: str,
        execute_fn: Callable,
        max_retries: int = 2,
        retry_delay: float = 1.0,
    ) -> StageResult:
        """Execute a stage with retry logic for transient failures."""
        retries = 0
        last_error = None

        while retries <= max_retries:
            try:
                logger.info(f"{stage_name}: attempt {retries + 1}/{max_retries + 1}")
                result = await execute_fn()

                if result.status == "success":
                    return result

                # Check if error is retryable (temporary API issues, timeouts, etc.)
                if self._is_retryable_error(result.error) and retries < max_retries:
                    last_error = result.error
                    retries += 1
                    self.metrics.total_retries += 1
                    logger.warning(
                        f"{stage_name}: retryable error, attempt {retries + 1}/"
                        f"{max_retries + 1} in {retry_delay}s: {result.error}"
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    return result
            except asyncio.TimeoutError as e:
                last_error = str(e)
                if retries < max_retries:
                    retries += 1
                    self.metrics.total_retries += 1
                    logger.warning(
                        f"{stage_name}: timeout, attempt {retries + 1}/"
                        f"{max_retries + 1} in {retry_delay}s"
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    return StageResult(
                        stage_name=stage_name,
                        status="failed",
                        error=f"Timeout after {max_retries} retries: {last_error}",
                    )
            except Exception as e:
                last_error = str(e)
                logger.error(f"{stage_name}: unexpected error: {e}")
                return StageResult(
                    stage_name=stage_name,
                    status="failed",
                    error=str(e),
                )

        return StageResult(
            stage_name=stage_name,
            status="failed",
            error=f"Failed after {max_retries} retries: {last_error}",
        )

    @staticmethod
    def _is_retryable_error(error_msg: Optional[str]) -> bool:
        """Determine if an error is retryable (transient vs permanent)."""
        if not error_msg:
            return False

        retryable_patterns = [
            "timeout",
            "connection",
            "temporarily unavailable",
            "rate limit",
            "503",
            "429",
            "temporarily",
        ]

        error_lower = error_msg.lower()
        return any(pattern in error_lower for pattern in retryable_patterns)

    async def run(self, holdings_file: str) -> PipelineResult:
        """
        Execute full pipeline with async parallelization.

        Flow:
        - P0: HoldingsLoader (serial)
        - P1: PerformanceStage, BondsStage, AnalystStage, NewsStage (parallel)
        - P2: SynthesisStage (serial, depends on P1)
        - P3: OptimizationStage (serial)
        - P4: PeerAnalysisStage (serial)

        Returns: PipelineResult with all stage outputs
        """
        start_time = time.time()
        context = PipelineContext(
            portfolio_data=None,
            upstream_results={},
            config=self.config,
            cache_dir=self.cache_dir,
        )

        # P0: Load holdings (prerequisite, with retry)
        logger.info("Pipeline: Starting P0 (holdings load)")
        holdings_stage = self.stages["holdings"]
        holdings_stage.holdings_file = holdings_file

        p0_start = time.time()
        max_retries = self.config.get("max_retries", 2)
        retry_delay = self.config.get("retry_delay_seconds", 1.0)

        p0_result = await self._execute_with_retry(
            "holdings",
            lambda: holdings_stage.execute(context),
            max_retries=max_retries,
            retry_delay=retry_delay,
        )

        p0_elapsed = (time.time() - p0_start) * 1000
        self.metrics.p0_duration_ms = p0_elapsed
        self.metrics.stage_metrics["holdings"] = {
            "status": p0_result.status,
            "duration_ms": p0_elapsed,
        }

        p0_result._timing["execution_ms"] = p0_elapsed
        context.upstream_results["holdings"] = p0_result

        if p0_result.status != "success":
            self.metrics.stages_failed += 1
            logger.error(f"Pipeline failed at P0 (holdings): {p0_result.error}")
            return PipelineResult(
                stages=context.upstream_results,
                _timing={"total_ms": (time.time() - start_time) * 1000},
                _status="failed",
                _metadata={
                    "failed_stage": "holdings",
                    "error": p0_result.error,
                    "metrics": self.metrics.to_dict(),
                },
            )

        # Load portfolio data into context for downstream stages
        # (In real implementation, would restore from p0_result.data)
        try:
            from ic_engine.internal.holdings_loader import HoldingsLoader

            loader = HoldingsLoader()
            context.portfolio_data = loader.load(holdings_file)
        except Exception as e:
            return PipelineResult(
                stages=context.upstream_results,
                _timing={"total_ms": (time.time() - start_time) * 1000},
                _status="failed",
                _metadata={"error": str(e)},
            )

        # P1: Run analysis stages in parallel with retry
        logger.info("Pipeline: Starting P1 (parallel analysis)")
        p1_start = time.time()

        async def execute_p1_stage(stage_name: str):
            stage = self.stages[stage_name]
            s_start = time.time()
            result = await self._execute_with_retry(
                stage_name,
                lambda: stage.execute(context),
                max_retries=max_retries,
                retry_delay=retry_delay,
            )
            s_elapsed = (time.time() - s_start) * 1000
            result._timing["execution_ms"] = s_elapsed
            self.metrics.stage_metrics[stage_name] = {
                "status": result.status,
                "duration_ms": s_elapsed,
            }
            return result

        try:
            p1_results = await asyncio.wait_for(
                asyncio.gather(
                    execute_p1_stage("performance"),
                    execute_p1_stage("bonds"),
                    execute_p1_stage("analyst"),
                    execute_p1_stage("news"),
                ),
                timeout=self.config.get("parallel_timeout_seconds", 60),
            )
        except asyncio.TimeoutError:
            logger.error("Pipeline: P1 stages exceeded timeout")
            self.metrics.stages_failed += 1
            return PipelineResult(
                stages=context.upstream_results,
                _timing={"total_ms": (time.time() - start_time) * 1000},
                _status="failed",
                _metadata={
                    "error": "P1 stages exceeded timeout",
                    "metrics": self.metrics.to_dict(),
                },
            )

        p1_elapsed = (time.time() - p1_start) * 1000
        self.metrics.p1_duration_ms = p1_elapsed

        for result in p1_results:
            context.upstream_results[result.stage_name] = result
            if result.status == "success":
                self.metrics.stages_completed += 1
            elif result.status == "skipped":
                self.metrics.stages_skipped += 1
            else:
                self.metrics.stages_failed += 1
                logger.warning(f"P1 stage {result.stage_name} failed: {result.error}")

        logger.info(f"Pipeline: P1 complete ({p1_elapsed:.0f}ms)")

        # P2-P4: Sequential stages with retry and metrics
        async def execute_sequential_stage(stage_name: str):
            return await self.stages[stage_name].execute(context)

        phase_timings = {}
        for phase_num, stage_names in enumerate([["synthesis"], ["optimization"], ["peer"]], 2):
            phase_start = time.time()
            logger.info(f"Pipeline: Starting P{phase_num} ({', '.join(stage_names)})")

            for stage_name in stage_names:
                s_start = time.time()
                result = await self._execute_with_retry(
                    stage_name,
                    lambda sn=stage_name: execute_sequential_stage(sn),
                    max_retries=max_retries,
                    retry_delay=retry_delay,
                )
                s_elapsed = (time.time() - s_start) * 1000
                result._timing["execution_ms"] = s_elapsed
                context.upstream_results[stage_name] = result

                self.metrics.stage_metrics[stage_name] = {
                    "status": result.status,
                    "duration_ms": s_elapsed,
                }

                if result.status == "success":
                    self.metrics.stages_completed += 1
                elif result.status == "skipped":
                    self.metrics.stages_skipped += 1
                else:
                    self.metrics.stages_failed += 1
                    logger.warning(f"P{phase_num} stage {stage_name} failed: {result.error}")

            phase_elapsed = (time.time() - phase_start) * 1000
            phase_timings[f"p{phase_num}_ms"] = phase_elapsed
            if phase_num == 2:
                self.metrics.p2_duration_ms = phase_elapsed
            elif phase_num == 3:
                self.metrics.p3_duration_ms = phase_elapsed
            elif phase_num == 4:
                self.metrics.p4_duration_ms = phase_elapsed

            logger.info(f"Pipeline: P{phase_num} complete ({phase_elapsed:.0f}ms)")

        total_elapsed = (time.time() - start_time) * 1000
        self.metrics.total_duration_ms = total_elapsed

        logger.info(
            f"Pipeline complete: {self.metrics.stages_completed} success, "
            f"{self.metrics.stages_failed} failed, {self.metrics.stages_skipped} skipped "
            f"({total_elapsed:.0f}ms total)"
        )

        return PipelineResult(
            stages=context.upstream_results,
            _timing={
                "p0_ms": self.metrics.p0_duration_ms,
                "p1_ms": self.metrics.p1_duration_ms,
                "p2_ms": self.metrics.p2_duration_ms,
                "p3_ms": self.metrics.p3_duration_ms,
                "p4_ms": self.metrics.p4_duration_ms,
                "total_ms": total_elapsed,
            },
            _status="complete",
            _metadata={
                "stages_completed": self.metrics.stages_completed,
                "stages_failed": self.metrics.stages_failed,
                "stages_skipped": self.metrics.stages_skipped,
                "total_retries": self.metrics.total_retries,
                "pipeline_duration_seconds": total_elapsed / 1000,
                "detailed_metrics": self.metrics.to_dict(),
            },
        )

    async def run_full(self, holdings_file: str) -> PipelineResult:
        """
        Execute the v2.5 deterministic-first full pipeline.

        Holdings is still the prerequisite load step; every downstream
        section then fans out concurrently so the narrator receives one
        complete envelope instead of routing the user's question to a single
        command.
        """
        start_time = time.time()
        self.metrics = PipelineMetrics()
        config = dict(self.config)
        config["holdings_file"] = holdings_file
        context = PipelineContext(
            portfolio_data=None,
            upstream_results={},
            config=config,
            cache_dir=self.cache_dir,
        )

        max_retries = config.get("max_retries", 2)
        retry_delay = config.get("retry_delay_seconds", 1.0)

        logger.info("Full pipeline: starting holdings load")
        holdings_stage = self.stages["holdings"]
        holdings_stage.holdings_file = holdings_file

        p0_start = time.time()
        p0_result = await self._execute_with_retry(
            "holdings",
            lambda: holdings_stage.execute(context),
            max_retries=max_retries,
            retry_delay=retry_delay,
        )
        p0_elapsed = (time.time() - p0_start) * 1000
        p0_result._timing["execution_ms"] = p0_elapsed
        self.metrics.p0_duration_ms = p0_elapsed
        self.metrics.stage_metrics["holdings"] = {
            "status": p0_result.status,
            "duration_ms": p0_elapsed,
        }
        context.upstream_results["holdings"] = p0_result

        if p0_result.status != "success":
            self.metrics.stages_failed += 1
            return PipelineResult(
                stages=context.upstream_results,
                _timing={"total_ms": (time.time() - start_time) * 1000},
                _status="failed",
                _metadata={
                    "failed_stage": "holdings",
                    "error": p0_result.error,
                    "metrics": self.metrics.to_dict(),
                    "full_pipeline": True,
                },
            )

        self.metrics.stages_completed += 1

        try:
            from ic_engine.internal.holdings_loader import HoldingsLoader

            context.portfolio_data = HoldingsLoader().load(holdings_file)
        except Exception as e:
            self.metrics.stages_failed += 1
            return PipelineResult(
                stages=context.upstream_results,
                _timing={"total_ms": (time.time() - start_time) * 1000},
                _status="failed",
                _metadata={
                    "failed_stage": "holdings",
                    "error": str(e),
                    "metrics": self.metrics.to_dict(),
                    "full_pipeline": True,
                },
            )

        fanout_stages = [
            "performance",
            "bonds",
            "analyst",
            "news",
            "synthesis",
            "optimization",
            "cashflow",
            "peer",
        ]
        fanout_start = time.time()

        async def execute_full_stage(stage_name: str) -> StageResult:
            stage = self.stages[stage_name]
            s_start = time.time()
            result = await self._execute_with_retry(
                stage_name,
                lambda: stage.execute(context),
                max_retries=max_retries,
                retry_delay=retry_delay,
            )
            s_elapsed = (time.time() - s_start) * 1000
            result._timing["execution_ms"] = s_elapsed
            self.metrics.stage_metrics[stage_name] = {
                "status": result.status,
                "duration_ms": s_elapsed,
            }
            return result

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(execute_full_stage(stage_name) for stage_name in fanout_stages)),
                timeout=config.get("parallel_timeout_seconds", 60),
            )
        except asyncio.TimeoutError:
            self.metrics.stages_failed += 1
            return PipelineResult(
                stages=context.upstream_results,
                _timing={"total_ms": (time.time() - start_time) * 1000},
                _status="failed",
                _metadata={
                    "error": "Full pipeline sections exceeded timeout",
                    "metrics": self.metrics.to_dict(),
                    "full_pipeline": True,
                },
            )

        fanout_elapsed = (time.time() - fanout_start) * 1000
        self.metrics.p1_duration_ms = fanout_elapsed

        for result in results:
            context.upstream_results[result.stage_name] = result
            if result.status == "success":
                self.metrics.stages_completed += 1
            elif result.status == "skipped":
                self.metrics.stages_skipped += 1
            else:
                self.metrics.stages_failed += 1
                logger.warning("Full pipeline stage %s failed: %s", result.stage_name, result.error)

        total_elapsed = (time.time() - start_time) * 1000
        self.metrics.total_duration_ms = total_elapsed

        return PipelineResult(
            stages=context.upstream_results,
            _timing={
                "p0_ms": self.metrics.p0_duration_ms,
                "fanout_ms": fanout_elapsed,
                "total_ms": total_elapsed,
            },
            _status="complete",
            _metadata={
                "stages_completed": self.metrics.stages_completed,
                "stages_failed": self.metrics.stages_failed,
                "stages_skipped": self.metrics.stages_skipped,
                "total_retries": self.metrics.total_retries,
                "pipeline_duration_seconds": total_elapsed / 1000,
                "detailed_metrics": self.metrics.to_dict(),
                "full_pipeline": True,
                "parallel_sections": fanout_stages,
            },
        )


async def run_portfolio_analysis(holdings_file: str, config_path: Optional[Path] = None) -> Dict:
    """Convenience function to run complete portfolio analysis."""
    pipeline = PortfolioPipeline(config_path=config_path)
    result = await pipeline.run(holdings_file)
    return result.to_dict()
