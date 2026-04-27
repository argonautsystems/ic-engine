# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for ic_engine.pipeline.run_pipeline.

Covers the four codex P2 fixes in v2.4.6:
  1. output_dir defaults to get_reports_dir() (env-aware).
  2. holdings_summary.json + performance.json land at reports_dir top
     level (not .raw/).
  3. ReportExporter receives the full CDM holdings (with portfolio.equity
     etc. buckets), not the compact summary.
  4. Uppercase .CSV inputs don't self-overwrite the source.

These are unit tests with the heavy-network parts (PerformanceAnalyzer and
PortfolioFetcher) monkey-patched; ReportExporter is mocked only for pipeline
orchestration assertions. Real integration is covered by manual runs against
a sample portfolio.
"""

from __future__ import annotations

import csv
import json
import os
import stat
from pathlib import Path

import pytest

# --- Synthetic CDM-shaped portfolio used across tests --------------------------

_SYNTHETIC_HOLDINGS_RAW: dict = {
    "portfolio": {
        "summary": {"total_value": 100_000.0, "asset_count": 2},
        "equity": {
            "AAPL": {
                "shares": 10.0,
                "purchase_price": 150.0,
                "current_price": 200.0,
                "value": 2000.0,
            },
            "MSFT": {
                "shares": 5.0,
                "purchase_price": 300.0,
                "current_price": 400.0,
                "value": 2000.0,
            },
        },
        "bond": {},
        "cash": {},
        "margin": {},
    }
}

_CANONICAL_FETCHER_SUMMARY: dict = {
    "disclaimer": "EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
    "is_investment_advice": False,
    "_note": (
        "Compact summary for LLM analysis. Full CDM data is at output_file for "
        "downstream scripts only."
    ),
    "summary": {
        "total_value": 100_000.0,
        "net_value": 100_000.0,
        "equity_value": 75_000.0,
        "bond_value": 15_000.0,
        "cash_value": 10_000.0,
        "equity_pct": 75.0,
        "bond_pct": 15.0,
        "cash_pct": 10.0,
    },
    "top_equity": [{"symbol": "AAPL", "sector": "Technology", "value": 50_000.0}],
    "sector_weights": {"Technology": 100.0},
    "accounts": {
        "Taxable": {
            "financial_type": "taxable",
            "managed": False,
            "value": 75_000.0,
            "position_count": 2,
            "weight_pct": 100.0,
        }
    },
}


def _synthetic_cdm_holdings_raw(summary: dict | None = None) -> dict:
    return {
        "cdmVersion": "5.0",
        "portfolio": {
            "summary": (
                summary
                if summary is not None
                else {
                    "totalPortfolioValue": 100_000.0,
                    "equityValue": 2_000.0,
                    "equityPct": 2.0,
                    "bondValue": 0.0,
                    "bondPct": 0.0,
                    "cashValue": 98_000.0,
                    "cashPct": 98.0,
                    "netValue": 100_000.0,
                    "totalUnrealizedGainLoss": 500.0,
                    "totalUnrealizedGainLossPct": 0.5,
                }
            ),
            "portfolioState": {
                "positions": [
                    {
                        "product": {"productIdentifier": {"identifier": "AAPL"}},
                        "asset": {
                            "securityType": "Equity",
                            "sector": "Technology",
                            "securityName": "Apple Inc.",
                        },
                        "priceQuantity": {
                            "quantity": {"amount": 10.0},
                            "currentPrice": {"amount": 200.0},
                            "costBasisPrice": {"amount": 150.0},
                        },
                        "marketValue": 2_000.0,
                        "costBasis": 1_500.0,
                        "unrealizedGainLoss": 500.0,
                        "unrealizedGainLossPct": 33.3333,
                    }
                ]
            },
        },
    }


def _synthetic_cdm_bond_cash_holdings_raw() -> dict:
    return {
        "cdmVersion": "5.0",
        "portfolio": {
            "summary": {
                "totalPortfolioValue": 12_450.0,
                "equityValue": 0.0,
                "bondValue": 9_950.0,
                "cashValue": 2_500.0,
                "netValue": 12_450.0,
            },
            "portfolioState": {
                "positions": [
                    {
                        "product": {"productIdentifier": {"identifier": "9128285M8"}},
                        "asset": {
                            "securityType": "Bond",
                            "securityName": "US Treasury 2.0% 2030",
                        },
                        "priceQuantity": {
                            "quantity": {"amount": 10_000.0},
                            "currentPrice": {"amount": 99.5},
                            "costBasisPrice": {"amount": 98.0},
                        },
                        "marketValue": 9_950.0,
                        "costBasis": 9_800.0,
                        "unrealizedGainLoss": 150.0,
                        "unrealizedGainLossPct": 1.53,
                    },
                    {
                        "product": {"productIdentifier": {"identifier": "CASH-USD"}},
                        "asset": {
                            "securityType": "Cash",
                            "securityName": "USD Cash Sweep",
                        },
                        "priceQuantity": {
                            "quantity": {"amount": 2_500.0},
                            "currentPrice": {"amount": 1.0},
                            "costBasisPrice": {"amount": 1.0},
                        },
                        "marketValue": 2_500.0,
                        "costBasis": 2_500.0,
                        "unrealizedGainLoss": 0.0,
                        "unrealizedGainLossPct": 0.0,
                    },
                ]
            },
        },
    }


def _write_holdings_json(tmp_path: Path) -> Path:
    holdings_file = tmp_path / "holdings.json"
    holdings_file.write_text(json.dumps(_SYNTHETIC_HOLDINGS_RAW), encoding="utf-8")
    return holdings_file


def _write_holdings_csv_uppercase(tmp_path: Path) -> Path:
    """Create a holdings file with .CSV (uppercase) suffix."""
    holdings_file = tmp_path / "holdings.CSV"
    holdings_file.write_text("symbol,shares,price\nAAPL,10,200\nMSFT,5,400\n", encoding="utf-8")
    return holdings_file


# --- Patch helpers --------------------------------------------------------------


class _FakePortfolioFetcher:
    """PortfolioFetcher stand-in that mirrors the JSON + compact summary side effects."""

    def main(self, input_path: str, output_path: str) -> None:
        del input_path
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps(_SYNTHETIC_HOLDINGS_RAW), encoding="utf-8")

        reports_dir = output_file.parent
        if reports_dir.name == ".raw":
            reports_dir = reports_dir.parent
        summary = {**_CANONICAL_FETCHER_SUMMARY, "output_file": str(output_file)}
        (reports_dir / "holdings_summary.json").write_text(json.dumps(summary), encoding="utf-8")


class _FakePerformanceAnalyzer:
    """PerformanceAnalyzer stand-in: write a stub performance.json so the
    pipeline can pass it to the exporter."""

    def analyze_portfolio(self, holdings_path: str, output_path: str) -> None:
        Path(output_path).write_text(
            json.dumps({"equities": {}, "bonds": {}, "summary": {"sharpe": 1.5}}),
            encoding="utf-8",
        )


class _RecordingReportExporter:
    """Captures what the pipeline passes to ReportExporter.load_data so we
    can assert the FULL CDM holdings file is fed (not the compact summary)."""

    last_holdings_file: str | None = None
    last_performance_file: str | None = None

    def load_data(self, holdings_file: str, performance_file: str | None = None) -> None:
        type(self).last_holdings_file = holdings_file
        type(self).last_performance_file = performance_file

    def export_to_csv(self, output_prefix: str) -> None:
        # No-op for the test; verifying export-path mechanics is out of scope.
        Path(output_prefix + ".sentinel").parent.mkdir(parents=True, exist_ok=True)
        Path(output_prefix + ".sentinel").write_text("ok", encoding="utf-8")


@pytest.fixture
def patched_pipeline(monkeypatch):
    """Patch the network/heavy pieces of run_pipeline so tests stay offline."""
    import ic_engine.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "PerformanceAnalyzer", _FakePerformanceAnalyzer)
    monkeypatch.setattr(pipeline_mod, "ReportExporter", _RecordingReportExporter)
    # PortfolioFetcher is imported INSIDE the function for the .csv branch;
    # patch the source module so the inline import picks up the fake.
    import ic_engine.commands.fetch_holdings as fetch_holdings_mod

    monkeypatch.setattr(fetch_holdings_mod, "PortfolioFetcher", _FakePortfolioFetcher)
    # Reset the recording exporter state between tests.
    _RecordingReportExporter.last_holdings_file = None
    _RecordingReportExporter.last_performance_file = None


# --- Fix 1: output_dir defaults to get_reports_dir() ---------------------------


def test_output_dir_defaults_to_get_reports_dir(tmp_path, monkeypatch, patched_pipeline):
    """When output_dir is omitted, pipeline must use get_reports_dir() (which
    honors INVESTOR_CLAW_REPORTS_DIR), not a hardcoded ~/portfolio_reports.
    """
    from ic_engine.pipeline import run_pipeline

    custom_reports_root = tmp_path / "custom-reports"
    monkeypatch.setenv("INVESTOR_CLAW_REPORTS_DIR", str(custom_reports_root))
    monkeypatch.setenv("INVESTOR_CLAW_DATED_REPORTS", "false")  # land at the base dir directly

    holdings_file = _write_holdings_json(tmp_path)
    result = run_pipeline(str(holdings_file))

    reports_dir = Path(result["reports_dir"])
    assert reports_dir == custom_reports_root.resolve(), (
        f"Expected reports_dir under INVESTOR_CLAW_REPORTS_DIR="
        f"{custom_reports_root}, got {reports_dir}"
    )


# --- Fix 2: summary + performance at reports_dir top-level ---------------------


def test_summary_and_performance_at_reports_dir_top_level(tmp_path, patched_pipeline):
    """Pre-v2.4.6 the pipeline buried holdings_summary.json + performance.json
    under .raw/, but downstream consumers (EOD report, FA discussion,
    dashboard) look for them at reports_dir top-level.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_file = _write_holdings_json(tmp_path)
    output_dir = tmp_path / "reports"
    result = run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary_path = Path(result["holdings_summary"])
    performance_path = Path(result["performance"])

    assert summary_path == output_dir / "holdings_summary.json"
    assert performance_path == output_dir / "performance.json"
    assert summary_path.exists()
    assert performance_path.exists()
    # The CDM snapshot stays under .raw/
    assert Path(result["normalized_holdings"]) == output_dir / ".raw" / "holdings.json"


