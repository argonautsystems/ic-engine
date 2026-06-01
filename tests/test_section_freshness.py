# SPDX-License-Identifier: Apache-2.0
"""Tests for section_freshness — per-section JSON staleness detection."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from structlog.testing import capture_logs

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))

from investorclaw_bridge import section_freshness as sf  # noqa: E402
from investorclaw_bridge import serve  # noqa: E402


def _seed(reports: Path, files: dict[str, float]) -> None:
    """Create files in `reports` with the given mtime overrides.

    `files` maps filename → mtime (epoch seconds). A None mtime
    leaves the file at default mtime (now).
    """
    reports.mkdir(parents=True, exist_ok=True)
    for fname, mtime in files.items():
        path = reports / fname
        path.write_text("{}")
        if mtime is not None:
            os.utime(path, (mtime, mtime))


def test_empty_dir_flags_core_sections_stale(tmp_path):
    out = sf.stale_sections(reports_dir=tmp_path, max_age_hours=24)
    assert out["any_core_stale"] is True
    # All 8 core sections flagged as missing+stale; optional sections aren't.
    stale = set(out["stale_names"])
    core = {"holdings", "performance", "bonds", "analyst", "news",
            "whatchanged", "scenario", "synthesize"}
    assert core <= stale, f"missing core stale: {core - stale}"
    optional = {"cashflow", "peer", "markets", "optimize", "rebalance"}
    assert not (optional & stale), (
        f"optional sections incorrectly flagged: {optional & stale}"
    )


def test_fresh_files_not_stale(tmp_path):
    now = time.time()
    _seed(tmp_path, {
        "holdings_summary.json": now,
        "performance.json": now - 60,
        "bond_analysis.json": now - 600,
        "analyst_recommendations_summary.json": now - 1000,
        "portfolio_news.json": now - 5,
        "whatchanged.json": now,
        "scenario.json": now,
        "portfolio_analysis.json": now,
    })
    out = sf.stale_sections(reports_dir=tmp_path, max_age_hours=24)
    assert out["any_core_stale"] is False
    assert out["stale_count"] == 0


def test_old_files_flagged_stale(tmp_path):
    too_old = time.time() - (25 * 3600)  # 25h old
    _seed(tmp_path, {
        "holdings_summary.json": too_old,
        "performance.json": too_old,
        "bond_analysis.json": too_old,
        "analyst_recommendations_summary.json": too_old,
        "portfolio_news.json": too_old,
        "whatchanged.json": too_old,
        "scenario.json": too_old,
        "portfolio_analysis.json": too_old,
    })
    out = sf.stale_sections(reports_dir=tmp_path, max_age_hours=24)
    assert out["any_core_stale"] is True
    assert out["stale_count"] >= 8


def test_partial_staleness_some_fresh_some_old(tmp_path):
    now = time.time()
    too_old = now - (30 * 3600)
    _seed(tmp_path, {
        "holdings_summary.json": now,
        "performance.json": now,
        "bond_analysis.json": now,
        "analyst_recommendations_summary.json": now,
        "portfolio_news.json": too_old,  # this one stale
        "whatchanged.json": now,
        "scenario.json": now,
        "portfolio_analysis.json": now,
    })
    out = sf.stale_sections(reports_dir=tmp_path, max_age_hours=24)
    assert out["any_core_stale"] is True
    assert "news" in out["stale_names"]
    assert "holdings" not in out["stale_names"]


def test_optional_section_missing_does_not_trigger_sweep(tmp_path):
    """A bond-less portfolio has no cashflow.json — that should NOT
    trigger a regenerate sweep (cashflow is an optional section).
    """
    now = time.time()
    _seed(tmp_path, {
        "holdings_summary.json": now,
        "performance.json": now,
        "bond_analysis.json": now,
        "analyst_recommendations_summary.json": now,
        "portfolio_news.json": now,
        "whatchanged.json": now,
        "scenario.json": now,
        "portfolio_analysis.json": now,
        # No cashflow.json — bond-less portfolio
    })
    should_sweep, report = sf.should_run_full_sweep(
        reports_dir=tmp_path, max_age_hours=24
    )
    assert should_sweep is False
    assert report["any_core_stale"] is False


def test_optional_section_stale_when_present(tmp_path):
    """If an optional section file EXISTS but is old, it's still
    flagged stale (in stale_names) but doesn't trigger sweep
    on its own (would need a core section to also be stale).
    """
    now = time.time()
    too_old = now - (30 * 3600)
    _seed(tmp_path, {
        "holdings_summary.json": now,
        "performance.json": now,
        "bond_analysis.json": now,
        "analyst_recommendations_summary.json": now,
        "portfolio_news.json": now,
        "whatchanged.json": now,
        "scenario.json": now,
        "portfolio_analysis.json": now,
        "cashflow.json": too_old,  # optional + stale
    })
    out = sf.stale_sections(reports_dir=tmp_path, max_age_hours=24)
    assert "cashflow" in out["stale_names"]
    assert out["any_core_stale"] is False


def test_should_run_full_sweep_with_typhon_pattern(tmp_path):
    """Reproduce the actual TYPHON state — most sections from
    yesterday's 21:00 cron, only analyst_recommendations refreshed today.
    Sweep MUST trigger."""
    now = time.time()
    yesterday = now - (20 * 3600)  # 20h old
    _seed(tmp_path, {
        "holdings_summary.json": yesterday,
        "performance.json": yesterday,
        "bond_analysis.json": yesterday,
        "analyst_recommendations_summary.json": now,  # refreshed today
        "portfolio_news.json": yesterday,
        "whatchanged.json": yesterday,
        "scenario.json": yesterday,
        "portfolio_analysis.json": yesterday,
    })
    # At 18h cutoff (TYPHON pattern), 20h-old sections are stale
    should_sweep, report = sf.should_run_full_sweep(
        reports_dir=tmp_path, max_age_hours=18
    )
    assert should_sweep is True
    assert "holdings" in report["stale_names"]
    assert "analyst" not in report["stale_names"]


def test_reports_dir_defaults_to_env_or_data(monkeypatch, tmp_path):
    monkeypatch.setenv("IC_REPORTS_DIR", str(tmp_path / "alt"))
    assert sf._reports_dir() == tmp_path / "alt"
    monkeypatch.delenv("IC_REPORTS_DIR")
    assert sf._reports_dir() == Path("/data/reports")


def test_age_hours_rounded_to_2dp(tmp_path):
    older = time.time() - (5 * 3600 + 17)  # ~5.005 hours
    _seed(tmp_path, {"holdings_summary.json": older})
    out = sf.stale_sections(reports_dir=tmp_path, max_age_hours=24)
    age = out["sections"]["holdings"]["age_hours"]
    assert age is not None
    # Verify rounded to 2 decimal places (no precision noise)
    assert abs(age - round(age, 2)) < 1e-9
    # Roughly 5h
    assert 4.99 < age < 5.02


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc", 24.0),
        ("0", 24.0),
        ("-5", 24.0),
        ("inf", 24.0),
        ("nan", 24.0),
        ("12.5", 12.5),
    ],
)
def test_parse_stale_hours(raw, expected):
    assert serve._parse_stale_hours(raw) == expected


def test_parse_stale_hours_parse_failed_logs_reason():
    with capture_logs() as events:
        assert serve._parse_stale_hours("abc") == 24.0

    assert events[-1]["event"] == "bridge.section_freshness.invalid_stale_hours"
    assert events[-1]["reason"] == "parse_failed"


def test_zero_byte_core_section_is_stale(tmp_path):
    (tmp_path / "holdings_summary.json").touch()
    out = sf.stale_sections(reports_dir=tmp_path, max_age_hours=24)
    holdings = out["sections"]["holdings"]
    assert holdings["exists"] is True
    assert holdings["stale"] is True
    assert "holdings" in out["stale_names"]


def test_future_mtime_core_section_is_stale_with_zero_age(tmp_path):
    future = time.time() + 3600
    _seed(tmp_path, {"holdings_summary.json": future})
    out = sf.stale_sections(reports_dir=tmp_path, max_age_hours=24)
    holdings = out["sections"]["holdings"]
    assert holdings["stale"] is True
    assert holdings["age_hours"] == 0.0


def test_core_section_stat_oserror_is_stale(monkeypatch, tmp_path):
    now = time.time()
    _seed(tmp_path, {
        "holdings_summary.json": now,
        "performance.json": now,
        "bond_analysis.json": now,
        "analyst_recommendations_summary.json": now,
        "portfolio_news.json": now,
        "whatchanged.json": now,
        "scenario.json": now,
        "portfolio_analysis.json": now,
    })
    original_stat = Path.stat

    def flaky_stat(self):
        if self.name == "holdings_summary.json":
            raise OSError("permission denied")
        return original_stat(self)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    out = sf.stale_sections(reports_dir=tmp_path, max_age_hours=24)
    assert out["sections"]["holdings"]["stale"] is True
    assert "holdings" in out["stale_names"]


@pytest.mark.asyncio
async def test_concurrent_regenerate_sweep_invokes_engine_once():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_engine(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        raise RuntimeError("stop after first subprocess")

    first = asyncio.create_task(serve._regenerate_sweep(slow_engine))
    await started.wait()
    second = await serve._regenerate_sweep(slow_engine)
    release.set()
    first_result = await asyncio.gather(first, return_exceptions=True)

    assert calls == 1
    assert isinstance(first_result[0], RuntimeError)
    assert second["setup"]["status"] == "already_running"
    assert second["setup"]["skipped"] is True
    assert serve.is_sweeping() is False


@pytest.mark.asyncio
async def test_portfolio_refresh_skips_engine_when_sweep_already_running(monkeypatch):
    from investorclaw_bridge.mcp.tools import portfolio

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_engine(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        raise RuntimeError("stop after first subprocess")

    async def refresh_engine(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {"refresh": True}

    monkeypatch.setattr(portfolio, "_run_ic_engine", refresh_engine)
    first = asyncio.create_task(serve._regenerate_sweep(slow_engine))
    await started.wait()
    try:
        second = await portfolio.portfolio_refresh()
    finally:
        release.set()

    first_result = await asyncio.gather(first, return_exceptions=True)

    assert calls == 1
    assert isinstance(first_result[0], RuntimeError)
    assert second["refresh"]["status"] == "already_running"
    assert second["refresh"]["skipped"] is True
    assert serve.is_sweeping() is False
