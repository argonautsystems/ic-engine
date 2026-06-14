from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
try:
    import structlog  # noqa: F401
except ModuleNotFoundError:  # bridge tests do not require real structlog
    import types

    structlog = types.ModuleType("structlog")
    structlog.get_logger = lambda *_args, **_kwargs: types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )
    sys.modules["structlog"] = structlog

from ic_engine.commands import performance_window
from ic_engine.commands.performance_window import build_performance_window, resolve_window
from ic_engine.runtime.envelope import validate_envelope


def _holdings_file(tmp_path: Path) -> Path:
    holdings = {
        "holdings": [
            {"symbol": "AAA", "shares": 10, "current_price": 12, "asset_type": "equity"},
            {"symbol": "BBB", "shares": 5, "current_price": 18, "asset_type": "equity"},
        ]
    }
    path = tmp_path / "holdings.json"
    path.write_text(json.dumps(holdings), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "period,start",
    [
        ("1d", "2026-06-13"),
        ("1w", "2026-06-07"),
        ("2w", "2026-05-31"),
        ("1mo", "2026-05-15"),
        ("3mo", "2026-03-16"),
        ("6mo", "2025-12-16"),
        ("ytd", "2026-01-01"),
        ("1y", "2025-06-14"),
        ("2y", "2024-06-14"),
        ("max", "1900-01-01"),
    ],
)
def test_period_resolver_tokens(period, start):
    window = resolve_window(period=period, today=date(2026, 6, 14))
    assert window.start_date == start
    assert window.end_date == "2026-06-14"


def test_max_period_requests_full_provider_history_and_clamps_to_earliest_available(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "perf-window-test-key")
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    seen = {}

    def fake_fetch(self, symbols, start_date, end_date, *, exact_range=False):
        seen["start_date"] = start_date
        seen["end_date"] = end_date
        frame = pd.DataFrame(
            {
                "Date": ["1999-01-04", "2026-06-14"],
                "Close_AAA": [10.0, 12.0],
                "Close_BBB": [20.0, 18.0],
            }
        )
        return pl.from_pandas(frame), {}, symbols

    def fake_returns(self, price_data, symbol, annual_dividend=0.0):
        return np.array([0.20 if symbol == "AAA" else -0.10])

    monkeypatch.setattr(
        "ic_engine.commands.analyze_performance_polars.PerformanceAnalyzer.fetch_equity_data",
        fake_fetch,
    )
    monkeypatch.setattr(
        "ic_engine.commands.analyze_performance_polars.PerformanceAnalyzer.calculate_returns",
        fake_returns,
    )

    envelope = build_performance_window(_holdings_file(tmp_path), period="max", today=date(2026, 6, 14))
    validate_envelope(envelope)
    assert seen["start_date"] == "1900-01-01"
    section = envelope["sections"]["performance_window"]
    assert section["requested_start_date"] == "1900-01-01"
    assert section["start_date"] == "1999-01-04"
    assert section["end_date"] == "2026-06-14"


def test_period_resolver_explicit_dates_pass_through():
    window = resolve_window(start_date="2026-06-01", end_date="2026-06-10", today=date(2026, 6, 14))
    assert window.period == "custom"
    assert window.start_date == "2026-06-01"
    assert window.end_date == "2026-06-10"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"period": "bogus"},
        {"start_date": "2026-06-10", "end_date": "2026-06-01"},
        {"period": "1w", "start_date": "2026-06-01"},
        {"end_date": "2026-06-10"},
    ],
)
def test_period_resolver_invalid_rejected(kwargs):
    with pytest.raises(ValueError):
        resolve_window(today=date(2026, 6, 14), **kwargs)


def test_build_performance_window_reuses_analyzer_total_returns(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "perf-window-test-key")
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    calls = []

    def fake_fetch(self, symbols, start_date, end_date, *, exact_range=False):
        assert start_date == "2026-06-07"
        assert end_date == "2026-06-14"
        frame = pd.DataFrame(
            {
                "Date": ["2026-06-07", "2026-06-14"],
                "Close_AAA": [10.0, 12.0],
                "Close_BBB": [20.0, 18.0],
            }
        )
        return pl.from_pandas(frame), {"AAA": 0.20, "BBB": 0.0}, symbols

    def fake_returns(self, price_data, symbol, annual_dividend=0.0):
        calls.append((symbol, annual_dividend))
        # Deliberately diverge from price-only math: AAA price return would be
        # +20%, but the analyzer total-return path returns +25% here.
        return np.array([0.25 if symbol == "AAA" else -0.05])

    monkeypatch.setattr(
        "ic_engine.commands.analyze_performance_polars.PerformanceAnalyzer.fetch_equity_data",
        fake_fetch,
    )
    monkeypatch.setattr(
        "ic_engine.commands.analyze_performance_polars.PerformanceAnalyzer.calculate_returns",
        fake_returns,
    )
    # Dated dividend events are the authoritative source; mock them so the test
    # is hermetic (no live Massive) and AAA carries a 0.20 in-window dividend.
    monkeypatch.setattr(
        "ic_engine.commands.performance_window_cache._fetch_dividend_events",
        lambda sym, s, e, agg: (
            [{"date": "2026-06-14", "amount": 0.20, "source": "test"}]
            if sym.upper() == "AAA"
            else []
        ),
    )

    envelope = build_performance_window(_holdings_file(tmp_path), period="1w", today=date(2026, 6, 14))
    validate_envelope(envelope)
    section = envelope["sections"]["performance_window"]
    by_symbol = {row["symbol"]: row for row in section["holdings"]}
    assert by_symbol["AAA"]["return_pct"] == 25.0
    assert by_symbol["AAA"]["pnl"] == 25.0
    assert section["totals"]["total_pnl"] == 20.0
    assert section["totals"]["total_return_pct"] == 10.0
    assert ("AAA", 0.20) in calls
    assert ("BBB", 0.0) in calls