# --- Fix 3: ReportExporter receives the FULL CDM (not compact summary) ---------


def test_exporter_receives_full_cdm_not_compact_summary(tmp_path, patched_pipeline):
    """Pre-v2.4.6 the pipeline passed holdings_summary.json (compact, no per-
    account buckets) to ReportExporter, which iterates portfolio.equity /
    bond / cash / margin. Exports came out empty per-bucket. Fix is to
    feed the full CDM snapshot from .raw/holdings.json instead.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_file = _write_holdings_json(tmp_path)
    output_dir = tmp_path / "reports"
    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    cdm_path = output_dir / ".raw" / "holdings.json"
    summary_path = output_dir / "holdings_summary.json"
    assert _RecordingReportExporter.last_holdings_file == str(cdm_path), (
        "ReportExporter should receive the full CDM holdings file under .raw/, "
        f"got {_RecordingReportExporter.last_holdings_file}"
    )
    # Sanity: ensure the CDM snapshot has the per-account buckets the
    # exporter actually needs.
    cdm_data = json.loads(cdm_path.read_text())
    assert "portfolio" in cdm_data
    assert "equity" in cdm_data["portfolio"]
    # And that the compact summary file does NOT have those buckets (we'd
    # be back to the old broken behavior if it did).
    summary_data = json.loads(summary_path.read_text())
    assert "equity" not in summary_data, (
        "compact summary unexpectedly carries equity bucket; if exporter is "
        "fed this file (the pre-v2.4.6 behavior), the per-bucket detail still "
        "round-trips and the test wouldn't be catching the regression"
    )


def test_report_exporter_uses_cdm_market_value_for_current_value(tmp_path):
    """ReportExporter must read the normalized CDM market_value field. The
    pipeline feeds it the full CDM file, and normalize_portfolio stores each
    position's current value as market_value rather than legacy value.
    """
    from ic_engine.commands.export_report import ReportExporter

    holdings_file = tmp_path / "holdings-cdm.json"
    holdings_file.write_text(json.dumps(_synthetic_cdm_holdings_raw()), encoding="utf-8")

    exporter = ReportExporter()
    exporter.load_data(str(holdings_file))

    holding = exporter.holdings_data["portfolio"]["equity"]["AAPL"]
    assert "market_value" in holding
    assert "value" not in holding

    row = exporter.create_equity_report().row(0, named=True)
    assert row["Current Value"] == 2_000.0
    assert row["Unrealized %"] == 33.3333


def test_report_exporter_exports_cdm_bond_quantity_and_cash_amount(tmp_path):
    """Detailed exports must preserve CDM bond and cash quantities after the
    exporter normalizes raw CDM holdings into legacy report buckets.
    """
    from ic_engine.commands.export_report import ReportExporter

    holdings_file = tmp_path / "holdings-cdm-bond-cash.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_bond_cash_holdings_raw()),
        encoding="utf-8",
    )

    exporter = ReportExporter()
    exporter.load_data(str(holdings_file))
    exporter.export_to_csv(str(tmp_path / "portfolio_report"))

    bond_file = next(tmp_path.glob("portfolio_report_bonds_*.csv"))
    cash_file = next(tmp_path.glob("portfolio_report_cash_*.csv"))

    with open(bond_file, newline="", encoding="utf-8") as f:
        bond_row = next(csv.DictReader(f))
    with open(cash_file, newline="", encoding="utf-8") as f:
        cash_row = next(csv.DictReader(f))

    assert float(bond_row["Quantity"]) == 10_000.0
    assert float(cash_row["Amount"]) == 2_500.0


def test_pipeline_external_cdm_exports_report_csv_without_keyerror(tmp_path, monkeypatch):
    """An external raw CDM input gets normalized into .raw/holdings.json and
    then loaded by the real ReportExporter. The second normalization pass must
    preserve portfolio.summary so summary/allocation CSV export does not raise
    KeyError for missing equity_value.
    """
    import ic_engine.pipeline as pipeline_mod
    from ic_engine.pipeline import run_pipeline

    monkeypatch.setattr(pipeline_mod, "PerformanceAnalyzer", _FakePerformanceAnalyzer)

    holdings_file = tmp_path / "external-cdm.json"
    holdings_file.write_text(json.dumps(_synthetic_cdm_holdings_raw()), encoding="utf-8")
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary_exports = list(output_dir.glob("portfolio_report_summary_*.csv"))
    allocation_exports = list(output_dir.glob("portfolio_report_allocation_*.csv"))
    equity_exports = list(output_dir.glob("portfolio_report_equities_*.csv"))

    assert summary_exports, "expected portfolio_report summary CSV export"
    assert allocation_exports, "expected portfolio_report allocation CSV export"
    assert equity_exports, "expected portfolio_report equities CSV export"
    assert "Equity Value" in summary_exports[0].read_text(encoding="utf-8")


# --- Fix 4: uppercase .CSV doesn't self-overwrite ------------------------------


def test_uppercase_csv_does_not_self_overwrite(tmp_path, patched_pipeline):
    """Pre-v2.4.6 the pipeline did `holdings_path.replace(".csv", ".json")`.
    A `.CSV` input was unchanged, so PortfolioFetcher.main(input, output)
    ran with output == input and overwrote the user's CSV with JSON content.
    """
    from ic_engine.pipeline import run_pipeline

    csv_file = _write_holdings_csv_uppercase(tmp_path)
    csv_original_content = csv_file.read_text(encoding="utf-8")
    output_dir = tmp_path / "reports"
    run_pipeline(str(csv_file), output_dir=str(output_dir))

    # Source CSV must be untouched.
    assert csv_file.exists()
    assert csv_file.read_text(encoding="utf-8") == csv_original_content, (
        "uppercase .CSV input was modified by the pipeline; conversion must "
        "write a separate JSON file instead of overwriting the source"
    )

    assert (output_dir / ".raw" / "holdings.json").exists()


def test_csv_pipeline_preserves_fetcher_compact_summary_in_reports_dir(tmp_path, patched_pipeline):
    """CSV conversion writes through PortfolioFetcher, whose compact summary
    must be the reports_dir artifact consumed by EOD/FA/dashboard.
    """
    from ic_engine.pipeline import run_pipeline

    csv_file = _write_holdings_csv_uppercase(tmp_path)
    output_dir = tmp_path / "separate-reports"

    result = run_pipeline(str(csv_file), output_dir=str(output_dir))

    summary_path = Path(result["holdings_summary"])
    assert summary_path == output_dir / "holdings_summary.json"
    assert Path(result["normalized_holdings"]) == output_dir / ".raw" / "holdings.json"

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert "_pipeline_compat_note" not in summary
    assert summary["output_file"] == str(output_dir / ".raw" / "holdings.json")
    assert summary["top_equity"]
    assert summary["accounts"]
    assert summary["sector_weights"]


# --- Codex round-2 P1: raw CDM input must not be self-overwritten -------------


def test_same_file_raw_cdm_input_is_not_rewritten(tmp_path, monkeypatch, patched_pipeline):
    """In the router's no-arg flow, the input already is
    `<reports_dir>/.raw/holdings.json`. The pipeline must not rewrite that
    same file with normalize_portfolio(raw), because that drops the raw CDM
    portfolioState.positions array used by lookup.query_holdings_symbol.
    """
    from ic_engine.pipeline import run_pipeline

    reports_dir = tmp_path / "reports"
    raw_dir = reports_dir / ".raw"
    raw_dir.mkdir(parents=True)
    holdings_file = raw_dir / "holdings.json"
    original_text = json.dumps(_synthetic_cdm_holdings_raw(), indent=2)
    holdings_file.write_text(original_text, encoding="utf-8")

    monkeypatch.setenv("INVESTOR_CLAW_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("INVESTOR_CLAW_DATED_REPORTS", "false")

    result = run_pipeline(str(holdings_file))

    assert Path(result["normalized_holdings"]) == holdings_file
    assert holdings_file.read_text(encoding="utf-8") == original_text
    preserved = json.loads(holdings_file.read_text(encoding="utf-8"))
    assert (
        preserved["portfolio"]["portfolioState"]["positions"][0]["product"]["productIdentifier"][
            "identifier"
        ]
        == "AAPL"
    )


# --- Codex round-2 P1: pre-existing compact summary is preserved ---------------


def test_existing_compact_summary_preserved(tmp_path, patched_pipeline):
    """If `<reports_dir>/holdings_summary.json` already exists (the canonical
    case after running ic-holdings), the pipeline must NOT overwrite it
    with a lossy CDM-derived stand-in. The canonical compact has snake-
    case keys (top_equity, sector_weights, summary.total_value,
    summary.equity_pct) that the CDM doesn't expose verbatim; clobbering
    it makes EOD/FA/dashboard show zero totals despite valid data.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_file = _write_holdings_json(tmp_path)
    output_dir = tmp_path / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)

    canonical_summary = {
        "summary": {"total_value": 999_999.0, "equity_pct": 87.5},
        "top_equity": [{"symbol": "NVDA", "value": 50000.0}],
        "sector_weights": {"Technology": 60.0, "Finance": 40.0},
    }
    summary_path = output_dir / "holdings_summary.json"
    summary_path.write_text(json.dumps(canonical_summary), encoding="utf-8")

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    after = json.loads(summary_path.read_text(encoding="utf-8"))
    assert after == canonical_summary, (
        "pre-existing canonical compact summary was clobbered by the pipeline's "
        "best-effort CDM-derived stand-in"
    )


