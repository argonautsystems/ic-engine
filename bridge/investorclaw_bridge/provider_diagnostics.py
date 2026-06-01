# SPDX-License-Identifier: Apache-2.0
"""Provider connectivity diagnostics — health-check each price/news provider.

Lets the user verify that a configured provider key actually works
end-to-end before trusting it for portfolio_refresh / regenerate. Each
provider has a hand-picked, lightweight test endpoint:

  - **No-key providers** (yfinance, frankfurter, treasury_fiscaldata):
    Always testable; hits a known-public endpoint.
  - **Key-required providers** (finnhub, massive, alpha_vantage,
    newsapi, marketaux): Resolves the key from /data/keys.env at call
    time; reports `unconfigured` if missing.

The dashboard surfaces per-provider "Test connection" buttons. Tests
fire on demand only — no automatic background polling — to avoid
burning quota on rate-limited free tiers (NewsAPI 100/day,
AlphaVantage 5/min, MarketAux 100/day).

Each check returns:
    {
        "provider": "finnhub",
        "ok": true,
        "configured": true,
        "latency_ms": 234,
        "status_code": 200,
        "error": null,
        "response_sample": "AAPL price=$184.32",
        "checked_at": "2026-05-08T01:30:00Z",
    }

On failure (network, timeout, HTTP error, response-shape mismatch):
    {
        "provider": "finnhub",
        "ok": false,
        ...
        "error": "HTTP 401 Unauthorized — key may be invalid or expired",
    }
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Callable

import structlog

from .key_resolver import KeysFileTooPermissiveError, load_keys_env

logger = structlog.get_logger("investorclaw_bridge.diagnostics")

# 5-second per-check timeout. Long enough to tolerate slow free-tier
# providers; short enough that a hung diagnostic doesn't block the
# dashboard render.
_CHECK_TIMEOUT = 5.0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _resolve_key(env_name: str) -> str | None:
    """Look up an API key from os.environ first (mirrored at bridge
    startup from /data/keys.env), then fall back to reading the file
    directly in case of out-of-band edits.
    """
    val = os.environ.get(env_name)
    if val:
        return val
    keys_file = os.environ.get("IC_KEYS_FILE", "/data/keys.env")
    try:
        return load_keys_env(Path(keys_file)).get(env_name)
    except KeysFileTooPermissiveError as e:
        logger.warning(
            "diagnostics.keys_file_too_permissive",
            path=keys_file,
            env_name=env_name,
            error=str(e),
        )
        return None
    except Exception:
        pass
    return None


def _ok_response_sample(provider: str, payload: Any) -> str:
    """Extract a small, human-readable sample from the response body
    so the user sees evidence the provider really answered with data.
    """
    try:
        if provider == "yfinance":
            chart = (payload.get("chart") or {}).get("result") or []
            if chart:
                meta = chart[0].get("meta") or {}
                price = meta.get("regularMarketPrice")
                sym = meta.get("symbol", "AAPL")
                if price is not None:
                    return f"{sym} regularMarketPrice=${price}"
        elif provider == "finnhub":
            c = payload.get("c")
            if c is not None:
                return f"AAPL c (current)={c}"
        elif provider == "massive":
            results = payload.get("results") or []
            if results:
                r = results[0]
                return f"AAPL prev close c={r.get('c')} v={r.get('v')}"
            return f"status={payload.get('status')}"
        elif provider == "alpha_vantage":
            quote = payload.get("Global Quote") or {}
            price = quote.get("05. price")
            if price:
                return f"AAPL 05. price={price}"
        elif provider == "newsapi":
            articles = payload.get("articles") or []
            return f"status={payload.get('status')} articles={len(articles)}"
        elif provider == "marketaux":
            data = payload.get("data") or []
            return f"data items={len(data)}"
        elif provider == "frankfurter":
            base = payload.get("base", "?")
            rates = payload.get("rates") or {}
            return f"base={base} rates_count={len(rates)}"
        elif provider == "treasury_fiscaldata":
            data = payload.get("data") or []
            return f"data rows={len(data)}"
    except Exception:
        pass
    return "ok"


async def _http_get(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    params: dict[str, str] | None = None,
) -> tuple[int, Any, str | None]:
    """GET a URL and return (status_code, parsed_json, error_or_none)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=_CHECK_TIMEOUT) as client:
            r = await client.get(url, headers=headers or {}, params=params)
            try:
                payload = r.json()
            except ValueError:
                payload = None
            return r.status_code, payload, None
    except httpx.TimeoutException:
        return 0, None, f"timeout after {_CHECK_TIMEOUT}s"
    except httpx.HTTPError as e:
        return 0, None, f"{type(e).__name__}: {e}"
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


