# SPDX-License-Identifier: Apache-2.0
"""Tests for provider_diagnostics — per-provider health checks."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))

from investorclaw_bridge import provider_diagnostics as pd  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Helpers — mock the network layer
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_http_get(monkeypatch):
    """Replace _http_get with a callable that returns canned responses.

    Usage:
        async def fake(url, headers=None):
            return (200, {"chart": {"result": [...]}}, None)
        mock_http_get(fake)
    """
    def _install(fake_fn):
        monkeypatch.setattr(pd, "_http_get", fake_fn)
    return _install


@pytest.fixture
def isolated_keys(monkeypatch, tmp_path):
    """Override IC_KEYS_FILE + clear all provider env keys."""
    monkeypatch.setenv("IC_KEYS_FILE", str(tmp_path / "keys.env"))
    for env_name in [
        "FINNHUB_KEY", "MASSIVE_API_KEY", "ALPHA_VANTAGE_KEY",
        "NEWSAPI_KEY", "MARKETAUX_API_KEY",
    ]:
        monkeypatch.delenv(env_name, raising=False)
    yield tmp_path


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────


def test_supported_providers_includes_all_expected():
    expected = {
        "yfinance", "frankfurter", "treasury_fiscaldata",
        "finnhub", "massive", "alpha_vantage", "newsapi", "marketaux",
    }
    assert set(pd.supported_providers()) == expected


@pytest.mark.asyncio
async def test_check_provider_unknown_returns_error():
    result = await pd.check_provider("bogus")
    assert result["ok"] is False
    assert result["error"] == "unknown_provider"
    assert "supported" in result


@pytest.mark.asyncio
async def test_yfinance_ok(mock_http_get):
    async def fake(url, headers=None):
        return 200, {
            "chart": {
                "result": [{
                    "meta": {"regularMarketPrice": 184.32, "symbol": "AAPL"}
                }]
            }
        }, None
    mock_http_get(fake)
    out = await pd.check_yfinance()
    assert out["ok"] is True
    assert out["configured"] is True
    assert out["status_code"] == 200
    assert "184.32" in (out["response_sample"] or "")
    assert out["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_yfinance_timeout(mock_http_get):
    async def fake(url, headers=None):
        return 0, None, "timeout after 5.0s"
    mock_http_get(fake)
    out = await pd.check_yfinance()
    assert out["ok"] is False
    assert out["error"] == "timeout after 5.0s"


@pytest.mark.asyncio
async def test_finnhub_unconfigured(isolated_keys):
    out = await pd.check_finnhub()
    assert out["ok"] is False
    assert out["configured"] is False
    assert "FINNHUB_KEY" in out["error"]


@pytest.mark.asyncio
async def test_finnhub_ok_with_env_key(monkeypatch, mock_http_get):
    monkeypatch.setenv("FINNHUB_KEY", "fh_test_key")
    async def fake(url, headers=None, *, params=None):
        assert url == "https://finnhub.io/api/v1/quote"
        assert params == {"symbol": "AAPL", "token": "fh_test_key"}
        return 200, {"c": 184.32, "h": 185.0, "l": 183.0}, None
    mock_http_get(fake)
    out = await pd.check_finnhub()
    assert out["ok"] is True
    assert "c (current)=184.32" in (out["response_sample"] or "")


@pytest.mark.asyncio
async def test_finnhub_401_invalid_key(monkeypatch, mock_http_get):
    monkeypatch.setenv("FINNHUB_KEY", "bad_key")
    async def fake(url, headers=None, *, params=None):
        return 401, {"error": "Invalid API key"}, None
    mock_http_get(fake)
    out = await pd.check_finnhub()
    assert out["ok"] is False
    assert "401" in out["error"]
    assert "key may be invalid" in out["error"]


@pytest.mark.asyncio
async def test_massive_ok(monkeypatch, mock_http_get):
    monkeypatch.setenv("MASSIVE_API_KEY", "mv_test")
    async def fake(url, headers=None, *, params=None):
        assert url == "https://api.massive.com/v2/aggs/ticker/AAPL/prev"
        assert params == {"adjusted": "true", "apiKey": "mv_test"}
        return 200, {
            "status": "OK",
            "results": [{"c": 184.32, "v": 50_000_000}],
        }, None
    mock_http_get(fake)
    out = await pd.check_massive()
    assert out["ok"] is True
    assert "184.32" in (out["response_sample"] or "")


@pytest.mark.asyncio
async def test_alpha_vantage_quota_exhausted(monkeypatch, mock_http_get):
    monkeypatch.setenv("ALPHA_VANTAGE_KEY", "av_test")
    async def fake(url, headers=None, *, params=None):
        # AlphaVantage returns 200 + a Note when rate-limited
        return 200, {"Note": "5 requests per minute limit reached"}, None
    mock_http_get(fake)
    out = await pd.check_alpha_vantage()
    assert out["ok"] is False
    assert "rate-limited" in out["error"]


@pytest.mark.asyncio
async def test_newsapi_invalid_key_status(monkeypatch, mock_http_get):
    monkeypatch.setenv("NEWSAPI_KEY", "newsapi_bad")
    async def fake(url, headers=None, *, params=None):
        return 401, {"status": "error", "code": "apiKeyInvalid"}, None
    mock_http_get(fake)
    out = await pd.check_newsapi()
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_marketaux_unconfigured(isolated_keys):
    out = await pd.check_marketaux()
    assert out["ok"] is False
    assert out["configured"] is False
    assert "MARKETAUX_API_KEY" in out["error"]


@pytest.mark.asyncio
async def test_frankfurter_no_key_required_ok(mock_http_get):
    async def fake(url, headers=None):
        return 200, {"base": "EUR", "rates": {"USD": 1.08, "GBP": 0.86}}, None
    mock_http_get(fake)
    out = await pd.check_frankfurter()
    assert out["ok"] is True
    assert "base=EUR" in (out["response_sample"] or "")
    assert "rates_count=2" in (out["response_sample"] or "")


@pytest.mark.asyncio
async def test_treasury_fiscaldata_ok(mock_http_get):
    async def fake(url, headers=None):
        return 200, {"data": [{"avg_interest_rate_amt": "4.32"}]}, None
    mock_http_get(fake)
    out = await pd.check_treasury_fiscaldata()
    assert out["ok"] is True


@pytest.mark.asyncio
async def test_check_provider_dispatches_to_named_check(monkeypatch, mock_http_get):
    monkeypatch.setenv("FINNHUB_KEY", "k")
    async def fake(url, headers=None, *, params=None):
        return 200, {"c": 100}, None
    mock_http_get(fake)
    out = await pd.check_provider("finnhub")
    assert out["ok"] is True
    assert out["provider"] == "finnhub"


@pytest.mark.asyncio
async def test_check_provider_catches_unhandled_exceptions(monkeypatch):
    async def boom():
        raise RuntimeError("simulated")
    monkeypatch.setitem(pd.CHECKS, "yfinance", boom)
    out = await pd.check_provider("yfinance")
    assert out["ok"] is False
    assert "RuntimeError" in out["error"]


def test_resolve_key_falls_back_to_keys_file(isolated_keys):
    keys_file = isolated_keys / "keys.env"
    keys_file.write_text("FINNHUB_KEY=from_file_value\n")
    keys_file.chmod(0o600)
    val = pd._resolve_key("FINNHUB_KEY")
    assert val == "from_file_value"


def test_resolve_key_env_wins_over_file(isolated_keys, monkeypatch):
    keys_file = isolated_keys / "keys.env"
    keys_file.write_text("FINNHUB_KEY=from_file\n")
    keys_file.chmod(0o600)
    monkeypatch.setenv("FINNHUB_KEY", "from_env")
    assert pd._resolve_key("FINNHUB_KEY") == "from_env"


def test_resolve_key_returns_none_when_unset(isolated_keys):
    assert pd._resolve_key("NEVER_SET_KEY") is None


@pytest.mark.asyncio
async def test_world_readable_keys_file_is_not_used(isolated_keys):
    keys_file = isolated_keys / "keys.env"
    keys_file.write_text("FINNHUB_KEY=from_world_readable_file\n")
    keys_file.chmod(0o644)

    assert pd._resolve_key("FINNHUB_KEY") is None

    out = await pd.check_finnhub()
    assert out["ok"] is False
    assert out["configured"] is False
    assert "FINNHUB_KEY" in out["error"]


@pytest.mark.parametrize("special_key", ["abc&injected=true", "abc=def", "abc#fragment"])
@pytest.mark.parametrize(
    ("env_name", "key_param", "check_fn", "expected_url", "expected_static", "body"),
    [
        (
            "FINNHUB_KEY",
            "token",
            pd.check_finnhub,
            "https://finnhub.io/api/v1/quote",
            {"symbol": "AAPL"},
            {"c": 184.32},
        ),
        (
            "MASSIVE_API_KEY",
            "apiKey",
            pd.check_massive,
            "https://api.massive.com/v2/aggs/ticker/AAPL/prev",
            {"adjusted": "true"},
            {"status": "OK", "results": [{"c": 184.32, "v": 1}]},
        ),
        (
            "ALPHA_VANTAGE_KEY",
            "apikey",
            pd.check_alpha_vantage,
            "https://www.alphavantage.co/query",
            {"function": "GLOBAL_QUOTE", "symbol": "AAPL"},
            {"Global Quote": {"01. symbol": "AAPL", "05. price": "184.32"}},
        ),
        (
            "NEWSAPI_KEY",
            "apiKey",
            pd.check_newsapi,
            "https://newsapi.org/v2/top-headlines",
            {"country": "us", "pageSize": "1"},
            {"status": "ok", "articles": []},
        ),
        (
            "MARKETAUX_API_KEY",
            "api_token",
            pd.check_marketaux,
            "https://api.marketaux.com/v1/news/all",
            {"limit": "1"},
            {"data": []},
        ),
    ],
)
@pytest.mark.asyncio
async def test_key_provider_urls_pass_keys_as_encoded_params(
    monkeypatch,
    mock_http_get,
    special_key,
    env_name,
    key_param,
    check_fn,
    expected_url,
    expected_static,
    body,
):
    monkeypatch.setenv(env_name, special_key)
    captured: dict[str, object] = {}

    async def fake(url, headers=None, *, params=None):
        captured["url"] = url
        captured["params"] = params
        return 200, body, None

    mock_http_get(fake)
    out = await check_fn()
    assert out["ok"] is True
    assert captured["url"] == expected_url
    assert "?" not in captured["url"]
    assert captured["params"] == {**expected_static, key_param: special_key}
    assert captured["params"][key_param] == special_key