# --- Codex round-2 P2: stale compact summary is refreshed ----------------------


def test_stale_compact_summary_regenerated_when_input_is_newer(tmp_path, patched_pipeline):
    """A pre-existing compact summary is only canonical for the matching input.
    If a later pipeline run points the same output_dir at a newer holdings
    file, the stale summary must be regenerated from that input.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_summary = {
        "totalPortfolioValue": 250_000.0,
        "equityValue": 125_000.0,
        "equityPct": 50.0,
        "bondValue": 75_000.0,
        "bondPct": 30.0,
        "cashValue": 50_000.0,
        "cashPct": 20.0,
        "netValue": 249_000.0,
        "totalUnrealizedGainLoss": 1_234.0,
        "totalUnrealizedGainLossPct": 0.49,
    }
    holdings_file = tmp_path / "new-holdings.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=holdings_summary)),
        encoding="utf-8",
    )

    output_dir = tmp_path / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "holdings_summary.json"
    summary_path.write_text(
        json.dumps({"summary": {"total_value": 1.0}, "top_equity": [{"symbol": "OLD"}]}),
        encoding="utf-8",
    )
    os.utime(summary_path, (1_700_000_000, 1_700_000_000))
    os.utime(holdings_file, (1_700_000_100, 1_700_000_100))

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    after = json.loads(summary_path.read_text(encoding="utf-8"))
    assert after["summary"]["total_value"] == 250_000.0
    assert after["summary"]["equity_value"] == 125_000.0
    assert after["summary"]["unrealized_gl"] == 1_234.0
    assert after["summary"]["unrealized_gl_pct"] == 0.49
    assert after["top_equity"][0]["symbol"] == "AAPL"
    assert after["sector_weights"] == {"Technology": 100.0}


def test_fallback_summary_derives_holdings_and_sectors_when_cdm_summary_exists(
    tmp_path, patched_pipeline
):
    """A normal CDM file has provider summary totals plus position buckets.
    Missing compact summaries must still derive top_equity/sector_weights from
    those buckets instead of returning only translated totals.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_summary = {
        "totalPortfolioValue": 2_000.0,
        "equityValue": 2_000.0,
        "equityPct": 100.0,
        "bondValue": 0.0,
        "bondPct": 0.0,
        "cashValue": 0.0,
        "cashPct": 0.0,
        "netValue": 2_000.0,
    }
    holdings_file = tmp_path / "cdm-summary-with-positions.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=holdings_summary)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary_path = output_dir / "holdings_summary.json"
    compact = json.loads(summary_path.read_text(encoding="utf-8"))
    assert compact["summary"]["total_value"] == 2_000.0
    assert compact["top_equity"][0]["symbol"] == "AAPL"
    assert compact["top_equity"][0]["value"] == 2_000.0
    assert compact["sector_weights"] == {"Technology": 100.0}