async def check_yfinance() -> dict[str, Any]:
    t0 = time.monotonic()
    sc, body, err = await _http_get(
        "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d",
        headers={"User-Agent": "Mozilla/5.0 InvestorClaw"},
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    if err:
        return _err("yfinance", True, latency_ms, sc, err)
    if sc != 200 or not isinstance(body, dict):
        return _err("yfinance", True, latency_ms, sc,
                    f"HTTP {sc} (no data)")
    return _ok("yfinance", True, latency_ms, sc, body)


async def check_frankfurter() -> dict[str, Any]:
    t0 = time.monotonic()
    sc, body, err = await _http_get("https://api.frankfurter.app/latest")
    latency_ms = int((time.monotonic() - t0) * 1000)
    if err:
        return _err("frankfurter", True, latency_ms, sc, err)
    if sc != 200 or not isinstance(body, dict) or "rates" not in body:
        return _err("frankfurter", True, latency_ms, sc,
                    "no rates in response")
    return _ok("frankfurter", True, latency_ms, sc, body)


async def check_treasury_fiscaldata() -> dict[str, Any]:
    t0 = time.monotonic()
    sc, body, err = await _http_get(
        "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
        "v2/accounting/od/avg_interest_rates?page[size]=1",
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    if err:
        return _err("treasury_fiscaldata", True, latency_ms, sc, err)
    if sc != 200 or not isinstance(body, dict):
        return _err("treasury_fiscaldata", True, latency_ms, sc,
                    f"HTTP {sc}")
    return _ok("treasury_fiscaldata", True, latency_ms, sc, body)


async def check_finnhub() -> dict[str, Any]:
    key = _resolve_key("FINNHUB_KEY")
    if not key:
        return _unconfigured("finnhub", "FINNHUB_KEY")
    t0 = time.monotonic()
    sc, body, err = await _http_get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": "AAPL", "token": key},
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    if err:
        return _err("finnhub", True, latency_ms, sc, err)
    if sc != 200:
        return _err("finnhub", True, latency_ms, sc,
                    f"HTTP {sc} — key may be invalid")
    if not isinstance(body, dict) or body.get("c") is None:
        return _err("finnhub", True, latency_ms, sc,
                    "no `c` (current price) in response")
    return _ok("finnhub", True, latency_ms, sc, body)


async def check_massive() -> dict[str, Any]:
    key = _resolve_key("MASSIVE_API_KEY")
    if not key:
        return _unconfigured("massive", "MASSIVE_API_KEY")
    t0 = time.monotonic()
    sc, body, err = await _http_get(
        "https://api.massive.com/v2/aggs/ticker/AAPL/prev",
        params={"adjusted": "true", "apiKey": key},
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    if err:
        return _err("massive", True, latency_ms, sc, err)
    if sc != 200:
        return _err("massive", True, latency_ms, sc,
                    f"HTTP {sc} — key may be invalid or out of quota")
    if not isinstance(body, dict) or body.get("status") not in ("OK", "DELAYED"):
        return _err("massive", True, latency_ms, sc,
                    f"status={body.get('status') if isinstance(body, dict) else 'n/a'}")
    return _ok("massive", True, latency_ms, sc, body)


async def check_alpha_vantage() -> dict[str, Any]:
    key = _resolve_key("ALPHA_VANTAGE_KEY")
    if not key:
        return _unconfigured("alpha_vantage", "ALPHA_VANTAGE_KEY")
    t0 = time.monotonic()
    sc, body, err = await _http_get(
        "https://www.alphavantage.co/query",
        params={"function": "GLOBAL_QUOTE", "symbol": "AAPL", "apikey": key},
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    if err:
        return _err("alpha_vantage", True, latency_ms, sc, err)
    if sc != 200:
        return _err("alpha_vantage", True, latency_ms, sc,
                    f"HTTP {sc}")
    # AlphaVantage returns 200 + an error message on quota exhaustion.
    if isinstance(body, dict) and "Error Message" in body:
        return _err("alpha_vantage", True, latency_ms, sc,
                    f"API error: {body['Error Message'][:200]}")
    if isinstance(body, dict) and "Note" in body:
        return _err("alpha_vantage", True, latency_ms, sc,
                    "rate-limited (free tier 5/min, 500/day)")
    if not isinstance(body, dict) or not (body.get("Global Quote") or {}).get("01. symbol"):
        return _err("alpha_vantage", True, latency_ms, sc,
                    "no Global Quote in response")
    return _ok("alpha_vantage", True, latency_ms, sc, body)


async def check_newsapi() -> dict[str, Any]:
    key = _resolve_key("NEWSAPI_KEY")
    if not key:
        return _unconfigured("newsapi", "NEWSAPI_KEY")
    t0 = time.monotonic()
    sc, body, err = await _http_get(
        "https://newsapi.org/v2/top-headlines",
        params={"country": "us", "pageSize": "1", "apiKey": key},
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    if err:
        return _err("newsapi", True, latency_ms, sc, err)
    if sc != 200:
        return _err("newsapi", True, latency_ms, sc,
                    f"HTTP {sc} — key may be invalid")
    if not isinstance(body, dict) or body.get("status") != "ok":
        return _err("newsapi", True, latency_ms, sc,
                    f"status={body.get('status') if isinstance(body, dict) else 'n/a'}")
    return _ok("newsapi", True, latency_ms, sc, body)


async def check_marketaux() -> dict[str, Any]:
    key = _resolve_key("MARKETAUX_API_KEY")
    if not key:
        return _unconfigured("marketaux", "MARKETAUX_API_KEY")
    t0 = time.monotonic()
    sc, body, err = await _http_get(
        "https://api.marketaux.com/v1/news/all",
        params={"api_token": key, "limit": "1"},
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    if err:
        return _err("marketaux", True, latency_ms, sc, err)
    if sc != 200:
        return _err("marketaux", True, latency_ms, sc,
                    f"HTTP {sc} — key may be invalid")
    if not isinstance(body, dict) or "data" not in body:
        return _err("marketaux", True, latency_ms, sc,
                    "no data field in response")
    return _ok("marketaux", True, latency_ms, sc, body)


# ──────────────────────────────────────────────────────────────────────
# Result builders
# ──────────────────────────────────────────────────────────────────────


def _ok(provider: str, configured: bool, latency_ms: int, sc: int,
        payload: Any) -> dict[str, Any]:
    return {
        "provider": provider,
        "ok": True,
        "configured": configured,
        "latency_ms": latency_ms,
        "status_code": sc,
        "error": None,
        "response_sample": _ok_response_sample(provider, payload),
        "checked_at": _now_iso(),
    }


def _err(provider: str, configured: bool, latency_ms: int, sc: int,
         err: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "ok": False,
        "configured": configured,
        "latency_ms": latency_ms,
        "status_code": sc,
        "error": err,
        "response_sample": None,
        "checked_at": _now_iso(),
    }


def _unconfigured(provider: str, env_name: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "ok": False,
        "configured": False,
        "latency_ms": 0,
        "status_code": 0,
        "error": f"{env_name} not configured",
        "response_sample": None,
        "checked_at": _now_iso(),
    }


# ──────────────────────────────────────────────────────────────────────
# Registry + dispatch
# ──────────────────────────────────────────────────────────────────────


CHECKS: dict[str, Callable[[], "asyncio.Future[dict[str, Any]]"]] = {
    "yfinance": check_yfinance,
    "frankfurter": check_frankfurter,
    "treasury_fiscaldata": check_treasury_fiscaldata,
    "finnhub": check_finnhub,
    "massive": check_massive,
    "alpha_vantage": check_alpha_vantage,
    "newsapi": check_newsapi,
    "marketaux": check_marketaux,
}


def supported_providers() -> list[str]:
    return sorted(CHECKS.keys())


async def check_provider(name: str) -> dict[str, Any]:
    """Run a single provider's health check by name."""
    fn = CHECKS.get(name)
    if fn is None:
        return {
            "provider": name,
            "ok": False,
            "error": "unknown_provider",
            "configured": False,
            "supported": supported_providers(),
            "checked_at": _now_iso(),
        }
    try:
        result = await fn()
        logger.info(
            "diagnostics.check",
            provider=name,
            ok=result.get("ok"),
            latency_ms=result.get("latency_ms"),
        )
        return result
    except Exception as exc:
        logger.warning(
            "diagnostics.check_unhandled",
            provider=name,
            error=f"{type(exc).__name__}: {exc}",
        )
        return _err(name, False, 0, 0, f"{type(exc).__name__}: {exc}")
