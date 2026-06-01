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
DataDownloader — Phase 2 Stage P1a
==================================

Financial-data download stage for InvestorClaw.

Adapts the ETLANTIS HTTPDownloader pattern (retry, rate-limit, cache-TTL) to
the financial-API landscape: yfinance, Finnhub, Massive, Alpha Vantage,
NewsAPI. Each provider gets its own ``DataProviderConfig`` describing rate
limits, retry policy, and cache semantics; the ``DataDownloader`` dispatches
symbol fetches via small provider-specific adapters, all wrapped in the same
retry / rate-limit / cache-validation framework.

Design goals
------------
* **Cache-first** — avoid re-fetching identical (provider, symbol, date_range)
  tuples within ``cache_max_age_hours`` (default 24h for OHLCV, 1h for news).
  Cache payload is stored as Parquet (fast reload, columnar, typed).
* **Rate-limit aware** — per-provider throttle. yfinance tolerates ~2 req/s,
  Finnhub free tier is 60 req/min (~1/sec), Massive free is 5 req/min, Alpha
  Vantage free is 5 req/min + 500/day, NewsAPI is 100/day. We use the most
  conservative safe floor and let callers override via config.
* **Retry with classification** — 429 (rate limit) → backoff + retry, 503
  (service unavailable) → retry, 404 (not found) → skip (return None, don't
  raise), anything else → retry up to ``retry_count`` then give up.
* **Statistics** — every ``download()`` call tracks cache hits, misses,
  retries, 404s, and failures; aggregate stats are exposed via
  ``DataDownloader.stats``.

This module only handles the *download* concern. Parsing/validation lives in
``data_extractor.py``; schema normalization lives in ``data_transformer.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------


@dataclass
class DataProviderConfig:
    """Per-provider download configuration.

    Attributes:
        provider_name:        Canonical provider identifier
                              ("yfinance" | "finnhub" | "massive" |
                              "alphavantage" | "newsapi").
        base_url:             Base URL for HTTP APIs (ignored for yfinance,
                              which uses its own SDK).
        retry_count:          Number of attempts before giving up.
        retry_delay:          Base seconds to wait between retries.
        rate_limit_delay:     Minimum seconds between sequential requests.
        cache_max_age_hours:  Cache TTL; 0 disables caching.
        timeout_seconds:      Per-request timeout.
        api_key_env:          Name of env var holding the API key, if any.
    """

    provider_name: str
    base_url: str = ""
    retry_count: int = 3
    retry_delay: float = 2.0
    rate_limit_delay: float = 1.0
    cache_max_age_hours: float = 24.0
    timeout_seconds: int = 30
    api_key_env: Optional[str] = None

    # Status codes that should trigger retry (as opposed to permanent failure).
    retry_status_codes: Tuple[int, ...] = (429, 500, 502, 503, 504)
    # Status codes that should be treated as "symbol not available" (skip, no raise).
    skip_status_codes: Tuple[int, ...] = (404,)


# Sensible defaults per provider (free-tier safe).
DEFAULT_PROVIDER_CONFIGS: Dict[str, DataProviderConfig] = {
    "yfinance": DataProviderConfig(
        provider_name="yfinance",
        base_url="",  # SDK-based, no HTTP base URL
        rate_limit_delay=0.5,  # ~2 req/s is safe for yfinance
        cache_max_age_hours=24.0,
    ),
    "finnhub": DataProviderConfig(
        provider_name="finnhub",
        base_url="https://finnhub.io/api/v1/",
        rate_limit_delay=1.05,  # 60 req/min free tier → ~1/s
        cache_max_age_hours=24.0,
        api_key_env="FINNHUB_API_KEY",
    ),
    "massive": DataProviderConfig(
        provider_name="massive",
        base_url="https://api.massive.com/",
        rate_limit_delay=12.5,  # 5 req/min free tier → 12s each
        cache_max_age_hours=24.0,
        api_key_env="MASSIVE_API_KEY",
    ),
    "alphavantage": DataProviderConfig(
        provider_name="alphavantage",
        base_url="https://www.alphavantage.co/query",
        rate_limit_delay=12.5,  # 5 req/min free tier
        cache_max_age_hours=24.0,
        api_key_env="ALPHA_VANTAGE_API_KEY",
    ),
    "newsapi": DataProviderConfig(
        provider_name="newsapi",
        base_url="https://newsapi.org/v2/",
        rate_limit_delay=1.0,
        cache_max_age_hours=1.0,  # news is time-sensitive
        api_key_env="NEWSAPI_KEY",
    ),
}