def test_fallback_summary_preserves_bucket_totals_with_partial_cdm_summary(
    tmp_path, patched_pipeline
):
    """A partial provider summary must not zero out bucket-derived allocations."""
    from ic_engine.pipeline import run_pipeline

    raw = _synthetic_cdm_bond_cash_holdings_raw()
    equity_position = _synthetic_cdm_holdings_raw(summary={})["portfolio"]["portfolioState"][
        "positions"
    ][0]
    raw["portfolio"]["portfolioState"]["positions"].insert(0, equity_position)
    raw["portfolio"]["summary"] = {"totalPortfolioValue": 14_450.0}

    holdings_file = tmp_path / "partial-summary-with-buckets.json"
    holdings_file.write_text(json.dumps(raw), encoding="utf-8")
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary = json.loads((output_dir / "holdings_summary.json").read_text(encoding="utf-8"))[
        "summary"
    ]
    assert summary["total_value"] == 14_450.0
    assert summary["equity_value"] == 2_000.0
    assert summary["bond_value"] == 9_950.0
    assert summary["cash_value"] == 2_500.0


def test_fallback_top_equity_weight_uses_provider_total_value(tmp_path, patched_pipeline):
    """Top-position weights use provider totalPortfolioValue when buckets are partial."""
    from ic_engine.pipeline import run_pipeline

    holdings_file = tmp_path / "provider-total-exceeds-bucket-sum.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary={"totalPortfolioValue": 100_000.0})),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    compact = json.loads((output_dir / "holdings_summary.json").read_text(encoding="utf-8"))
    assert compact["summary"]["total_value"] == 100_000.0
    assert compact["summary"]["equity_value"] == 2_000.0
    assert compact["top_equity"][0]["weight_pct"] == 2.0