def test_router_path_accepts_default_verbose_and_emits_signed_envelope(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "perf-window-test-key")
    monkeypatch.setenv("INVESTORCLAW_OHLCV_PANEL_DIR", str(tmp_path / "panel"))
    monkeypatch.setenv("INVESTOR_CLAW_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("INVESTOR_CLAW_DATED_REPORTS", "false")
    reports_dir = tmp_path / "reports"
    raw_dir = reports_dir / ".raw"
    raw_dir.mkdir(parents=True)
    holdings_path = _holdings_file(raw_dir)

    def fake_fetch(self, symbols, start_date, end_date, *, exact_range=False):
        frame = pd.DataFrame(
            {
                "Date": ["2026-06-07", "2026-06-14"],
                "Close_AAA": [10.0, 12.0],
                "Close_BBB": [20.0, 18.0],
            }
        )
        return pl.from_pandas(frame), {}, symbols

    def fake_returns(self, price_data, symbol, annual_dividend=0.0):
        return np.array([0.20 if symbol == "AAA" else -0.10])

    monkeypatch.setattr(
        "ic_engine.commands.analyze_performance_polars.PerformanceAnalyzer.fetch_equity_data",
        fake_fetch,
    )
    monkeypatch.setattr(
        "ic_engine.commands.analyze_performance_polars.PerformanceAnalyzer.calculate_returns",
        fake_returns,
    )

    from ic_engine.config.path_resolver import get_reports_dir
    from ic_engine.runtime.router import synthesize_args

    args, error_code = synthesize_args(
        "performance-window",
        ["--period", "1w"],
        tmp_path,
    )
    assert error_code == 0
    assert args[0] == str(holdings_path)
    assert "--verbose" in args  # router compatibility regression guard

    assert performance_window.main(args) == 0
    stdout = capsys.readouterr().out.strip().splitlines()
    envelope = json.loads(stdout[-1])
    validate_envelope(envelope)
    assert envelope["ic_result"]["hmac"]
    assert envelope["sections"]["performance_window"]["period"] == "1w"
    assert get_reports_dir() == reports_dir


def test_mcp_tool_unwraps_real_stdout_and_returns_valid_signed_envelope(monkeypatch):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "perf-window-test-key")
    holdings_path = Path(__file__)
    envelope = {
        "schema_version": "v2.5.0",
        "generated_at": "2026-06-14T00:00:00Z",
        "portfolio_id": "test-portfolio",
        "ic_result": {
            "hmac": "",
            "engine_version": "4.8.0",
            "command": "performance-window",
            "run_id": "run-1",
        },
        "sections": {
            "performance_window": {
                "period": "1w",
                "start_date": "2026-06-07",
                "end_date": "2026-06-14",
                "holdings": [],
                "totals": {"total_return_pct": 1.23, "total_pnl": 456.0, "top_movers": []},
            }
        },
        "section_meta": {
            "performance_window": {
                "computed_at": "2026-06-14T00:00:00Z",
                "ttl_seconds": 300,
                "source": "performance_window",
                "status": "success",
            }
        },
        "failed_sections": [],
    }
    from ic_engine.runtime.envelope import attach_hmac

    attach_hmac(envelope)
    validate_envelope(envelope)

    from investorclaw_bridge.mcp.tools import portfolio

    async def fake_run(args, timeout_sec=1800.0):
        assert args == ["performance-window", "--period", "1w"]
        # This mirrors _runtime._run_ic_engine: command JSON appears in the
        # narrative, while the router's trailing {"ic_result": ...} is parsed
        # separately. The tool must unwrap the real stdout shape.
        return {
            "stdout": json.dumps(envelope) + "\n" + json.dumps({"ic_result": {"script": "performance_window.py", "exit_code": 0}}),
            "stderr": "",
            "exit_code": 0,
            "ic_result": {"ic_result": {"script": "performance_window.py", "exit_code": 0}},
            "narrative": json.dumps(envelope),
        }

    monkeypatch.setattr(portfolio, "_run_ic_engine", fake_run)
    result = asyncio.run(portfolio.portfolio_performance_window(period="1w"))
    validate_envelope(result)
    assert result["ic_result"]["hmac"] == envelope["ic_result"]["hmac"]
    assert result["sections"]["performance_window"]["totals"]["total_pnl"] == 456.0


def test_skill_cookbooks_route_temporal_phrases_to_performance_window():
    skill_dir = Path(__file__).resolve().parents[1] / "agent-skills"
    for skill in skill_dir.glob("*/SKILL.md"):
        text = skill.read_text(encoding="utf-8").lower()
        assert "time-window" in text or "historical questions" in text, skill
        assert "portfolio_performance_window" in text, skill
        for phrase in ("last week", "last month", "last quarter", "this year", "ytd", "since date", "last n days"):
            assert phrase in text, f"{skill} missing {phrase} mapping"
        assert "period=1w" in text, skill
        assert "period=1mo" in text, skill
        assert "period=3mo" in text, skill
        assert "period=ytd" in text, skill
        assert "start_date=yyyy-mm-dd" in text, skill
