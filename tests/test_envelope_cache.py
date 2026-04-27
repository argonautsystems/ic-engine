from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ic_engine.runtime.envelope import CANONICAL_SECTIONS, Envelope, attach_hmac, new_ic_result
from ic_engine.runtime.envelope_cache import cache_status, get_or_run, save_envelope


def _holdings_file(tmp_path: Path) -> Path:
    path = tmp_path / "holdings.json"
    path.write_text(json.dumps({"portfolio": {"positions": [{"symbol": "AAPL"}]}}))
    return path


def _envelope(holdings_file: Path, *, computed_at: str | None = None) -> Envelope:
    from ic_engine.runtime.envelope import portfolio_id_for_holdings

    now = computed_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    sections = {section: {"value": section} for section in CANONICAL_SECTIONS}
    meta = {
        section: {
            "computed_at": now,
            "ttl_seconds": 300,
            "source": section,
            "status": "success",
        }
        for section in CANONICAL_SECTIONS
    }
    return attach_hmac(
        {
            "schema_version": "v2.5.0",
            "generated_at": now,
            "portfolio_id": portfolio_id_for_holdings(holdings_file),
            "ic_result": new_ic_result("run_full", "test-run-id"),
            "sections": sections,
            "section_meta": meta,
            "failed_sections": [],
        }
    )


@pytest.fixture(autouse=True)
def _hmac_key(monkeypatch):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "cache-test-key")


def test_get_or_run_uses_fresh_cache(tmp_path, monkeypatch):
    holdings_file = _holdings_file(tmp_path)
    cached = _envelope(holdings_file)
    save_envelope(cached, reports_dir=tmp_path)

    def _unexpected_run(*args, **kwargs):
        raise AssertionError("fresh cache should not run the full pipeline")

    monkeypatch.setattr("ic_engine.runtime.envelope_cache.run_full", _unexpected_run)

    got = get_or_run(holdings_file, reports_dir=tmp_path)

    assert got["ic_result"]["hmac"] == cached["ic_result"]["hmac"]
    assert cache_status(holdings_file, reports_dir=tmp_path)["needs_run"] is False


def test_get_or_run_refreshes_stale_section(tmp_path, monkeypatch):
    holdings_file = _holdings_file(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    cached = _envelope(holdings_file)
    cached["section_meta"]["news"]["computed_at"] = old
    attach_hmac(cached)
    save_envelope(cached, reports_dir=tmp_path)
    refreshed = _envelope(holdings_file)
    calls = []

    def _fake_run(path, ttl_by_section=None):
        calls.append((path, ttl_by_section))
        return refreshed

    monkeypatch.setattr("ic_engine.runtime.envelope_cache.run_full", _fake_run)

    before = cache_status(holdings_file, reports_dir=tmp_path)
    assert before["stale_sections"] == ["news"]
    assert before["missing_sections"] == []

    got = get_or_run(holdings_file, reports_dir=tmp_path)

    assert calls
    assert got["ic_result"]["hmac"] == refreshed["ic_result"]["hmac"]
    status = cache_status(holdings_file, reports_dir=tmp_path)
    assert status["valid"] is True


def test_force_refresh_runs_even_when_cache_is_fresh(tmp_path, monkeypatch):
    holdings_file = _holdings_file(tmp_path)
    save_envelope(_envelope(holdings_file), reports_dir=tmp_path)
    refreshed = _envelope(holdings_file)
    calls = []

    def _fake_run(path, ttl_by_section=None):
        calls.append(path)
        return refreshed

    monkeypatch.setattr("ic_engine.runtime.envelope_cache.run_full", _fake_run)

    got = get_or_run(holdings_file, force_refresh=True, reports_dir=tmp_path)

    assert calls == [holdings_file]
    assert got["ic_result"]["hmac"] == refreshed["ic_result"]["hmac"]