def test_summary_regenerated_when_input_identity_changes_even_if_older(tmp_path, patched_pipeline):
    """A newer summary from one pipeline input must not be reused for a
    different holdings file just because that new input has an older mtime.
    """
    from ic_engine.pipeline import run_pipeline

    first_summary = {
        "totalPortfolioValue": 111_000.0,
        "equityValue": 11_000.0,
        "equityPct": 9.9,
        "bondValue": 0.0,
        "bondPct": 0.0,
        "cashValue": 100_000.0,
        "cashPct": 90.1,
        "netValue": 111_000.0,
        "totalUnrealizedGainLoss": 100.0,
        "totalUnrealizedGainLossPct": 0.09,
    }
    second_summary = {
        "totalPortfolioValue": 222_000.0,
        "equityValue": 122_000.0,
        "equityPct": 55.0,
        "bondValue": 50_000.0,
        "bondPct": 22.5,
        "cashValue": 50_000.0,
        "cashPct": 22.5,
        "netValue": 222_000.0,
        "totalUnrealizedGainLoss": 2_222.0,
        "totalUnrealizedGainLossPct": 1.0,
    }
    first_holdings = tmp_path / "first-holdings.json"
    second_holdings = tmp_path / "second-holdings.json"
    first_holdings.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=first_summary)),
        encoding="utf-8",
    )
    second_holdings.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=second_summary)),
        encoding="utf-8",
    )

    output_dir = tmp_path / "reports"
    run_pipeline(str(first_holdings), output_dir=str(output_dir))

    summary_path = output_dir / "holdings_summary.json"
    os.utime(summary_path, (1_700_000_200, 1_700_000_200))
    os.utime(second_holdings, (1_700_000_000, 1_700_000_000))

    run_pipeline(str(second_holdings), output_dir=str(output_dir))

    after = json.loads(summary_path.read_text(encoding="utf-8"))
    assert after["summary"]["total_value"] == 222_000.0
    assert after["summary"]["equity_value"] == 122_000.0
    assert after["summary"]["unrealized_gl"] == 2_222.0


