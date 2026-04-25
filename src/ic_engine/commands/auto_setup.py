#!/usr/bin/env python3
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
Auto-setup script for portfolio analyzer skill.
Runs on first initialization to:
1. Detect PDFs and XLS files in the portfolios directory
2. Extract tables using tabula/camelot
3. Convert XLS to CSV
4. Consolidate multiple files if found
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from ic_engine.rendering.progress import Phase, bootstrap, phase, update
from ic_engine.rendering.progress import error as report_error
from ic_engine.rendering.stonkmode import stonkmode_tip
from ic_engine.services.extract_pdf import PDFExtractor

# Redirect logging to avoid interfering with progress reporting
logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SKILL_DIR = Path(__file__).parent.parent

# Portfolio directory: delegate to the single source-of-truth resolver so env
# var naming, home-directory probing, and skill-dir fallback stay consistent
# across auto_setup.py, path_resolver.py, and the rest of the skill.
try:
    from config.path_resolver import get_portfolio_dir as _resolve_portfolio_dir

    PORTFOLIO_DIR = _resolve_portfolio_dir(SKILL_DIR)
except Exception:
    # Defensive fallback (kept minimal — should never fire in normal installs)
    _env = (
        os.environ.get("INVESTOR_CLAW_PORTFOLIO_DIR")
        or os.environ.get("INVESTORCLAW_PORTFOLIO_DIR")
        or ""
    ).strip()
    _candidates = [
        Path(_env).expanduser() if _env else None,
        Path.home() / "portfolios",
        SKILL_DIR / "portfolios",
    ]
    PORTFOLIO_DIR = next((p for p in _candidates if p and p.exists()), SKILL_DIR / "portfolios")

EXAMPLES_DIR = PORTFOLIO_DIR / "examples"
SETUP_MARKER = PORTFOLIO_DIR / ".setup_complete"
ENV_FILE = SKILL_DIR / ".env"
ENV_EXAMPLE = SKILL_DIR / ".env.example"

# API keys required for full functionality
_REQUIRED_KEYS = {
    "FINNHUB_KEY": ("Finnhub (real-time quotes & analyst ratings)", "https://finnhub.io/register"),
    "NEWSAPI_KEY": ("NewsAPI (news correlation)", "https://newsapi.org/register"),
    "MASSIVE_API_KEY": (
        "Massive (market data, polygon.io-compatible)",
        "https://polygon.io/dashboard/signup",
    ),
    "ALPHA_VANTAGE_KEY": (
        "Alpha Vantage (supplemental pricing)",
        "https://www.alphavantage.co/support/#api-key",
    ),
    "FRED_API_KEY": (
        "FRED / St. Louis Fed (Treasury & TIPS yields)",
        "https://fred.stlouisfed.org/docs/api/api_key.html",
    ),
}


def check_api_keys() -> Dict[str, bool]:
    """Check which API keys are configured in .env.

    Returns a dict mapping key name -> True (present) / False (missing).
    Prints guided instructions for any missing key.
    """
    # Load .env if it exists
    env_values: Dict[str, str] = {}
    if ENV_FILE.exists():
        with open(ENV_FILE) as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env_values[k.strip()] = v.strip()

    results: Dict[str, bool] = {}
    missing = []

    for key, (description, signup_url) in _REQUIRED_KEYS.items():
        value = os.environ.get(key) or env_values.get(key, "")
        present = bool(value)
        results[key] = present
        if not present:
            missing.append((key, description, signup_url))

    if not missing:
        update(f"✅ All {len(_REQUIRED_KEYS)} API keys configured")
        return results

    # Optional keys — yfinance works without them
    update("ℹ️  No API keys configured — using yfinance (free, unlimited)")
    update("   Optional: Add keys to .env for enhanced data")
    if ENV_EXAMPLE.exists():
        update(f"   Template: {ENV_EXAMPLE}")

    return results