# ---------------------------------------------------------------------------
# Download result + statistics
# ---------------------------------------------------------------------------


@dataclass
class DownloadResult:
    """Outcome of a single provider+symbol download attempt."""

    provider: str
    symbol: str
    cache_path: Optional[Path] = None
    cache_hit: bool = False
    attempts: int = 0
    skipped: bool = False  # True for 404s (symbol not available)
    error: Optional[str] = None
    rows: int = 0  # Best-effort row count for logging

    @property
    def success(self) -> bool:
        return self.cache_path is not None and self.error is None


@dataclass
class DownloadStats:
    """Aggregate statistics across a DataDownloader session."""

    requests_total: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    retries: int = 0
    skipped_404: int = 0
    failures: int = 0
    by_provider: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def record(self, provider: str, result: DownloadResult) -> None:
        self.requests_total += 1
        prov = self.by_provider.setdefault(
            provider,
            {
                "requests": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "retries": 0,
                "skipped": 0,
                "failures": 0,
            },
        )
        prov["requests"] += 1

        if result.cache_hit:
            self.cache_hits += 1
            prov["cache_hits"] += 1
        elif result.success:
            self.cache_misses += 1
            prov["cache_misses"] += 1

        if result.attempts > 1:
            retries = result.attempts - 1
            self.retries += retries
            prov["retries"] += retries

        if result.skipped:
            self.skipped_404 += 1
            prov["skipped"] += 1

        if result.error and not result.skipped:
            self.failures += 1
            prov["failures"] += 1

    def summary(self) -> Dict[str, Any]:
        return {
            "requests_total": self.requests_total,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "retries": self.retries,
            "skipped_404": self.skipped_404,
            "failures": self.failures,
            "by_provider": dict(self.by_provider),
        }


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------
# Each adapter knows how to turn (symbol, date_range, api_key) into raw data
# and write it to a Parquet cache file. Adapters raise on retryable errors
# and return None on "skip" (symbol not available). They do NOT implement
# retry, rate limiting, or caching — those are concerns of DataDownloader.