def test_fallback_summary_preserves_margin_debt_and_derives_pct(tmp_path, patched_pipeline):
    """Fallback compact summaries must keep margin debt so EOD/FA consumers
    do not report a zero margin balance after regeneration.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_summary = {
        "totalPortfolioValue": 100_000.0,
        "equityValue": 60_000.0,
        "bondValue": 25_000.0,
        "cashValue": 15_000.0,
        "marginValue": 10_000.0,
        "netValue": 90_000.0,
    }
    holdings_file = tmp_path / "holdings-with-margin.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=holdings_summary)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary = json.loads((output_dir / "holdings_summary.json").read_text(encoding="utf-8"))[
        "summary"
    ]
    assert summary["margin_value"] > 0
    assert summary["margin_value"] == 10_000.0
    assert summary["margin_pct"] == 10.0


# --- Codex round-2 P2: all CDM summary fields translate to snake_case ----------


def test_fallback_summary_translates_all_cdm_summary_fields(tmp_path, patched_pipeline):
    """The fallback compact summary must expose the snake_case keys EOD/FA read,
    prefer snake_case when both spellings exist, and avoid leaking unmapped CDM
    camelCase keys verbatim.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_summary = {
        "totalPortfolioValue": 100_000.0,
        "total_value": 101_000.0,
        "equityValue": 60_000.0,
        "equity_value": 61_000.0,
        "equityPct": 60.0,
        "equity_pct": 61.0,
        "bondValue": 30_000.0,
        "bondPct": 30.0,
        "cashValue": 10_000.0,
        "cash_value": 11_000.0,
        "cashPct": 10.0,
        "marginValue": 5_000.0,
        "margin_value": 6_000.0,
        "marginPct": 5.0,
        "margin_pct": 6.0,
        "netValue": 99_500.0,
        "totalUnrealizedGainLoss": -250.0,
        "totalUnrealizedGainLossPct": -0.25,
        "assetCount": 4,
    }
    holdings_file = tmp_path / "holdings.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=holdings_summary)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary = json.loads((output_dir / "holdings_summary.json").read_text(encoding="utf-8"))[
        "summary"
    ]
    assert summary == {
        "total_value": 101_000.0,
        "equity_value": 61_000.0,
        "equity_pct": 61.0,
        "bond_value": 30_000.0,
        "bond_pct": 30.0,
        "cash_value": 11_000.0,
        "cash_pct": 10.0,
        "margin_value": 6_000.0,
        "margin_pct": 6.0,
        "net_value": 99_500.0,
        "total_cost_basis": 0.0,
        "unrealized_gl": -250.0,
        "unrealized_gl_pct": -0.25,
        "crypto_value": 0.0,
        "futures_value": 0.0,
        "metals_value": 0.0,
        "crypto_pct": 0.0,
        "futures_pct": 0.0,
        "metals_pct": 0.0,
        "position_count": {
            "equity": 1,
            "bond": 0,
            "cash": 0,
            "crypto": 0,
            "futures": 0,
            "metals": 0,
        },
    }
    assert "totalPortfolioValue" not in summary
    assert "assetCount" not in summary
    assert "unrealizedPct" not in summary