def check_dependencies() -> Dict[str, bool]:
    """Check if required dependencies are available."""
    deps = {
        "pdfplumber": False,
        "tabula": False,
        "camelot": False,
        "pypdf2": False,
        "pdfminer": False,
        "openpyxl": False,
        "xlrd": False,
        "polars": False,
    }

    # Check PDF extraction tools (in priority order)
    try:
        import pdfplumber  # noqa: F401

        deps["pdfplumber"] = True
    except ImportError:
        logger.warning("pdfplumber not installed. Install with: pip install pdfplumber")

    try:
        import tabula  # noqa: F401

        deps["tabula"] = True
    except ImportError:
        logger.warning("tabula-py not installed. Install with: pip install tabula-py")

    try:
        import camelot  # noqa: F401

        deps["camelot"] = True
    except ImportError:
        logger.warning(
            "camelot-py not installed (best for complex tables). Install with: pip install camelot-py[cv]"
        )

    try:
        import pypdf  # noqa: F401

        deps["pypdf2"] = True
    except ImportError:
        logger.warning("pypdf not installed. Install with: pip install pypdf2")

    try:
        import pdfminer  # noqa: F401

        deps["pdfminer"] = True
    except ImportError:
        logger.warning("pdfminer not installed. Install with: pip install pdfminer.six")

    # Excel and data processing
    try:
        import openpyxl  # noqa: F401

        deps["openpyxl"] = True
    except ImportError:
        logger.warning("openpyxl not installed. Install with: pip install openpyxl")

    try:
        import xlrd  # noqa: F401

        deps["xlrd"] = True
    except ImportError:
        logger.warning(
            "xlrd not installed (needed for legacy .xls files). Install with: pip install xlrd"
        )

    try:
        import polars  # noqa: F401

        deps["polars"] = True
    except ImportError:
        logger.error("polars not installed. Install with: pip install polars")
        return deps

    return deps


def extract_pdf_tables(pdf_path: Path) -> List[Path]:
    """Extract tables from PDF and save as CSV files using PDFExtractor with fallback strategies."""
    csv_files = []

    try:
        # Use PDFExtractor with multiple strategy fallbacks
        extractor = PDFExtractor(pdf_path, timeout=30)
        result = extractor.extract()

        if result.get("status") == "success":
            update(f"  ✅ {result['tool']}: Extracted {result['count']} holdings")

            # Save holdings as CSV
            try:
                import csv

                output_file = PORTFOLIO_DIR / f"{pdf_path.stem}_extracted.csv"
                with open(output_file, "w") as f:
                    writer = csv.DictWriter(
                        f, fieldnames=["symbol", "shares", "price", "asset_type"]
                    )
                    writer.writeheader()
                    for holding in result["holdings"]:
                        holding["asset_type"] = "equity"  # Default
                        writer.writerow(holding)
                csv_files.append(output_file)
                logger.info(f"  Saved: {output_file.name}")
            except Exception as e:
                logger.error(f"Error saving CSV for {pdf_path.name}: {e}")

        elif result.get("status") == "partial":
            update("  ⚠️  Text extracted (manual parsing required)")
            logger.warning(f"Partial extraction for {pdf_path.name} - raw text extracted")
        else:
            update(f"  ❌ All extraction strategies failed for {pdf_path.name}")
            logger.warning(f"Could not extract tables from {pdf_path.name}: {result.get('error')}")

    except Exception as e:
        logger.error(f"Error processing PDF {pdf_path.name}: {e}")
        update(f"  ❌ PDF processing error: {str(e)}")

    return csv_files


