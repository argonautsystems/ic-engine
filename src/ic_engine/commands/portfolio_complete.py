#!/usr/bin/env python3
"""
Complete portfolio analysis workflow.

Orchestrates full analysis across all dimensions:
- Holdings snapshot (positions, allocation, concentration)
- Performance metrics (returns, Sharpe, volatility, drawdown)
- Bond analytics (YTM, duration, ladder, convexity)
- Analyst consensus (ratings, price targets, recommendation changes)
- News sentiment (headlines, correlation, macro themes)
- Portfolio synthesis (multi-factor analysis, risks, opportunities)
- Optimization scenarios (rebalancing, tax-aware, efficient frontier)
- Peer/factor analysis (exposure vs benchmarks)

Returns: Unified comprehensive analysis with all dimensions.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

# Setup
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from ic_engine.runtime.portfolio_arg_parser import extract_portfolio_slug, resolve_portfolio_file

from ic_engine.config.path_resolver import get_reports_dir
from ic_engine.internal.performance_timer import get_timer
from ic_engine.internal.pipeline import PortfolioPipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def run_command(command_name: str, holdings_file: str, reports_dir: Path) -> Optional[Dict]:
    """
    Run a single analysis command via subprocess and return parsed result from output file.

    Args:
        command_name: Name of command (performance, bonds, analyst, news, synthesize, optimize, peer)
        holdings_file: Path to holdings JSON file
        reports_dir: Directory where output files are written

    Returns: Parsed output from command's output file, or None if failed
    """
    import subprocess

    try:
        timer = get_timer()

        # Map command to script, arguments, and expected output file
        commands_map = {
            "performance": ("analyze_performance_polars.py", [holdings_file], "performance.json"),
            "bonds": ("bond_analyzer.py", [holdings_file], "holdings_bond_analysis.json"),
            "analyst": (
                "fetch_analyst_recommendations_parallel.py",
                [holdings_file],
                "analyst_data.json",
            ),
            "news": ("fetch_portfolio_news.py", [holdings_file], "portfolio_news.json"),
            "synthesize": ("portfolio_analyzer.py", [holdings_file], "portfolio_analysis.json"),
            "optimize": ("optimize.py", [holdings_file], "optimization_results.json"),
            "peer": ("peer_analysis.py", [holdings_file], "peer_analysis.json"),
        }

        if command_name not in commands_map:
            logger.warning(f"Unknown command: {command_name}")
            return None

        script_name, args, output_file = commands_map[command_name]
        script_path = Path(__file__).parent / script_name
        output_path = reports_dir / output_file

        with timer.measure(f"run_{command_name}"):
            # Setup environment with proper PYTHONPATH
            env = os.environ.copy()
            project_root = str(Path(__file__).parent.parent)
            env["PYTHONPATH"] = project_root + ":" + env.get("PYTHONPATH", "")

            result = subprocess.run(
                [sys.executable, str(script_path)] + args,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=project_root,
                env=env,
            )

        if result.returncode != 0:
            logger.warning(f"{command_name} script exited with code {result.returncode}")
            if result.stderr:
                logger.debug(f"stderr: {result.stderr[:500]}")
            return None

        # Read output from file (each command writes its own output)
        if output_path.exists():
            try:
                with open(output_path) as f:
                    return json.load(f)
            except json.JSONDecodeError as e:
                logger.warning(f"{command_name}: failed to parse output file {output_path}: {e}")
                return None
        else:
            logger.warning(f"{command_name}: output file not found: {output_path}")
            return None

    except subprocess.TimeoutExpired:
        logger.warning(f"{command_name} command timed out")
        return None
    except Exception as e:
        logger.warning(f"{command_name} command failed: {e}")
        import traceback

        logger.debug(traceback.format_exc())
        return None


async def run_complete_analysis_async(
    portfolio_input: str, portfolio_slug: Optional[str] = None
) -> Dict:
    """
    Run complete portfolio analysis using async pipeline (Phase 3).

    Converts portfolio_input to holdings JSON, then orchestrates parallel analysis stages.
    Returns comprehensive result with all analyses organized by dimension.
    """
    import subprocess

    timer = get_timer()
    analysis_start = datetime.utcnow().isoformat()

    # Step 0: Convert portfolio input to holdings JSON if needed
    reports_dir = get_reports_dir()
    holdings_file = reports_dir / "holdings.json"

    if portfolio_input.endswith(".json"):
        holdings_file = portfolio_input
        logger.info(f"Using existing holdings file: {holdings_file}")
    else:
        logger.info("Converting portfolio input to holdings JSON...")
        try:
            with timer.measure("convert_to_holdings"):
                env = os.environ.copy()
                project_root = str(Path(__file__).parent.parent)
                env["PYTHONPATH"] = project_root + ":" + env.get("PYTHONPATH", "")

                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).parent / "fetch_holdings.py"),
                        portfolio_input,
                        str(holdings_file),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=project_root,
                    env=env,
                )
            if result.returncode != 0:
                logger.error(f"Failed to convert portfolio: {result.stderr}")
                return {
                    "error": "Failed to convert portfolio input to holdings JSON",
                    "details": result.stderr,
                }
        except Exception as e:
            logger.error(f"Conversion failed: {e}")
            return {"error": f"Portfolio conversion failed: {e}"}

    result = {
        "portfolio": {
            "input": str(portfolio_input),
            "holdings_json": str(holdings_file),
            "slug": portfolio_slug,
            "analyzed_at": analysis_start,
        },
        "dimensions": {},
        "_timing": {},
        "_status": "in_progress",
    }

    logger.info("Starting complete portfolio analysis with Phase 3 async pipeline")

    try:
        # Run async pipeline
        with timer.measure("async_pipeline"):
            pipeline = PortfolioPipeline()
            pipeline_result = await pipeline.run(str(holdings_file))

        # Extract results from pipeline stages
        pipeline_dict = pipeline_result.to_dict()
        stage_map = {
            "performance": "performance",
            "bonds": "bonds",
            "analyst": "analyst",
            "news": "news",
            "synthesis": "synthesize",
            "optimization": "optimize",
            "peer": "peer",
        }

        for pipeline_stage, dimension_name in stage_map.items():
            if pipeline_stage in pipeline_dict.get("stages", {}):
                stage_result = pipeline_dict["stages"][pipeline_stage]
                result["dimensions"][dimension_name] = stage_result.get("data", {})
                if stage_result.get("status") == "success":
                    logger.info(f"    ✓ {dimension_name} completed")
                else:
                    result["dimensions"][dimension_name] = {
                        "_error": f"Stage failed: {stage_result.get('error')}"
                    }
                    logger.info(f"    ✗ {dimension_name} failed")
            else:
                result["dimensions"][dimension_name] = {"_error": "Stage not executed"}
                logger.info(f"    ✗ {dimension_name} not executed")

        # Add pipeline timing
        result["_timing"] = timer.all_timers()
        result["_timing"]["pipeline"] = pipeline_dict.get("_timing", {})

    except Exception as e:
        logger.error(f"Pipeline execution failed: {e}")
        import traceback

        logger.debug(traceback.format_exc())
        result["_status"] = "failed"
        result["error"] = str(e)
        return result

    # Calculate completion stats
    completed = sum(1 for d in result["dimensions"].values() if "_error" not in d)
    total = len(result["dimensions"])
    result["_status"] = "complete"
    result["_completion"] = {
        "completed": completed,
        "total": total,
        "percentage": int((completed / total * 100)) if total > 0 else 0,
    }

    return result


def run_complete_analysis(portfolio_input: str, portfolio_slug: Optional[str] = None) -> Dict:
    """
    Synchronous wrapper for run_complete_analysis_async (backward compatible).
    """
    return asyncio.run(run_complete_analysis_async(portfolio_input, portfolio_slug))


def main():
    """Main entry point for complete portfolio analysis."""

    # Extract --portfolio-slug
    _argv = list(sys.argv)
    _portfolio_slug, _argv = extract_portfolio_slug(_argv)
    sys.argv = _argv

    # Resolve portfolio file
    try:
        portfolio_input = resolve_portfolio_file(
            explicit_slug=_portfolio_slug,
            input_file=None,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("\nUsage: portfolio_complete.py [--portfolio-slug slug]")
        print("\nExamples:")
        print("  portfolio_complete.py --portfolio-slug my-portfolio")
        print("  portfolio_complete.py  # Uses current session portfolio")
        sys.exit(1)

    # Run complete analysis
    try:
        result = run_complete_analysis(str(portfolio_input), _portfolio_slug)

        # Output comprehensive result
        print(json.dumps(result, indent=2))

        # Summary to stderr for user visibility
        completion = result.get("_completion", {})
        completed = completion.get("completed", 0)
        total = completion.get("total", 0)
        percentage = completion.get("percentage", 0)

        logger.info(f"\n{'=' * 70}")
        logger.info(f"PORTFOLIO ANALYSIS COMPLETE: {completed}/{total} analyses ({percentage}%)")
        logger.info(f"{'=' * 70}")

        # Save to file
        reports_dir = get_reports_dir()
        output_file = reports_dir / "complete_analysis.json"
        with open(output_file, "w") as f:
            json.dump(result, f, indent=2)
        logger.info(f"Full results saved to: {output_file}")

    except KeyboardInterrupt:
        logger.info("Analysis interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