def test_fallback_summary_skips_null_aliases_before_cdm_camel_case(tmp_path, patched_pipeline):
    from ic_engine.pipeline import run_pipeline

    holdings_summary = {
        "total_value": None,
        "total_portfolio_value": None,
        "totalPortfolioValue": 100_000.0,
        "equity_value": None,
        "equityValue": 60_000.0,
        "cash_value": None,
        "cashValue": 40_000.0,
    }
    holdings_file = tmp_path / "holdings-with-null-aliases.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=holdings_summary)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary = json.loads((output_dir / "holdings_summary.json").read_text(encoding="utf-8"))[
        "summary"
    ]
    assert summary["total_value"] == 100_000.0
    assert summary["equity_value"] == 60_000.0
    assert summary["cash_value"] == 40_000.0
    assert summary["equity_pct"] == 60.0
    assert summary["cash_pct"] == 40.0


def test_fallback_summary_translates_legacy_dataclass_summary_fields(tmp_path, patched_pipeline):
    """Legacy normalized JSON may carry PortfolioSummary dataclass field names.
    The pipeline fallback must preserve those totals before downstream
    consumers get a chance to normalize aliases.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_summary = {
        "total_portfolio_value": 100_000.0,
        "net_worth": 92_000.0,
        "total_cost_basis": 80_000.0,
        "total_unrealized_gain_loss": 20_000.0,
        "total_unrealized_gain_loss_pct": 25.0,
        "equity_value": 75_000.0,
        "equity_pct": 75.0,
        "bond_value": 24_500.0,
        "bond_pct": 24.5,
        "cash_value": 500.0,
        "cash_pct": 0.5,
        "margin_value": 8_000.0,
        "margin_pct": 8.0,
    }
    holdings_file = tmp_path / "legacy-summary-holdings.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=holdings_summary)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary = json.loads((output_dir / "holdings_summary.json").read_text(encoding="utf-8"))[
        "summary"
    ]
    assert summary["total_value"] == 100_000.0
    assert summary["net_value"] == 92_000.0
    assert summary["total_cost_basis"] == 80_000.0
    assert summary["unrealized_gl"] == 20_000.0
    assert summary["unrealized_gl_pct"] == 25.0
    assert summary["equity_value"] == 75_000.0
    assert summary["bond_value"] == 24_500.0
    assert summary["cash_value"] == 500.0
    assert summary["margin_value"] == 8_000.0
    assert summary["cash_pct"] == 0.5


def test_fallback_summary_defaults_missing_net_value_to_total_value(tmp_path, patched_pipeline):
    """PortfolioSummary.to_dict() emits totalPortfolioValue but no netValue.
    The fallback compact summary must not materialize net_value as 0.0 because
    EOD treats a present zero as authoritative.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_summary = {
        "totalPortfolioValue": 125_000.0,
        "equityValue": 75_000.0,
        "equityPct": 60.0,
        "bondValue": 25_000.0,
        "bondPct": 20.0,
        "cashValue": 25_000.0,
        "cashPct": 20.0,
        "totalUnrealizedGainLoss": 5_000.0,
        "totalUnrealizedGainLossPct": 4.0,
    }
    holdings_file = tmp_path / "holdings-without-net-value.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=holdings_summary)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary = json.loads((output_dir / "holdings_summary.json").read_text(encoding="utf-8"))[
        "summary"
    ]
    assert summary["total_value"] == 125_000.0
    assert summary["net_value"] == summary["total_value"]


def test_fallback_summary_derives_missing_allocation_percentages(tmp_path, patched_pipeline):
    """External CDM can provide asset values but omit allocation percentages;
    the fallback compact summary must derive every allocation field EOD renders.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_summary = {
        "totalPortfolioValue": 200_000.0,
        "equityValue": 50_000.0,
        "bondValue": 30_000.0,
        "cashValue": 120_000.0,
    }
    holdings_file = tmp_path / "holdings-without-pcts.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=holdings_summary)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary = json.loads((output_dir / "holdings_summary.json").read_text(encoding="utf-8"))[
        "summary"
    ]
    assert summary["equity_pct"] == 25.0
    assert summary["bond_pct"] == 15.0
    assert summary["cash_pct"] == 60.0


def test_fallback_summary_derives_null_allocation_percentages(tmp_path, patched_pipeline):
    """JSON null percentage keys should behave like missing percentages so
    providers can emit values and still get derived allocation percentages.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_summary = {
        "totalPortfolioValue": 100_000.0,
        "equityValue": 50_000.0,
        "equityPct": None,
        "bondValue": 20_000.0,
        "bond_pct": None,
        "cashValue": 10_000.0,
        "cashPct": None,
        "marginValue": 5_000.0,
        "margin_pct": None,
        "cryptoValue": 10_000.0,
        "cryptoPct": None,
        "futuresValue": 5_000.0,
        "futures_pct": None,
        "metalsValue": 5_000.0,
        "metalsPct": None,
    }
    holdings_file = tmp_path / "holdings-with-null-pcts.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=holdings_summary)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary_path = output_dir / "holdings_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))["summary"]
    assert summary["equity_pct"] == 50.0
    assert summary["bond_pct"] == 20.0
    assert summary["cash_pct"] == 10.0
    assert summary["margin_pct"] == 5.0
    assert summary["crypto_pct"] == 10.0
    assert summary["futures_pct"] == 5.0
    assert summary["metals_pct"] == 5.0