def convert_xls_to_csv(xls_path: Path) -> List[Path]:
    """Convert XLS/XLSX file to CSV using appropriate library for file format.

    Detects file format by extension and uses:
    - xlrd for legacy .xls (Excel 97-2003)
    - openpyxl for modern .xlsx (Excel 2007+)
    - Polars for transparent reading

    Surfaces errors to user via progress reporting.
    """
    logger.info(f"Converting Excel file: {xls_path.name}")
    csv_files = []

    try:
        import polars as pl
    except ImportError:
        error_msg = (
            f"❌ Skipping {xls_path.name} - polars not installed. Install with: pip install polars"
        )
        logger.error(error_msg)
        report_error(error_msg)
        return csv_files

    # Detect file format by extension
    is_old_xls = xls_path.suffix.lower() == ".xls"

    try:
        # Get sheet names using the appropriate library
        sheet_names = None

        if is_old_xls:
            # Legacy Excel 97-2003 format
            try:
                import xlrd
            except ImportError:
                error_msg = f"❌ {xls_path.name} is .xls format but xlrd not installed. Install with: pip install xlrd"
                logger.error(error_msg)
                report_error(error_msg)
                return csv_files

            try:
                wb = xlrd.open_workbook(xls_path, on_demand=True)
                sheet_names = wb.sheet_names()
                wb.release_resources()
                logger.info(f"  Found {len(sheet_names)} sheets in legacy .xls file")
            except Exception as e:
                error_msg = (
                    f"❌ Cannot read legacy XLS file {xls_path.name}: {e}. File may be corrupted."
                )
                logger.error(error_msg)
                report_error(error_msg)
                return csv_files
        else:
            # Modern Excel 2007+ format
            try:
                import openpyxl
            except ImportError:
                error_msg = f"❌ {xls_path.name} is .xlsx format but openpyxl not installed. Install with: pip install openpyxl"
                logger.error(error_msg)
                report_error(error_msg)
                return csv_files

            try:
                wb = openpyxl.load_workbook(xls_path, read_only=True, data_only=True)
                sheet_names = wb.sheetnames
                wb.close()
                logger.info(f"  Found {len(sheet_names)} sheets in modern .xlsx file")
            except Exception as e:
                error_msg = f"❌ Cannot read XLSX file {xls_path.name}: {e}. File may be corrupted."
                logger.error(error_msg)
                report_error(error_msg)
                return csv_files

        # Process each sheet
        if not sheet_names:
            update(f"  ⚠️  No sheets found in {xls_path.name}")
            return csv_files

        for sheet_name in sheet_names:
            try:
                # Polars can handle both formats transparently
                df = pl.read_excel(xls_path, sheet_name=sheet_name)

                if df.is_empty():
                    logger.debug(f"Sheet '{sheet_name}' is empty, skipping")
                    continue

                # Check if this sheet has portfolio data (flexible column matching)
                cols_lower = [c.lower() for c in df.columns]
                portfolio_indicators = [
                    "symbol",
                    "ticker",
                    "security",
                    "shares",
                    "quantity",
                    "holdings",
                    "position",
                    "description",
                    "account",
                    "asset",
                    "value",
                    "price",
                    "cost",
                ]
                has_portfolio_data = any(col in cols_lower for col in portfolio_indicators)

                # Fallback: if sheet is the first sheet, assume it's portfolio data even if column names don't match exactly
                is_first_sheet = (
                    sheet_names.index(sheet_name) == 0 if sheet_name in sheet_names else False
                )
                if not has_portfolio_data and is_first_sheet and len(df) > 0:
                    logger.debug(
                        f"Sheet '{sheet_name}' has no standard portfolio columns, but is first sheet - attempting conversion anyway"
                    )
                    has_portfolio_data = True

                if has_portfolio_data:
                    output_file = PORTFOLIO_DIR / f"{xls_path.stem}_{sheet_name}.csv"
                    df.write_csv(output_file)
                    csv_files.append(output_file)
                    update(f"  ✅ Converted sheet '{sheet_name}' → {output_file.name}")
                    logger.info(f"  Saved: {output_file.name}")
            except Exception as sheet_error:
                error_msg = (
                    f"  ❌ Could not process sheet '{sheet_name}' in {xls_path.name}: {sheet_error}"
                )
                logger.error(error_msg)
                report_error(error_msg)

        if csv_files:
            update(
                f"  ✅ Successfully converted {xls_path.name} ({len(csv_files)} sheet{'s' if len(csv_files) != 1 else ''})"
            )
        else:
            update(f"  ⚠️  No portfolio data found in {xls_path.name}")

        return csv_files

    except Exception as e:
        error_msg = f"❌ Unexpected error converting {xls_path.name}: {e}"
        logger.error(error_msg)
        report_error(error_msg)
        return csv_files


