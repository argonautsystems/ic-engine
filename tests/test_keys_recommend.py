# SPDX-License-Identifier: Apache-2.0
"""Tests for portfolio_keys_recommend — size-aware key guidance (#44)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from investorclaw_bridge.mcp.tools import keys as keys_module  # noqa: E402
from investorclaw_bridge.mcp.tools.keys import (  # noqa: E402
    _LARGE_PORTFOLIO_THRESHOLD,
    _HUGE_PORTFOLIO_THRESHOLD,
    _count_portfolio_holdings,
    _key_recommendations,
    portfolio_keys_recommend,
)


# ── _count_portfolio_holdings ────────────────────────────────────────


def _write_csv(path: Path, n: int) -> Path:
    """Write a tiny CSV with n holdings rows."""
    rows = ["Symbol,Quantity,Price"]
    for i in range(n):
        rows.append(f"SYM{i:04d},10,100.00")
    path.write_text("\n".join(rows) + "\n")
    return path


def test_count_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert _count_portfolio_holdings(str(tmp_path / "missing.csv")) is None


def test_count_returns_zero_for_empty_csv(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("Symbol,Quantity,Price\n")
    assert _count_portfolio_holdings(str(p)) == 0


@pytest.mark.parametrize("n", [1, 25, 50, 100, 250])
def test_count_matches_row_count(tmp_path: Path, n: int) -> None:
    p = _write_csv(tmp_path / f"p{n}.csv", n)
    assert _count_portfolio_holdings(str(p)) == n


def test_count_handles_utf8_bom(tmp_path: Path) -> None:
    """Excel-on-Windows prepends BOM; sizer must strip it."""
    p = tmp_path / "bom.csv"
    content = "﻿Symbol,Quantity,Price\nAAPL,10,150.00\n"
    p.write_bytes(content.encode("utf-8"))
    assert _count_portfolio_holdings(str(p)) == 1


# ── _key_recommendations priority logic ──────────────────────────────


def _by_name(recs: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in recs}


def test_recommendations_small_portfolio_makes_massive_optional() -> None:
    """20 holdings is well below the 50-threshold; MASSIVE is optional."""
    block = _key_recommendations(holdings_count=20)
    by_name = _by_name(block["keys"])
    assert by_name["MASSIVE_API_KEY"]["priority"] == "optional"


def test_recommendations_large_portfolio_makes_massive_recommended() -> None:
    """At the 50-threshold MASSIVE crosses to recommended."""
    block = _key_recommendations(holdings_count=_LARGE_PORTFOLIO_THRESHOLD)
    by_name = _by_name(block["keys"])
    assert by_name["MASSIVE_API_KEY"]["priority"] == "recommended"


def test_recommendations_huge_portfolio_makes_massive_strongly_recommended() -> None:
    """At the 100-threshold MASSIVE crosses to strongly_recommended."""
    block = _key_recommendations(holdings_count=_HUGE_PORTFOLIO_THRESHOLD)
    by_name = _by_name(block["keys"])
    assert by_name["MASSIVE_API_KEY"]["priority"] == "strongly_recommended"


def test_recommendations_unknown_size_makes_massive_optional() -> None:
    """When holdings_count is None we can't reason about size — MASSIVE is optional."""
    block = _key_recommendations(holdings_count=None)
    by_name = _by_name(block["keys"])
    assert by_name["MASSIVE_API_KEY"]["priority"] == "optional"


def test_recommendations_together_always_strongly_recommended() -> None:
    """TOGETHER_API_KEY is the narrator endpoint; without it ic-engine
    falls back to the heuristic narrator (catalog-style answers).
    Always strongly_recommended."""
    for size in [None, 5, 50, 200, 1000]:
        block = _key_recommendations(holdings_count=size)
        by_name = _by_name(block["keys"])
        assert by_name["TOGETHER_API_KEY"]["priority"] == "strongly_recommended", (
            f"size={size}: TOGETHER must always be strongly_recommended"
        )


def test_recommendations_news_keys_recommended() -> None:
    """News providers are `recommended` regardless of size."""
    block = _key_recommendations(holdings_count=20)
    by_name = _by_name(block["keys"])
    for k in ("FINNHUB_KEY", "MARKETAUX_API_KEY", "NEWSAPI_KEY", "ALPHA_VANTAGE_KEY"):
        assert by_name[k]["priority"] == "recommended", f"{k} priority mismatch"


def test_recommendations_fred_optional() -> None:
    block = _key_recommendations(holdings_count=20)
    by_name = _by_name(block["keys"])
    assert by_name["FRED_API_KEY"]["priority"] == "optional"


def test_recommendations_each_has_signup_url_and_reason() -> None:
    """Every recommendation must carry signup_url + reason — the dashboard
    surfaces both."""
    block = _key_recommendations(holdings_count=200)
    for entry in block["keys"]:
        assert entry.get("signup_url"), f"{entry['name']} missing signup_url"
        assert entry.get("reason"), f"{entry['name']} missing reason"


# ── End-to-end portfolio_keys_recommend handler ──────────────────────


@pytest.mark.asyncio
async def test_recommend_uses_explicit_portfolio_path(tmp_path: Path, monkeypatch) -> None:
    p = _write_csv(tmp_path / "test.csv", 75)
    # Stub _read_existing so we don't depend on the real /data/keys.env.
    monkeypatch.setattr(keys_module, "_read_existing", lambda: {"TOGETHER_API_KEY": "abc"})
    result = await portfolio_keys_recommend(portfolio_path=str(p))
    assert result["holdings_count"] == 75
    by_name = _by_name(result["recommendations"])
    # 75 holdings is in the 50-99 band → MASSIVE recommended.
    assert by_name["MASSIVE_API_KEY"]["priority"] == "recommended"
    # TOGETHER configured marker propagates from _read_existing.
    assert by_name["TOGETHER_API_KEY"]["configured"] is True
    assert by_name["MASSIVE_API_KEY"]["configured"] is False


@pytest.mark.asyncio
async def test_recommend_no_active_portfolio(tmp_path: Path, monkeypatch) -> None:
    """When /data/portfolios is empty, holdings_count is None and MASSIVE
    is optional (but TOGETHER still strongly_recommended)."""
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path))
    monkeypatch.setattr(keys_module, "_read_existing", lambda: {})
    result = await portfolio_keys_recommend()
    assert result["portfolio_path"] is None
    assert result["holdings_count"] is None
    by_name = _by_name(result["recommendations"])
    assert by_name["TOGETHER_API_KEY"]["priority"] == "strongly_recommended"
    assert by_name["MASSIVE_API_KEY"]["priority"] == "optional"


@pytest.mark.asyncio
async def test_recommend_picks_most_recent_portfolio(tmp_path: Path, monkeypatch) -> None:
    """When multiple CSVs exist, the most-recently-modified wins."""
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path))
    monkeypatch.setattr(keys_module, "_read_existing", lambda: {})
    old = _write_csv(tmp_path / "old.csv", 10)
    new = _write_csv(tmp_path / "new.csv", 200)
    # Force `new` to be more-recently-modified.
    import os as _os
    _os.utime(old, (1000000, 1000000))
    _os.utime(new, (2000000, 2000000))
    result = await portfolio_keys_recommend()
    assert result["holdings_count"] == 200
    by_name = _by_name(result["recommendations"])
    # 200 ≥ HUGE_THRESHOLD → MASSIVE strongly_recommended.
    assert by_name["MASSIVE_API_KEY"]["priority"] == "strongly_recommended"
