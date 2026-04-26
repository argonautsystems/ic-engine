# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
ic-engine unified pipeline orchestrator.

Single entry point for full portfolio analysis: load → normalize →
validate → analyze → export. Invoked by the router via the `run` /
`pipeline` aliases (router.COMMANDS) which resolve to `../pipeline.py`
relative to commands/ — i.e. this file at the engine package root.

Ported from InvestorClaw v2.2.x's top-level pipeline.py during Phase 2 of
IC_DECOMPOSITION. Bare imports rewritten to package-qualified form so the
pipeline runs under both installed wheels and source-checkout PYTHONPATH.
"""

import json
from pathlib import Path

from ic_engine.commands.analyze_performance_polars import PerformanceAnalyzer
from ic_engine.commands.export_report import ReportExporter
from ic_engine.config.schema import normalize_portfolio, validate_portfolio
from ic_engine.services.portfolio_utils import load_holdings_list


def run_pipeline(holdings_file: str, output_dir: str = None):
    """Run full pipeline: load → normalize → validate → analyze → export"""

    holdings_path = Path(holdings_file).expanduser()
    if not holdings_path.exists():
        raise FileNotFoundError(f"Holdings file not found: {holdings_file}")

    # Auto-detect CSV input and convert to JSON via the canonical
    # PortfolioFetcher.main(input, output) entry point.
    if str(holdings_path).lower().endswith(".csv"):
        from ic_engine.commands.fetch_holdings import PortfolioFetcher

        holdings_path_json = Path(str(holdings_path).replace(".csv", ".json"))
        fetcher = PortfolioFetcher()
        fetcher.main(str(holdings_path), str(holdings_path_json))
        with open(holdings_path_json, "r") as f:
            raw = json.load(f)
        # Performance analyzer expects the JSON path; carry it forward.
        analyzer_input = holdings_path_json
    else:
        with open(holdings_path, "r") as f:
            raw = json.load(f)
        analyzer_input = holdings_path

    # Normalize + validate
    data = normalize_portfolio(raw)
    validate_portfolio(data)

    # Load holdings list
    load_holdings_list(data)

    # Output paths
    output_dir = Path(output_dir or Path.home() / "portfolio_reports")
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_dir = output_dir / ".raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    holdings_cdm_out = raw_dir / "holdings.json"
    holdings_summary_out = raw_dir / "holdings_summary.json"
    performance_out = raw_dir / "performance.json"

    # Save CDM normalized holdings snapshot
    with open(holdings_cdm_out, "w") as f:
        json.dump(data, f, indent=2)

    # Also save compact summary for ReportExporter + Dashboard compatibility.
    # normalize_portfolio canonicalizes output under data["portfolio"]; legacy
    # callers wrote summary at the top level. Cover both shapes so the
    # exporter never sees a missing file.
    portfolio = data.get("portfolio", {}) if isinstance(data.get("portfolio"), dict) else {}
    summary_block = data.get("summary") or portfolio.get("summary")
    if summary_block is not None:
        compact_summary = {
            "summary": summary_block,
            "top_equity": data.get("top_equity") or portfolio.get("top_equity", []),
            "sector_breakdown": (
                data.get("sector_breakdown") or portfolio.get("sector_breakdown", {})
            ),
            "accounts": data.get("accounts") or portfolio.get("accounts", {}),
        }
        with open(holdings_summary_out, "w") as f:
            json.dump(compact_summary, f, indent=2)
    else:
        # No summary block — fall back to the full normalized snapshot so
        # ReportExporter has *some* data to read rather than a 404.
        with open(holdings_summary_out, "w") as f:
            json.dump(data, f, indent=2)

    # Run performance analysis on the JSON-shaped holdings (CSV inputs are
    # converted to JSON above so analyzer always receives a JSON path).
    analyzer = PerformanceAnalyzer()
    analyzer.analyze_portfolio(str(analyzer_input), str(performance_out))

    # Export reports (use compact schema, not CDM)
    exporter = ReportExporter()
    exporter.load_data(str(holdings_summary_out), str(performance_out))
    exporter.export_to_csv(str(output_dir / "portfolio_report"))

    return {
        "normalized_holdings": str(holdings_cdm_out),
        "performance": str(performance_out),
        "reports_dir": str(output_dir),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m ic_engine.pipeline <holdings.json>")
        exit(1)

    result = run_pipeline(sys.argv[1])

    print("\nPipeline complete:")
    for k, v in result.items():
        print(f"  {k}: {v}")