class _ProviderError(Exception):
    """Wraps an underlying fetch error with an HTTP-like status code."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _yfinance_adapter(
    symbol: str,
    date_range: Tuple[str, str],
    api_key: Optional[str],
    timeout: int,
) -> Optional[Any]:
    """Fetch OHLCV from yfinance. Returns a pandas DataFrame or None if empty."""
    try:
        import yfinance as yf  # noqa: F401
    except ImportError as e:
        raise _ProviderError(f"yfinance not installed: {e}") from e

    start, end = date_range
    ticker = yf.Ticker(symbol)
    try:
        df = ticker.history(start=start, end=end, auto_adjust=False, timeout=timeout)
    except Exception as e:  # yfinance wraps network errors generically
        # Best-effort HTTP code detection from message
        msg = str(e).lower()
        if "404" in msg or "not found" in msg:
            raise _ProviderError(f"yfinance 404 for {symbol}: {e}", 404) from e
        if "429" in msg or "too many" in msg:
            raise _ProviderError(f"yfinance 429 for {symbol}: {e}", 429) from e
        raise _ProviderError(f"yfinance error for {symbol}: {e}") from e

    if df is None or df.empty:
        # Treat as "not available" rather than a hard failure.
        raise _ProviderError(f"yfinance returned empty data for {symbol}", 404)
    return df


def _http_json_adapter(
    url: str,
    params: Dict[str, Any],
    timeout: int,
    retry_status_codes: Tuple[int, ...],
    skip_status_codes: Tuple[int, ...],
) -> Dict[str, Any]:
    """Fetch JSON over HTTP; classify status codes into _ProviderError."""
    try:
        import requests
    except ImportError as e:
        raise _ProviderError(f"requests not installed: {e}") from e

    response = requests.get(url, params=params, timeout=timeout)
    if response.status_code in skip_status_codes:
        raise _ProviderError(f"HTTP {response.status_code} for {url}", response.status_code)
    if response.status_code in retry_status_codes:
        raise _ProviderError(f"HTTP {response.status_code} for {url}", response.status_code)
    response.raise_for_status()
    return response.json()


def _finnhub_adapter(
    symbol: str,
    date_range: Tuple[str, str],
    api_key: Optional[str],
    timeout: int,
    base_url: str,
    retry_codes: Tuple[int, ...],
    skip_codes: Tuple[int, ...],
) -> Dict[str, Any]:
    """Finnhub /stock/candle endpoint."""
    if not api_key:
        raise _ProviderError("Finnhub API key missing (FINNHUB_API_KEY)")

    start_ts = int(time.mktime(time.strptime(date_range[0], "%Y-%m-%d")))
    end_ts = int(time.mktime(time.strptime(date_range[1], "%Y-%m-%d")))
    url = base_url.rstrip("/") + "/stock/candle"
    params = {
        "symbol": symbol,
        "resolution": "D",
        "from": start_ts,
        "to": end_ts,
        "token": api_key,
    }
    data = _http_json_adapter(url, params, timeout, retry_codes, skip_codes)
    if data.get("s") != "ok":
        raise _ProviderError(f"Finnhub returned status={data.get('s')!r} for {symbol}", 404)
    return data


def _massive_adapter(
    symbol: str,
    date_range: Tuple[str, str],
    api_key: Optional[str],
    timeout: int,
    base_url: str,
    retry_codes: Tuple[int, ...],
    skip_codes: Tuple[int, ...],
) -> Dict[str, Any]:
    """Massive aggregates v2 endpoint."""
    if not api_key:
        raise _ProviderError("Massive API key missing (MASSIVE_API_KEY)")
    start, end = date_range
    url = f"{base_url.rstrip('/')}/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"
    return _http_json_adapter(url, {"apiKey": api_key}, timeout, retry_codes, skip_codes)


def _alphavantage_adapter(
    symbol: str,
    date_range: Tuple[str, str],
    api_key: Optional[str],
    timeout: int,
    base_url: str,
    retry_codes: Tuple[int, ...],
    skip_codes: Tuple[int, ...],
) -> Dict[str, Any]:
    """Alpha Vantage TIME_SERIES_DAILY endpoint."""
    if not api_key:
        raise _ProviderError("Alpha Vantage API key missing (ALPHA_VANTAGE_API_KEY)")
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": "full",
        "datatype": "json",
        "apikey": api_key,
    }
    data = _http_json_adapter(base_url, params, timeout, retry_codes, skip_codes)
    if "Error Message" in data:
        raise _ProviderError(f"Alpha Vantage error for {symbol}: {data['Error Message']}", 404)
    if "Note" in data:  # rate-limited
        raise _ProviderError(f"Alpha Vantage rate-limited: {data['Note']}", 429)
    return data


def _newsapi_adapter(
    symbol: str,
    date_range: Tuple[str, str],
    api_key: Optional[str],
    timeout: int,
    base_url: str,
    retry_codes: Tuple[int, ...],
    skip_codes: Tuple[int, ...],
) -> Dict[str, Any]:
    """NewsAPI /everything endpoint (ticker used as a keyword query)."""
    if not api_key:
        raise _ProviderError("NewsAPI key missing (NEWSAPI_KEY)")
    url = base_url.rstrip("/") + "/everything"
    params = {
        "q": symbol,
        "from": date_range[0],
        "to": date_range[1],
        "language": "en",
        "sortBy": "publishedAt",
        "apiKey": api_key,
    }
    return _http_json_adapter(url, params, timeout, retry_codes, skip_codes)


# ---------------------------------------------------------------------------
# DataDownloader
# ---------------------------------------------------------------------------


class DataDownloader:
    """Retry/rate-limit/cache-aware multi-provider download stage.

    Typical usage::

        downloader = DataDownloader(cache_dir=Path("~/.investorclaw/cache"))
        results = downloader.download(
            provider="yfinance",
            symbols=["AAPL", "MSFT", "GOOG"],
            date_range=("2024-01-01", "2024-12-31"),
        )
        # results: Dict[symbol, DownloadResult]
        print(downloader.stats.summary())
    """

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        provider_configs: Optional[Dict[str, DataProviderConfig]] = None,
    ):
        self.cache_dir = Path(
            cache_dir or Path.home() / ".investorclaw" / "data_cache"
        ).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Merge user-provided configs over defaults.
        self.provider_configs: Dict[str, DataProviderConfig] = {**DEFAULT_PROVIDER_CONFIGS}
        if provider_configs:
            self.provider_configs.update(provider_configs)

        # Per-provider last-request timestamp for rate limiting.
        self._last_request: Dict[str, float] = {}

        self.stats = DownloadStats()

    # ----- Public API -------------------------------------------------------

    def download(
        self,
        provider: str,
        symbols: List[str],
        date_range: Tuple[str, str],
    ) -> Dict[str, DownloadResult]:
        """Download data for ``symbols`` from ``provider`` over ``date_range``.

        Returns a dict mapping each symbol to a :class:`DownloadResult`.
        Does not raise for individual symbol failures — inspect
        ``result.success`` and ``result.error`` per symbol.

        Args:
            provider:   One of the registered provider names.
            symbols:    List of ticker-like symbols.
            date_range: ``(start_date, end_date)`` in ``YYYY-MM-DD`` format.
        """
        cfg = self.provider_configs.get(provider)
        if cfg is None:
            raise ValueError(
                f"Unknown provider {provider!r}. Registered: {sorted(self.provider_configs)}"
            )

        results: Dict[str, DownloadResult] = {}
        for symbol in symbols:
            result = self._download_one(cfg, symbol, date_range)
            results[symbol] = result
            self.stats.record(provider, result)
        return results

    def clear_cache(self, provider: Optional[str] = None) -> int:
        """Remove cached Parquet/JSON files, optionally scoped to a provider.

        Returns the number of files removed.
        """
        removed = 0
        pattern = f"{provider}__*" if provider else "*"
        for p in self.cache_dir.glob(pattern):
            if p.is_file():
                p.unlink()
                removed += 1
        return removed

    # ----- Internal plumbing ------------------------------------------------

    def _cache_key(self, provider: str, symbol: str, date_range: Tuple[str, str]) -> Path:
        """Deterministic cache path for (provider, symbol, date_range)."""
        digest = hashlib.sha1(
            f"{provider}|{symbol}|{date_range[0]}|{date_range[1]}".encode("utf-8")
        ).hexdigest()[:12]
        safe_symbol = "".join(c if c.isalnum() else "_" for c in symbol)
        # Use .bin extension; the adapter decides whether to write Parquet or JSON.
        return self.cache_dir / f"{provider}__{safe_symbol}__{digest}.bin"

    def _is_cached(self, path: Path, cfg: DataProviderConfig) -> bool:
        if cfg.cache_max_age_hours <= 0:
            return False
        if not path.exists():
            return False
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
        if age_hours <= cfg.cache_max_age_hours:
            logger.debug(
                "[DataDownloader] Cache hit: %s (%.1fh old, max %.1fh)",
                path.name,
                age_hours,
                cfg.cache_max_age_hours,
            )
            return True
        return False

    def _rate_limit(self, cfg: DataProviderConfig) -> None:
        if cfg.rate_limit_delay <= 0:
            return
        now = time.time()
        elapsed = now - self._last_request.get(cfg.provider_name, 0.0)
        wait = cfg.rate_limit_delay - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request[cfg.provider_name] = time.time()

    def _api_key(self, cfg: DataProviderConfig) -> Optional[str]:
        if not cfg.api_key_env:
            return None
        import os

        return os.environ.get(cfg.api_key_env)

    def _invoke_adapter(
        self,
        cfg: DataProviderConfig,
        symbol: str,
        date_range: Tuple[str, str],
    ) -> Any:
        """Dispatch to the correct adapter for cfg.provider_name."""
        api_key = self._api_key(cfg)
        if cfg.provider_name == "yfinance":
            return _yfinance_adapter(symbol, date_range, api_key, cfg.timeout_seconds)
        if cfg.provider_name == "finnhub":
            return _finnhub_adapter(
                symbol,
                date_range,
                api_key,
                cfg.timeout_seconds,
                cfg.base_url,
                cfg.retry_status_codes,
                cfg.skip_status_codes,
            )
        if cfg.provider_name == "massive":
            return _massive_adapter(
                symbol,
                date_range,
                api_key,
                cfg.timeout_seconds,
                cfg.base_url,
                cfg.retry_status_codes,
                cfg.skip_status_codes,
            )
        if cfg.provider_name == "alphavantage":
            return _alphavantage_adapter(
                symbol,
                date_range,
                api_key,
                cfg.timeout_seconds,
                cfg.base_url,
                cfg.retry_status_codes,
                cfg.skip_status_codes,
            )
        if cfg.provider_name == "newsapi":
            return _newsapi_adapter(
                symbol,
                date_range,
                api_key,
                cfg.timeout_seconds,
                cfg.base_url,
                cfg.retry_status_codes,
                cfg.skip_status_codes,
            )
        raise ValueError(f"No adapter registered for {cfg.provider_name!r}")

    @staticmethod
    def _write_cache(path: Path, payload: Any) -> int:
        """Write ``payload`` to ``path``. Returns row/record count for logging.

        Pandas DataFrames are written as Parquet (columnar, fast reload).
        Dicts/JSON-ish payloads are written as JSON.
        """
        try:
            import pandas as pd
        except ImportError:
            pd = None  # type: ignore

        if pd is not None and isinstance(payload, pd.DataFrame):
            # Use a .parquet companion path for clarity but also keep the
            # canonical .bin path so cache lookups hit.
            parquet_path = path.with_suffix(".parquet")
            payload.to_parquet(parquet_path, index=True)
            # Mirror to .bin so _is_cached() detects freshness via the
            # canonical cache key path.
            path.write_bytes(parquet_path.read_bytes())
            return int(len(payload))

        # Fallback: JSON-serializable payload.
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, default=str)
        if (
            isinstance(payload, dict)
            and "results" in payload
            and isinstance(payload["results"], list)
        ):
            return len(payload["results"])
        if (
            isinstance(payload, dict)
            and "articles" in payload
            and isinstance(payload["articles"], list)
        ):
            return len(payload["articles"])
        return 1

    def _download_one(
        self,
        cfg: DataProviderConfig,
        symbol: str,
        date_range: Tuple[str, str],
    ) -> DownloadResult:
        """Download a single symbol with retry/rate-limit/cache logic."""
        cache_path = self._cache_key(cfg.provider_name, symbol, date_range)
        result = DownloadResult(provider=cfg.provider_name, symbol=symbol)

        # 1. Cache check
        if self._is_cached(cache_path, cfg):
            result.cache_hit = True
            result.cache_path = cache_path
            return result

        # 2. Rate limit + retry loop
        last_error: Optional[Exception] = None
        for attempt in range(1, cfg.retry_count + 1):
            result.attempts = attempt
            self._rate_limit(cfg)
            try:
                logger.info(
                    "[DataDownloader] %s/%s (attempt %d/%d)",
                    cfg.provider_name,
                    symbol,
                    attempt,
                    cfg.retry_count,
                )
                payload = self._invoke_adapter(cfg, symbol, date_range)
                rows = self._write_cache(cache_path, payload)
                result.cache_path = cache_path
                result.rows = rows
                return result

            except _ProviderError as e:
                last_error = e
                # Skip on 404
                if e.status_code in cfg.skip_status_codes:
                    result.skipped = True
                    result.error = str(e)
                    logger.info(
                        "[DataDownloader] Skipping %s/%s (status=%s)",
                        cfg.provider_name,
                        symbol,
                        e.status_code,
                    )
                    return result
                # Retry on classified status codes or unknown
                if attempt < cfg.retry_count:
                    backoff = cfg.retry_delay * attempt  # linear backoff
                    logger.warning(
                        "[DataDownloader] %s/%s attempt %d failed (%s); retrying in %.1fs",
                        cfg.provider_name,
                        symbol,
                        attempt,
                        e,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
            except Exception as e:  # noqa: BLE001 — last-resort catch
                last_error = e
                if attempt < cfg.retry_count:
                    time.sleep(cfg.retry_delay * attempt)
                    continue

        result.error = f"{type(last_error).__name__}: {last_error}"
        logger.error(
            "[DataDownloader] %s/%s failed after %d attempts: %s",
            cfg.provider_name,
            symbol,
            cfg.retry_count,
            last_error,
        )
        return result


__all__ = [
    "DataDownloader",
    "DataProviderConfig",
    "DownloadResult",
    "DownloadStats",
    "DEFAULT_PROVIDER_CONFIGS",
]