def discover_and_convert_files() -> Dict[str, List[Path]]:
    """Discover PDFs, XLS files, and CSV files in portfolios directory and convert non-CSV formats.

    Searches PORTFOLIO_DIR (~/portfolios by default) for portfolio files:
    - CSV files: detected directly
    - Excel files (.xls, .xlsx): converted to CSV
    - PDF files: tables extracted to CSV

    Subdirectories are NOT scanned (e.g., examples/).
    Hidden files (starting with .) are skipped.
    """
    results = {
        "csv_files": [],
        "pdf_files": [],
        "xls_files": [],
        "converted_files": [],
    }

    # Find all files in portfolios directory (excluding subdirectories like examples/)
    PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Searching for portfolio files in: {PORTFOLIO_DIR}")

    all_files = list(PORTFOLIO_DIR.glob("*"))
    if not all_files:
        logger.warning(f"No files found in {PORTFOLIO_DIR}")
        return results

    for file in all_files:
        if file.is_dir():
            # Skip directories (including examples/)
            logger.debug(f"Skipping directory: {file.name}")
            continue
        if file.name.startswith("."):
            # Skip hidden files
            logger.debug(f"Skipping hidden file: {file.name}")
            continue

        if file.suffix.lower() == ".pdf":
            logger.debug(f"Found PDF: {file.name}")
            results["pdf_files"].append(file)
            csv_files = extract_pdf_tables(file)
            results["converted_files"].extend(csv_files)

        elif file.suffix.lower() in [".xls", ".xlsx"]:
            logger.debug(f"Found Excel file: {file.name}")
            results["xls_files"].append(file)
            csv_files = convert_xls_to_csv(file)
            results["converted_files"].extend(csv_files)

        elif file.suffix.lower() == ".csv":
            logger.debug(f"Found CSV: {file.name}")
            results["csv_files"].append(file)
        else:
            logger.debug(f"Skipping unsupported file type: {file.name}")

    logger.info(
        f"File discovery complete: {len(results['csv_files'])} CSVs, {len(results['xls_files'])} Excel, {len(results['pdf_files'])} PDFs, {len(results['converted_files'])} converted"
    )
    return results


