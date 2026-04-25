"""
Phase 3: Pipeline stage base classes and context.
Provides abstract base for all portfolio analysis stages.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class StageResult:
    """Unified result envelope across all stages."""

    stage_name: str
    status: str  # "success" | "failed" | "skipped"
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    _timing: Dict[str, float] = field(default_factory=dict)  # elapsed_ms, etc.
    _metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """Serialize for JSON output."""
        return {
            "stage_name": self.stage_name,
            "status": self.status,
            "data": self.data,
            "error": self.error,
            "_timing": self._timing,
            "_metadata": self._metadata,
        }


@dataclass
class PipelineContext:
    """Shared context passed through all stages."""

    portfolio_data: Optional[Any] = None  # PortfolioData from Phase 1
    upstream_results: Dict[str, StageResult] = field(default_factory=dict)
    config: Dict = field(default_factory=dict)
    cache_dir: Path = field(default_factory=lambda: Path(".cache/portfolio"))
    cdm_version: str = "5.x"  # CDM version for this pipeline ("5.x" or "6.0")

    def get_result(self, stage_name: str) -> Optional[StageResult]:
        """Retrieve a prior stage result."""
        return self.upstream_results.get(stage_name)


class PipelineStage(ABC):
    """Base class for all portfolio analysis stages."""

    stage_name: str = "base"
    depends_on: List[str] = []
    parallel_group: Optional[str] = None  # "P0", "P1", "P2", etc.

    @abstractmethod
    async def execute(self, context: PipelineContext) -> StageResult:
        """Execute the stage. Must return StageResult."""
        pass

    def _time_block(self, func):
        """Decorator to time a code block."""
        start = time.time()
        result = func()
        elapsed_ms = (time.time() - start) * 1000
        return result, elapsed_ms


@dataclass
class PipelineResult:
    """Final result with all stage outputs."""

    stages: Dict[str, StageResult]
    _timing: Dict[str, float] = field(default_factory=dict)
    _status: str = "complete"
    _metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """Serialize to JSON."""
        return {
            "stages": {name: result.to_dict() for name, result in self.stages.items()},
            "_timing": self._timing,
            "_status": self._status,
            "_metadata": self._metadata,
        }
