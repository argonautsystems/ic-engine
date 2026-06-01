# SPDX-License-Identifier: Apache-2.0
"""Tests for provider_routing — primary + fallback chain persistence."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))

from investorclaw_bridge import provider_routing as pr  # noqa: E402


@pytest.fixture
def routing_dir(tmp_path, monkeypatch):
    """Override the routing-file path + clear engine env vars."""
    rfile = tmp_path / "provider_routing.env"
    monkeypatch.setenv("IC_PROVIDER_ROUTING_FILE", str(rfile))
    monkeypatch.delenv("INVESTORCLAW_PRICE_PROVIDER", raising=False)
    monkeypatch.delenv("INVESTORCLAW_FALLBACK_CHAIN", raising=False)
    yield tmp_path


def test_load_routing_unset_returns_auto_default(routing_dir):
    out = pr.load_routing()
    assert out["primary"] == "auto"
    assert out["fallback_chain"] == []
    assert "valid_providers" in out
    # Spot-check the allowlist mirrors ic-engine PROVIDER_CLASSES
    assert "yfinance" in out["valid_providers"]
    assert "massive" in out["valid_providers"]
    assert "marketaux" in out["valid_providers"]


def test_valid_providers_cover_ic_engine_provider_classes():
    try:
        from ic_engine.providers.price_provider import PROVIDER_CLASSES
    except ImportError as exc:
        pytest.skip(f"ic_engine provider registry unavailable: {exc}")

    assert pr._VALID_PROVIDERS.issuperset(PROVIDER_CLASSES.keys())


def test_save_routing_round_trip(routing_dir):
    result = pr.save_routing(primary="massive", fallback_chain=["finnhub", "yfinance"])
    assert result["saved"] is True
    assert result["primary"] == "massive"
    assert result["fallback_chain"] == ["finnhub", "yfinance"]

    # Re-read fresh
    out = pr.load_routing()
    assert out["primary"] == "massive"
    assert out["fallback_chain"] == ["finnhub", "yfinance"]


def test_save_routing_pushes_into_environ(routing_dir):
    pr.save_routing(primary="finnhub", fallback_chain=["yfinance"])
    assert os.environ.get("INVESTORCLAW_PRICE_PROVIDER") == "finnhub"
    assert os.environ.get("INVESTORCLAW_FALLBACK_CHAIN") == "yfinance"


def test_save_routing_auto_removes_primary_env(routing_dir, monkeypatch):
    pr.save_routing(primary="finnhub", fallback_chain=[])
    assert os.environ.get("INVESTORCLAW_PRICE_PROVIDER") == "finnhub"

    pr.save_routing(primary="auto", fallback_chain=[])
    assert "INVESTORCLAW_PRICE_PROVIDER" not in os.environ
    assert "INVESTORCLAW_FALLBACK_CHAIN" not in os.environ


def test_save_routing_empty_chain_clears_env(routing_dir):
    pr.save_routing(primary="auto", fallback_chain=["yfinance", "finnhub"])
    assert os.environ.get("INVESTORCLAW_FALLBACK_CHAIN") == "yfinance,finnhub"
    pr.save_routing(primary="auto", fallback_chain=[])
    assert "INVESTORCLAW_FALLBACK_CHAIN" not in os.environ


def test_save_routing_rejects_unknown_primary(routing_dir):
    result = pr.save_routing(primary="bloomberg-terminal", fallback_chain=[])
    assert result.get("error") == "invalid_primary"
    assert "Unknown provider" in result.get("detail", "")
    # No file written
    assert not Path(os.environ["IC_PROVIDER_ROUTING_FILE"]).exists()


def test_save_routing_rejects_unknown_chain_member(routing_dir):
    result = pr.save_routing(
        primary="massive", fallback_chain=["finnhub", "evil-provider"]
    )
    assert result.get("error") == "invalid_fallback_chain"
    assert "Unknown provider" in result.get("detail", "")


def test_save_routing_normalizes_case(routing_dir):
    result = pr.save_routing(primary="MASSIVE", fallback_chain=["YFinance", "FinnHub"])
    assert result["primary"] == "massive"
    assert result["fallback_chain"] == ["yfinance", "finnhub"]


def test_save_routing_strips_whitespace_in_chain(routing_dir):
    result = pr.save_routing(
        primary="auto", fallback_chain=["  yfinance  ", "", "  finnhub"]
    )
    assert result["fallback_chain"] == ["yfinance", "finnhub"]


def test_save_routing_primary_none_preserves(routing_dir):
    pr.save_routing(primary="finnhub", fallback_chain=["yfinance"])
    pr.save_routing(primary=None, fallback_chain=["massive"])
    out = pr.load_routing()
    assert out["primary"] == "finnhub"
    assert out["fallback_chain"] == ["massive"]


def test_save_routing_chain_none_preserves(routing_dir):
    pr.save_routing(primary="finnhub", fallback_chain=["yfinance"])
    pr.save_routing(primary="massive", fallback_chain=None)
    out = pr.load_routing()
    assert out["primary"] == "massive"
    assert out["fallback_chain"] == ["yfinance"]


def test_save_routing_atomic_no_tmp_after_success(routing_dir):
    pr.save_routing(primary="auto", fallback_chain=["yfinance"])
    rpath = Path(os.environ["IC_PROVIDER_ROUTING_FILE"])
    assert rpath.exists()
    assert not rpath.with_suffix(rpath.suffix + ".tmp").exists()


def test_save_routing_overwrites_cleanly(routing_dir):
    pr.save_routing(primary="finnhub", fallback_chain=["yfinance"])
    pr.save_routing(primary="massive", fallback_chain=["alpha_vantage"])
    out = pr.load_routing()
    assert out["primary"] == "massive"
    assert out["fallback_chain"] == ["alpha_vantage"]
    # Confirm no append behavior — file has exactly one PRIMARY line
    body = Path(os.environ["IC_PROVIDER_ROUTING_FILE"]).read_text()
    assert body.count("INVESTORCLAW_PRICE_PROVIDER=") == 1


def test_load_routing_falls_back_to_environ_when_no_file(routing_dir, monkeypatch):
    # No file written, but compose/quadlet sets the env directly.
    monkeypatch.setenv("INVESTORCLAW_PRICE_PROVIDER", "yfinance")
    monkeypatch.setenv("INVESTORCLAW_FALLBACK_CHAIN", "massive,finnhub")
    out = pr.load_routing()
    assert out["primary"] == "yfinance"
    assert out["fallback_chain"] == ["massive", "finnhub"]


def test_load_routing_skips_invalid_persisted_values(routing_dir, monkeypatch):
    rpath = Path(os.environ["IC_PROVIDER_ROUTING_FILE"])
    rpath.write_text(
        "INVESTORCLAW_PRICE_PROVIDER=bogus\n"
        "INVESTORCLAW_FALLBACK_CHAIN=yfinance,bogus\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INVESTORCLAW_PRICE_PROVIDER", "finnhub")
    monkeypatch.setenv("INVESTORCLAW_FALLBACK_CHAIN", "massive")

    out = pr.load_routing()

    assert out["primary"] == "finnhub"
    assert out["fallback_chain"] == ["massive"]


def test_hydrate_environ_from_file_setdefault_only(routing_dir, monkeypatch):
    pr.save_routing(primary="finnhub", fallback_chain=["yfinance"])
    # Simulate compose-set override at startup
    monkeypatch.setenv("INVESTORCLAW_PRICE_PROVIDER", "compose-set-massive")
    pr.hydrate_environ_from_file()
    # Compose value wins (setdefault doesn't overwrite)
    assert os.environ["INVESTORCLAW_PRICE_PROVIDER"] == "compose-set-massive"


def test_hydrate_environ_from_file_skips_invalid_persisted_values(routing_dir):
    rpath = Path(os.environ["IC_PROVIDER_ROUTING_FILE"])
    rpath.write_text(
        "INVESTORCLAW_PRICE_PROVIDER=bogus\n"
        "INVESTORCLAW_FALLBACK_CHAIN=yfinance,bogus\n",
        encoding="utf-8",
    )

    pr.hydrate_environ_from_file()

    assert "INVESTORCLAW_PRICE_PROVIDER" not in os.environ
    assert "INVESTORCLAW_FALLBACK_CHAIN" not in os.environ


def test_routing_path_respects_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom-routing.env"
    monkeypatch.setenv("IC_PROVIDER_ROUTING_FILE", str(custom))
    assert pr._routing_path() == custom


def test_valid_providers_excludes_auto(routing_dir):
    """'auto' is a control word for primary, not a real provider name."""
    assert "auto" not in pr.valid_providers()


def test_save_routing_atomic_failure_preserves_existing(routing_dir, monkeypatch):
    # First write a known-good config.
    pr.save_routing(primary="finnhub", fallback_chain=["yfinance"])
    rpath = Path(os.environ["IC_PROVIDER_ROUTING_FILE"])
    original = rpath.read_text()

    # Force atomic write to fail.
    def boom(*a, **kw):
        raise OSError("simulated disk full")

    monkeypatch.setattr(pr, "_atomic_write", boom)

    result = pr.save_routing(primary="massive", fallback_chain=["alpha_vantage"])
    assert result.get("error") == "routing_write_failed"
    # Original content unchanged.
    assert rpath.read_text() == original