def consolidate_portfolios(csv_files: List[Path]) -> Path:
    """Consolidate multiple CSV files into a master portfolio."""
    if not csv_files:
        return None

    if len(csv_files) == 1:
        return csv_files[0]

    logger.info(f"Consolidating {len(csv_files)} portfolio files...")

    try:
        # Use the consolidate_portfolios script if available
        consolidate_script = (
            Path(__file__).resolve().parent.parent / "services" / "consolidate_portfolios.py"
        )

        if consolidate_script.exists():
            cmd = [
                sys.executable,
                str(consolidate_script),
                "--input",
                ",".join(str(f) for f in csv_files),
                "--output",
                str(PORTFOLIO_DIR / "master_portfolio.csv"),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                master_file = PORTFOLIO_DIR / "master_portfolio.csv"
                logger.info(f"Consolidated master portfolio: {master_file.name}")
                return master_file

        return csv_files[0]

    except Exception as e:
        logger.error(f"Error consolidating portfolios: {e}")
        return csv_files[0]


def analyze_bonds_if_present(csv_files: List[Path]) -> Optional[Path]:
    """
    Automatically run bond analysis if bonds are detected in portfolio files.
    Returns path to bond_analysis.json if bonds were found and analyzed.
    """
    if not csv_files:
        return None

    try:
        from pathlib import Path

        import polars as pl

        # Check if any CSV contains bonds
        has_bonds = False
        for csv_file in csv_files:
            try:
                df = pl.read_csv(csv_file)
                asset_types = df.select("asset_type").to_series().unique().to_list()
                if any("bond" in str(t).lower() for t in asset_types if t):
                    has_bonds = True
                    break
            except Exception:
                continue

        if not has_bonds:
            return None

        update("🔍 Bonds detected. Running bond analysis...")

        # Import bond analyzer
        bond_analyzer_script = Path(__file__).parent / "bond_analyzer.py"
        if not bond_analyzer_script.exists():
            logger.warning("bond_analyzer.py not found. Skipping bond analysis.")
            return None

        # Determine input file (preferably master portfolio if available)
        input_file = None
        for csv_file in csv_files:
            if "master" in csv_file.name.lower():
                input_file = csv_file
                break
        if not input_file:
            input_file = csv_files[0]

        # Output path
        output_path = PORTFOLIO_DIR.parent / "portfolio_reports" / "bond_analysis.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Run bond analyzer
        cmd = [
            sys.executable,
            str(bond_analyzer_script),
            str(input_file),
            str(output_path),
        ]

        update(f"  Analyzing bonds from {input_file.name}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            update("  ✅ Bond analysis complete")
            logger.info(f"Bond analysis saved to {output_path}")
            return output_path
        else:
            logger.warning(f"Bond analysis failed: {result.stderr}")
            return None

    except Exception as e:
        logger.warning(f"Error running bond analysis: {e}")
        return None


def generate_setup_summary(results: Dict) -> str:
    """Generate a summary of what was discovered and converted."""
    summary = []
    summary.append("\n📊 **Portfolio Auto-Setup Complete**\n")

    if results["pdf_files"]:
        summary.append(f"📄 PDFs found: {len(results['pdf_files'])}")
        for pdf in results["pdf_files"]:
            summary.append(f"  - {pdf.name}")

    if results["xls_files"]:
        summary.append(f"\n📊 Excel files found: {len(results['xls_files'])}")
        for xls in results["xls_files"]:
            summary.append(f"  - {xls.name}")

    if results["converted_files"]:
        summary.append(f"\n✅ Converted files: {len(results['converted_files'])}")
        for csv in results["converted_files"]:
            summary.append(f"  - {csv.name}")

    if results["csv_files"]:
        summary.append(f"\n📋 CSV portfolios available: {len(results['csv_files'])}")
        for csv in results["csv_files"]:
            summary.append(f"  - {csv.name}")

    if results["converted_files"] or results["csv_files"]:
        summary.append("\n✨ Ready for analysis. Use `/InvestorClaw` to start.")
    else:
        summary.append(
            "\n⚠️  No portfolio files found. Add CSV, XLS, or PDF files to your portfolios directory."
        )
        summary.append("📖 See the examples/ folder in your skill directory for format reference.")

    return "\n".join(summary)


def main():
    """Main setup function."""
    try:
        # Bootstrap: immediate acknowledgement
        bootstrap("portfolio_skill_setup")

        print(
            "\n" + "🔔 ORIENTATION FOR NEW USERS" + "\n"
            "InvestorClaw is an EDUCATIONAL portfolio analysis tool.\n"
            "It is NOT a fiduciary advisor and cannot provide investment advice.\n"
            "Use the analysis to have informed conversations with your financial advisor.\n"
        )

        # Check if already setup
        if SETUP_MARKER.exists():
            phase(Phase.COMPLETE, "Setup already complete. Skipping.")
            print()
            tip = stonkmode_tip(always=False)
            if tip:
                print(tip)
                print()
            return 0

        phase(
            Phase.INIT, "Initializing portfolio analyzer...", {"portfolio_dir": str(PORTFOLIO_DIR)}
        )

        # Check API keys
        update("Checking API key configuration...")
        check_api_keys()

        # Check dependencies
        update("Checking dependencies...")
        deps = check_dependencies()

        # Report available PDF extraction tools
        pdf_tools = [
            tool
            for tool in ["pdfplumber", "tabula", "camelot", "pypdf2", "pdfminer"]
            if deps.get(tool)
        ]
        if pdf_tools:
            update(f"✅ PDF extraction tools available: {', '.join(pdf_tools)}")
        else:
            update("⚠️  No PDF extraction tools found. For best results: pip install pdfplumber")

        if not deps.get("polars") or not deps.get("openpyxl"):
            update(
                "⚠️  Optional: pip install polars tabula-py camelot-py[cv] pdfplumber openpyxl (for Excel/PDF support)"
            )

        # Discover and convert files
        phase(Phase.DISCOVER, "Discovering portfolio files...")
        results = discover_and_convert_files()

        update(
            f"Found {len(results['pdf_files'])} PDFs, {len(results['xls_files'])} Excel files, {len(results['csv_files'])} CSVs"
        )

        # Extract from PDFs if tabula available
        if results["pdf_files"]:
            phase(
                Phase.EXTRACT_PDF,
                f"Extracting tables from {len(results['pdf_files'])} PDF files...",
            )
            for pdf in results["pdf_files"]:
                update(f"Processing {pdf.name}...")

        # Convert Excel files
        if results["xls_files"]:
            phase(
                Phase.CONVERT_XLS, f"Converting {len(results['xls_files'])} Excel files to CSV..."
            )
            for xls in results["xls_files"]:
                update(f"Converting {xls.name}...")

        # Consolidate if multiple files
        all_csv = results["csv_files"] + results["converted_files"]
        if not all_csv:
            update(
                "⚠️  No portfolio files found or converted. Please add CSV/Excel/PDF files to ~/portfolios/"
            )
            logger.warning("No usable portfolio files found after discovery")
        elif len(all_csv) > 1:
            phase(Phase.CONSOLIDATE, f"Consolidating {len(all_csv)} portfolio files...")
            update("Merging holdings, detecting duplicates...")
            master = consolidate_portfolios(all_csv)
            if master:
                results["master_portfolio"] = str(master)
                update("Created master_portfolio.csv")

        # Analyze bonds if present
        phase(Phase.ANALYZE, "Analyzing bonds if present...")
        bond_analysis = analyze_bonds_if_present(all_csv if all_csv else results["csv_files"])
        if bond_analysis:
            results["bond_analysis"] = str(bond_analysis)

        # Generate summary
        summary = generate_setup_summary(results)
        update("Setup summary:")
        for line in summary.split("\n"):
            if line.strip():
                update(line)

        # Mark setup as complete only if we have usable portfolio data
        if all_csv:
            SETUP_MARKER.parent.mkdir(parents=True, exist_ok=True)
            SETUP_MARKER.touch()

        # Save results to manifest
        import time

        manifest_path = PORTFOLIO_DIR / "setup_results.json"
        results_to_save = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "pdf_files": [str(f) for f in results["pdf_files"]],
            "xls_files": [str(f) for f in results["xls_files"]],
            "csv_files": [str(f) for f in results["csv_files"]],
            "converted_files": [str(f) for f in results["converted_files"]],
        }

        with open(manifest_path, "w") as f:
            json.dump(results_to_save, f, indent=2)

        phase(
            Phase.COMPLETE,
            "Portfolio analyzer setup complete",
            {
                "pdfs_found": len(results["pdf_files"]),
                "excel_files_found": len(results["xls_files"]),
                "csv_files": len(results["csv_files"]),
                "converted_files": len(results["converted_files"]),
                "ready_for_analysis": "Yes" if all_csv else "No",
            },
        )

        return 0

    except Exception as e:
        report_error(f"Setup failed: {str(e)}", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
