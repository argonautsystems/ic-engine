from __future__ import annotations

import json
from pathlib import Path

import pytest

from ic_engine.commands import market_snapshot as ms
from ic_engine.runtime.envelope import validate_envelope


def _holdings_file(tmp_path: Path) -> Path:
    h = {"holdings": [
        {"symbol": "AAA", "shares": 10, "asset_type": "equity"},
        {"symbol": "BBB", "shares": 5, "asset_type": "equity"},
    ]}
    p = tmp_path / "h.json"
    p.write_text(json.dumps(h), encoding="utf-8")
    return p


class _FakeProvider:
    def get_quotes(self, symbols):
        out = {}
        for s in symbols:
            if s in ("AAA", "BBB"):
                out[s] = {"symbol": s, "price": 100.0 if s == "AAA" else 50.0,
                          "change_pct": 3.5 if s == "AAA" else -1.2,
                          "prev_close": 96.6, "open": 99.0, "high": 101.0,
                          "low": 98.0, "volume": 1000, "provider": "fake"}
            elif s == "X:BTCUSD":
                out[s] = {"symbol": s, "price": 66000.0, "change_pct": 1.5,
                          "prev_close": 65000.0, "provider": "fake"}
        return out


@pytest.fixture(autouse=True)
def _hmac(monkeypatch):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "snap-test")


def test_market_snapshot_signed_holdings_and_change(monkeypatch, tmp_path):
    monkeypatch.setattr("ic_engine.providers.price_provider.PriceProvider", lambda: _FakeProvider())
    env = ms.build_market_snapshot(_holdings_file(tmp_path), use_cache=False)
    validate_envelope(env)
    s = env["sections"]["market_snapshot"]
    by = {h["symbol"]: h for h in s["holdings"]}
    assert by["AAA"]["price"] == 100.0 and by["AAA"]["change_pct"] == 3.5
    assert by["BBB"]["change_pct"] == -1.2
    # biggest absolute mover first
    assert s["top_movers"][0]["symbol"] == "AAA"
    # benchmarks best-effort (BTC present in fake)
    assert any(b["symbol"] == "X:BTCUSD" for b in s["benchmarks"])


def test_market_snapshot_explicit_symbols(monkeypatch):
    monkeypatch.setattr("ic_engine.providers.price_provider.PriceProvider", lambda: _FakeProvider())
    env = ms.build_market_snapshot(symbols=["AAA"], benchmarks=False, use_cache=False)
    s = env["sections"]["market_snapshot"]
    assert [h["symbol"] for h in s["holdings"]] == ["AAA"]
    assert s["benchmarks"] == []


def test_market_snapshot_ttl_cache_idempotent(monkeypatch):
    calls = {"n": 0}
    class _Counting(_FakeProvider):
        def get_quotes(self, symbols):
            calls["n"] += 1
            return super().get_quotes(symbols)
    monkeypatch.setattr("ic_engine.providers.price_provider.PriceProvider", lambda: _Counting())
    ms._SNAPSHOT_CACHE.clear()
    e1 = ms.build_market_snapshot(symbols=["AAA"], benchmarks=False)
    e2 = ms.build_market_snapshot(symbols=["AAA"], benchmarks=False)
    assert calls["n"] == 1  # second call served from TTL cache
    assert e1["ic_result"]["hmac"] == e2["ic_result"]["hmac"]
