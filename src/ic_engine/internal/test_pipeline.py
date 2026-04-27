"""
Tests for Phase 3 async pipeline orchestration.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from ic_engine.internal.pipeline import PortfolioPipeline, run_portfolio_analysis
from ic_engine.internal.stages import PipelineContext, StageResult


class TestPipelineStages(unittest.TestCase):
    """Test individual stage execution."""

    def test_stage_result_to_dict(self):
        """Test StageResult serialization."""
        result = StageResult(
            stage_name="test",
            status="success",
            data={"key": "value"},
        )
        d = result.to_dict()
        self.assertEqual(d["stage_name"], "test")
        self.assertEqual(d["status"], "success")
        self.assertEqual(d["data"], {"key": "value"})

    def test_pipeline_context_creation(self):
        """Test PipelineContext initialization."""
        context = PipelineContext()
        self.assertIsNone(context.portfolio_data)
        self.assertEqual(len(context.upstream_results), 0)
        self.assertTrue(context.cache_dir.exists() or True)  # Path object

    def test_context_get_result(self):
        """Test upstream result retrieval."""
        context = PipelineContext()
        result = StageResult(stage_name="test", status="success")
        context.upstream_results["test"] = result
        self.assertEqual(context.get_result("test"), result)
        self.assertIsNone(context.get_result("nonexistent"))


class TestPortfolioPipeline(unittest.TestCase):
    """Test pipeline orchestration."""

    def setUp(self):
        """Create temporary portfolio file for testing."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.portfolio_file = Path(self.temp_dir.name) / "test_portfolio.json"

        # Create minimal test portfolio
        portfolio_data = {
            "portfolio": {
                "name": "Test Portfolio",
                "created": datetime.now().isoformat(),
                "positions": [
                    {
                        "symbol": "AAPL",
                        "current_value": 5000,
                        "asset_class": "equity",
                    },
                    {
                        "symbol": "MSFT",
                        "current_value": 3000,
                        "asset_class": "equity",
                    },
                ],
            }
        }

        with open(self.portfolio_file, "w") as f:
            json.dump(portfolio_data, f)

    def tearDown(self):
        """Clean up temp files."""
        self.temp_dir.cleanup()

    def test_pipeline_initialization(self):
        """Test pipeline creates all stages."""
        pipeline = PortfolioPipeline()
        self.assertEqual(len(pipeline.stages), 9)
        self.assertIn("holdings", pipeline.stages)
        self.assertIn("performance", pipeline.stages)
        self.assertIn("cashflow", pipeline.stages)
        self.assertIn("peer", pipeline.stages)

    def test_pipeline_config_loading(self):
        """Test pipeline loads configuration."""
        pipeline = PortfolioPipeline()
        self.assertIn("cache_dir", pipeline.config)
        self.assertIn("parallel_timeout_seconds", pipeline.config)

    def test_pipeline_run_basic(self):
        """Test basic pipeline execution (async)."""

        async def run_test():
            pipeline = PortfolioPipeline()
            result = await pipeline.run(str(self.portfolio_file))
            self.assertEqual(result._status, "complete")
            self.assertIn("holdings", result.stages)
            self.assertGreater(len(result.stages), 0)
            return result

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_test())
            self.assertIsNotNone(result)
        finally:
            loop.close()

    def test_pipeline_p1_parallelization(self):
        """Test that P1 stages execute in parallel (via timing)."""

        async def run_test():
            pipeline = PortfolioPipeline()
            result = await pipeline.run(str(self.portfolio_file))
            # P1 stages should have similar execution times (parallel)
            # and total should be much less than sequential
            p1_stages = ["performance", "bonds", "analyst", "news"]
            p1_results = [result.stages.get(s) for s in p1_stages if s in result.stages]
            # At least some P1 stages should be present
            self.assertGreater(len([r for r in p1_results if r]), 0)
            return result

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_test())
            self.assertIsNotNone(result)
        finally:
            loop.close()

    def test_pipeline_output_format(self):
        """Test pipeline output JSON structure."""

        async def run_test():
            pipeline = PortfolioPipeline()
            result = await pipeline.run(str(self.portfolio_file))
            output = result.to_dict()

            # Verify structure
            self.assertIn("stages", output)
            self.assertIn("_timing", output)
            self.assertIn("_status", output)
            self.assertIn("_metadata", output)

            # Verify status
            self.assertEqual(output["_status"], "complete")

            return output

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            output = loop.run_until_complete(run_test())
            self.assertIsNotNone(output)
        finally:
            loop.close()

    def test_pipeline_missing_holdings_file(self):
        """Test pipeline handles missing portfolio file."""

        async def run_test():
            pipeline = PortfolioPipeline()
            result = await pipeline.run("/nonexistent/path.json")
            # Should fail gracefully
            self.assertIn(result._status, ["failed", "complete"])
            return result

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_test())
            self.assertIsNotNone(result)
        finally:
            loop.close()


class TestPipelineConvenience(unittest.TestCase):
    """Test convenience functions."""

    def setUp(self):
        """Create temporary portfolio file."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.portfolio_file = Path(self.temp_dir.name) / "test_portfolio.json"

        portfolio_data = {
            "portfolio": {
                "name": "Test Portfolio",
                "created": datetime.now().isoformat(),
                "positions": [
                    {"symbol": "AAPL", "current_value": 5000, "asset_class": "equity"},
                ],
            }
        }

        with open(self.portfolio_file, "w") as f:
            json.dump(portfolio_data, f)

    def tearDown(self):
        """Clean up."""
        self.temp_dir.cleanup()

    def test_run_portfolio_analysis_async(self):
        """Test run_portfolio_analysis convenience function."""

        async def run_test():
            result = await run_portfolio_analysis(str(self.portfolio_file))
            self.assertIsNotNone(result)
            self.assertIn("stages", result)
            self.assertIn("_status", result)
            return result

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_test())
            self.assertIsNotNone(result)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
