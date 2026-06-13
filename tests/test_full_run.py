from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ic_engine.internal.pipeline import PortfolioPipeline
from ic_engine.internal.stages import PipelineResult, StageResult
from ic_engine.runtime.envelope import CANONICAL_SECTIONS, validate_envelope
from ic_engine.runtime.full_run import envelope_from_pipeline_result


class _FakePosition:
    symbol = "AAPL"
    asset_class = "equity"
    shares = 1.0
    current_price = 100.0
    market_value = 100.0
    cost_basis = 90.0


class _FakePortfolio:
    positions = [_FakePosition()]


class _FakeHoldingsLoader:
    def load(self, holdings_file: str):
        del holdings_file
        return _FakePortfolio()


class _SleepStage:
    def __init__(self, stage_name: str, delay: float = 0.0, seen: list[str] | None = None):
        self.stage_name = stage_name
        self.delay = delay
        self.seen = seen
        self.holdings_file = None

    async def execute(self, context):
        if self.stage_name == "synthesis":
            required = {"performance", "bonds", "analyst", "news"}
            assert required.issubset(context.upstream_results)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.seen is not None:
            self.seen.append(self.stage_name)
        return StageResult(
            stage_name=self.stage_name,
            status="success",
            data={"stage": self.stage_name, "computed_at": datetime.now(timezone.utc).isoformat()},
        )


def _holdings_file(tmp_path: Path) -> Path:
    path = tmp_path / "holdings.json"
    path.write_text(json.dumps({"portfolio": {"positions": [{"symbol": "AAPL"}]}}))
    return path


def test_envelope_shape_contains_all_canonical_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "test-envelope-key")
    holdings_file = _holdings_file(tmp_path)
    stages = {
        "holdings": StageResult("holdings", "success", {"summary": {"total": "$100.00"}}),
        "performance": StageResult("performance", "success", {"return": "4.5%"}),
        "bonds": StageResult("bonds", "skipped", {"message": "No bonds"}),
        "analyst": StageResult("analyst", "success", {"AAPL": {"rating": "buy"}}),
        "news": StageResult("news", "success", {"headlines": []}),
        "synthesis": StageResult("synthesis", "success", {"risk": "moderate"}),
        "optimization": StageResult("optimization", "success", {"status": "ok"}),
        "cashflow": StageResult("cashflow", "success", {"annual_total": "$0.00"}),
        "peer": StageResult("peer", "success", {"benchmark": "SPY"}),
    }

    envelope = envelope_from_pipeline_result(PipelineResult(stages), holdings_file)

    validate_envelope(envelope)
    assert envelope["schema_version"] == "v2.5.0"
    assert set(envelope["sections"]) == set(CANONICAL_SECTIONS)
    assert "synthesize" in envelope["sections"]
    assert "optimize" in envelope["sections"]
    assert envelope["failed_sections"] == []
    assert envelope["ic_result"]["hmac"]


def test_portfolio_pipeline_run_full_fans_out_downstream_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "test-envelope-key")
    import ic_engine.internal.holdings_loader as holdings_loader_mod

    monkeypatch.setattr(holdings_loader_mod, "HoldingsLoader", _FakeHoldingsLoader)
    pipeline = PortfolioPipeline(cache_dir=tmp_path / "cache")
    seen: list[str] = []
    pipeline.stages = {
        "holdings": _SleepStage("holdings", seen=seen),
        "performance": _SleepStage("performance", 0.1, seen),
        "bonds": _SleepStage("bonds", 0.1, seen),
        "analyst": _SleepStage("analyst", 0.1, seen),
        "news": _SleepStage("news", 0.1, seen),
        "synthesis": _SleepStage("synthesis", 0.1, seen),
        "optimization": _SleepStage("optimization", 0.1, seen),
        "cashflow": _SleepStage("cashflow", 0.1, seen),
        "peer": _SleepStage("peer", 0.1, seen),
    }
    holdings_file = _holdings_file(tmp_path)

    started = time.perf_counter()
    result = asyncio.run(pipeline.run_full(str(holdings_file)))
    elapsed = time.perf_counter() - started

    assert result._status == "complete"
    assert set(result.stages) == set(pipeline.stages)
    assert elapsed < 0.45
    assert result._metadata["full_pipeline"] is True
    assert len(result._metadata["parallel_sections"]) == 7
    assert result._metadata["serial_sections"] == ["synthesis"]
    assert seen.index("synthesis") > max(
        seen.index(stage) for stage in ["performance", "bonds", "analyst", "news"]
    )