def test_fallback_summary_derives_totals_from_buckets_without_summary(tmp_path, patched_pipeline):
    """Bucket-only inputs must not produce a compact fallback full of zeros."""
    from ic_engine.pipeline import run_pipeline

    holdings_file = tmp_path / "bucket-only-holdings.json"
    holdings_file.write_text(
        json.dumps(
            {
                "portfolio": {
                    "equity": {
                        "AAPL": {
                            "market_value": 60_000.0,
                            "shares": 100.0,
                            "current_price": 600.0,
                            "purchase_price": 500.0,
                            "sector": "Technology",
                        }
                    },
                    "bond": {
                        "9128285M8": {
                            "market_value": 30_000.0,
                            "shares": 30_000.0,
                            "current_price": 100.0,
                            "purchase_price": 99.0,
                            "security_name": "US Treasury 2.0% 2030",
                        }
                    },
                    "cash": {"CASH": {"market_value": 10_000.0}},
                    "margin": {},
                    "crypto": {"BTC": {"market_value": 5_000.0}},
                    "futures": {},
                    "metals": {"GLD": {"market_value": 5_000.0}},
                }
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary = json.loads((output_dir / "holdings_summary.json").read_text(encoding="utf-8"))[
        "summary"
    ]
    assert summary["total_value"] == 110_000.0
    assert summary["equity_value"] == 60_000.0
    assert summary["bond_value"] == 30_000.0
    assert summary["cash_value"] == 10_000.0
    assert summary["crypto_value"] == 5_000.0
    assert summary["metals_value"] == 5_000.0
    assert summary["equity_pct"] == pytest.approx(54.5)
    assert summary["bond_pct"] == pytest.approx(27.3)
    assert summary["cash_pct"] == pytest.approx(9.1)


def test_fallback_summary_translates_cdm5_extension_allocations_and_counts(
    tmp_path, patched_pipeline
):
    """CDM-derived fallback summaries must expose compact CDM-5 extension
    allocation fields and a position_count block for dashboard/router consumers.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_summary = {
        "totalPortfolioValue": 100_000.0,
        "equityValue": 40_000.0,
        "bondValue": 0.0,
        "cashValue": 0.0,
        "cryptoValue": 30_000.0,
        "futuresValue": 20_000.0,
        "metalsValue": 10_000.0,
    }
    holdings_file = tmp_path / "holdings-with-cdm5-extensions.json"
    holdings_file.write_text(
        json.dumps(_synthetic_cdm_holdings_raw(summary=holdings_summary)),
        encoding="utf-8",
    )
    output_dir = tmp_path / "reports"

    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    summary = json.loads((output_dir / "holdings_summary.json").read_text(encoding="utf-8"))[
        "summary"
    ]
    assert summary["crypto_value"] == 30_000.0
    assert summary["futures_value"] == 20_000.0
    assert summary["metals_value"] == 10_000.0
    assert summary["crypto_pct"] == 30.0
    assert summary["futures_pct"] == 20.0
    assert summary["metals_pct"] == 10.0
    assert summary["position_count"]["equity"] == 1


# --- Codex round-2 P2: performance.json mirrored under .raw/ for lookup --------


def test_performance_mirrored_under_raw_for_lookup_backcompat(tmp_path, patched_pipeline):
    """ic_engine.commands.lookup.query_performance_top reads
    `.raw/performance.json` via _load_raw. Moving the pipeline output
    only to top-level breaks that consumer, so v2.4.6 mirrors the
    artifact to BOTH locations.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_file = _write_holdings_json(tmp_path)
    output_dir = tmp_path / "reports"
    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    top_level = output_dir / "performance.json"
    raw_mirror = output_dir / ".raw" / "performance.json"

    assert top_level.exists(), "top-level performance.json missing"
    assert raw_mirror.exists(), ".raw/performance.json mirror missing — lookup will 404"
    assert top_level.read_text(encoding="utf-8") == raw_mirror.read_text(encoding="utf-8"), (
        "top-level vs .raw/ performance.json content drift; dual-write should "
        "produce identical files"
    )


def test_performance_raw_mirror_uses_secure_permissions(tmp_path, patched_pipeline):
    """The .raw performance mirror contains the same sensitive analysis as the
    canonical performance file, so it must be owner-only even when first
    created in an explicit output_dir.
    """
    from ic_engine.pipeline import run_pipeline

    holdings_file = _write_holdings_json(tmp_path)
    output_dir = tmp_path / "reports"
    run_pipeline(str(holdings_file), output_dir=str(output_dir))

    raw_mirror = output_dir / ".raw" / "performance.json"
    assert stat.S_IMODE(raw_mirror.stat().st_mode) == 0o600
