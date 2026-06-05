#!/usr/bin/env python3
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
Multi-provider financial data abstraction for InvestorClaw.

Supported providers:
  finnhub    - Finnhub.io: quotes, historical candles, company news, analyst ratings
  yfinance   - Yahoo Finance (unofficial): batch quotes, historical, news, analyst
  newsapi    - NewsAPI.org: news headlines only (no price data)
  massive    - Massive: quotes, historical, news

Provider priority is resolved at runtime from INVESTORCLAW_PRICE_PROVIDER env var
or passed explicitly to PriceProvider(primary=...).

All methods return plain dicts / lists of dicts — no pandas, no external types.
"""

import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from ratelimit import limits, sleep_and_retry

logger = logging.getLogger(__name__)

# Provider API keys come from os.environ, populated by runtime/bootstrap.py
# at engine startup. Earlier versions of this module called load_dotenv()
# against src/ic_engine/.env at import time; that regressed the security
# fix removing repo-internal .env loading and risked pulling secrets from
# an unexpected package path. The bootstrap path is the only source.


def _canonical_futures_api_ticker(ticker: str) -> str:
    normalized = str(ticker).strip().upper()
    if normalized[:1] in {"/", "@"}:
        normalized = normalized[1:]
    return normalized


# ─── Provider implementations with official SDKs ──────────────────────────


class FinnhubProvider:
    """
    Finnhub.io REST API provider (using official finnhub-python SDK).
    Free tier: 60 calls/minute.
    Docs: https://finnhub.io/docs/api
    """

    NAME = "finnhub"

    def __init__(self, api_key: Optional[str] = None):
        try:
            import finnhub
        except ImportError:
            raise ImportError(
                "finnhub-python not installed. Install with: pip install finnhub-python"
            )

        self.api_key = api_key or os.getenv("FINNHUB_KEY") or os.getenv("FINNHUB_API_KEY")
        if not self.api_key:
            raise ValueError("FINNHUB_KEY not set")

        self._client = finnhub.Client(api_key=self.api_key)

    @sleep_and_retry
    @limits(calls=60, period=60)
    def _rate_limited_quote(self, symbol: str) -> Optional[Dict]:
        """Finnhub free tier is 60 calls/minute; serialize quote calls through one limiter."""
        data = self._client.quote(symbol)
        if not data or data.get("c", 0) == 0:
            return None
        return {
            "symbol": symbol,
            "price": data["c"],
            "change": data.get("d", 0),
            "pct_change": data.get("dp", 0),
            "high": data.get("h", 0),
            "low": data.get("l", 0),
            "open": data.get("o", 0),
            "prev_close": data.get("pc", 0),
            "provider": self.NAME,
        }

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """Current quote. Returns dict with price, change, pct_change, high, low, open, prev_close."""
        try:
            return self._rate_limited_quote(symbol)
        except Exception as e:
            logger.warning(f"Finnhub quote({symbol}): {e}")
            return None

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        Batch quotes via ThreadPoolExecutor for parallelism.
        Rate limiter respects 60/min quota across threads.
        """
        results = {}
        if not symbols:
            return results

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self.get_quote, sym): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    q = future.result()
                    if q:
                        results[sym] = q
                except Exception as e:
                    logger.warning(f"Finnhub get_quote({sym}) in thread pool: {e}")

        return results

    def get_history(self, symbol: str, days: int = 365) -> List[Dict]:
        """
        Daily OHLCV candles.
        NOTE: Finnhub /stock/candle requires a Premium plan (returns 403 on free tier).
        This method will return [] on free tier — Alpha Vantage or yfinance are preferred.
        """
        try:
            to_ts = int(datetime.now().timestamp())
            from_ts = int((datetime.now() - timedelta(days=days)).timestamp())
            data = self._client.candle(symbol, "D", from_ts, to_ts)
            if not data or data.get("s") != "ok":
                return []
            return [
                {
                    "date": datetime.fromtimestamp(t).strftime("%Y-%m-%d"),
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                    "symbol": symbol,
                    "provider": self.NAME,
                }
                for t, o, h, l, c, v in zip(
                    data["t"], data["o"], data["h"], data["l"], data["c"], data["v"]
                )
            ]
        except Exception as e:
            logger.warning(f"Finnhub history({symbol}): {e}")
            return []

    def get_news(self, symbols: List[str], days: int = 7) -> List[Dict]:
        """Company news for a list of symbols over the past N days."""
        to_date = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        articles = []

        for sym in symbols:
            try:
                data = self._client.company_news(sym, from_date, to_date)
                if not data:
                    continue
                for item in data[:5]:  # cap at 5 per symbol
                    articles.append(
                        {
                            "symbol": sym,
                            "headline": item.get("headline", ""),
                            "summary": item.get("summary", ""),
                            "source": item.get("source", ""),
                            "url": item.get("url", ""),
                            "datetime": datetime.fromtimestamp(item.get("datetime", 0)).strftime(
                                "%Y-%m-%d %H:%M"
                            ),
                            "provider": self.NAME,
                        }
                    )
            except Exception as e:
                logger.warning(f"Finnhub news({sym}): {e}")

        return articles

    def get_general_news(self, category: str = "general") -> List[Dict]:
        """General market-wide news from Finnhub by category.

        Uses the Finnhub SDK's ``general_news(category)`` endpoint.
        Categories: ``general``, ``forex``, ``crypto``, ``merger``.

        Returns list of dicts in the same shape as ``get_news()`` (symbol set to
        the category string so callers can distinguish source).
        """
        valid_categories = {"general", "forex", "crypto", "merger"}
        if category not in valid_categories:
            logger.warning(
                f"Finnhub get_general_news: unknown category {category!r}; "
                f"valid: {valid_categories}"
            )
            return []
        try:
            data = self._client.general_news(category, min_id=0)
            if not data:
                return []
            articles = []
            for item in data[:20]:  # cap at 20 to respect rate budget
                articles.append(
                    {
                        "symbol": category,
                        "headline": item.get("headline", ""),
                        "summary": item.get("summary", ""),
                        "source": item.get("source", ""),
                        "url": item.get("url", ""),
                        "datetime": (
                            datetime.fromtimestamp(item.get("datetime", 0)).strftime(
                                "%Y-%m-%d %H:%M"
                            )
                            if item.get("datetime")
                            else ""
                        ),
                        "provider": self.NAME,
                    }
                )
            return articles
        except Exception as e:
            logger.warning(f"Finnhub get_general_news({category}): {e}")
            return []

    def get_analyst_ratings(self, symbols: List[str]) -> Dict[str, Dict]:
        """Latest analyst consensus recommendation for each symbol."""
        results = {}

        for sym in symbols:
            try:
                data = self._client.recommendation_trends(sym)
                if not data or not isinstance(data, list) or len(data) == 0:
                    continue
                latest = data[0]
                total = sum(
                    [
                        latest.get("strongBuy", 0),
                        latest.get("buy", 0),
                        latest.get("hold", 0),
                        latest.get("sell", 0),
                        latest.get("strongSell", 0),
                    ]
                )
                results[sym] = {
                    "symbol": sym,
                    "period": latest.get("period", ""),
                    "strong_buy": latest.get("strongBuy", 0),
                    "buy": latest.get("buy", 0),
                    "hold": latest.get("hold", 0),
                    "sell": latest.get("sell", 0),
                    "strong_sell": latest.get("strongSell", 0),
                    "total": total,
                    "provider": self.NAME,
                }
            except Exception as e:
                logger.warning(f"Finnhub analyst({sym}): {e}")

        return results


class YFinanceProvider:
    """
    Yahoo Finance via yfinance (unofficial, no API key).
    Fastest for batch quote downloads but rate-limited and non-deterministic.
    """

    NAME = "yfinance"

    def __init__(self):
        try:
            import yfinance as yf

            self._yf = yf
        except ImportError:
            raise ImportError("yfinance not installed. Install with: pip install yfinance")

    @staticmethod
    def _yf_symbol(sym: str) -> str:
        """Convert broker symbols to yfinance format (BRK.B → BRK-B)."""
        return sym.replace(".", "-")

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """Batch quote download — one HTTP call for all symbols."""
        if not symbols:
            return {}

        yf_syms = [self._yf_symbol(s) for s in symbols]
        reverse = {self._yf_symbol(s): s for s in symbols}
        try:
            data = self._yf.download(
                yf_syms if len(yf_syms) > 1 else yf_syms[0],
                period="1d",
                progress=False,
                auto_adjust=True,
            )
            results = {}
            if data.empty:
                return {}

            if len(yf_syms) == 1:
                yf_sym = yf_syms[0]
                orig = reverse[yf_sym]
                row = data.iloc[-1]
                close_val = row.get("Close", row.get("close", 0))
                if hasattr(close_val, "iloc"):
                    close_val = close_val.iloc[0] if len(close_val) > 0 else 0
                results[orig] = {
                    "symbol": orig,
                    "price": float(close_val),
                    "provider": self.NAME,
                }
            else:
                close = data["Close"] if "Close" in data.columns else data["close"]
                for yf_sym in yf_syms:
                    orig = reverse[yf_sym]
                    if yf_sym in close.columns and not close[yf_sym].isna().all():
                        price = float(close[yf_sym].dropna().iloc[-1])
                        results[orig] = {"symbol": orig, "price": price, "provider": self.NAME}
            return results
        except Exception as e:
            logger.warning(f"yfinance batch quotes: {e}")
            return {}

    def get_quote(self, symbol: str) -> Optional[Dict]:
        r = self.get_quotes([symbol])
        return r.get(symbol)

    def get_history(self, symbol: str, days: int = 365) -> List[Dict]:
        """Historical daily OHLCV."""
        try:
            t = self._yf.Ticker(self._yf_symbol(symbol))
            period = "1y" if days <= 365 else "2y"
            hist = t.history(period=period)
            if hist.empty:
                return []
            hist = hist.reset_index()
            return [
                {
                    "date": str(row["Date"])[:10],
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]),
                    "symbol": symbol,
                    "provider": self.NAME,
                }
                for _, row in hist.iterrows()
            ]
        except Exception as e:
            logger.warning(f"yfinance history {symbol}: {e}")
            return []

    def get_news(self, symbols: List[str], days: int = 7) -> List[Dict]:
        """News via yfinance Ticker.news."""
        articles = []
        for sym in symbols:
            try:
                t = self._yf.Ticker(self._yf_symbol(sym))
                for item in (t.news or [])[:5]:
                    articles.append(
                        {
                            "symbol": sym,
                            "headline": item.get("title", ""),
                            "summary": item.get("summary", ""),
                            "source": item.get("publisher", ""),
                            "url": item.get("link", ""),
                            "datetime": datetime.fromtimestamp(
                                item.get("providerPublishTime", 0)
                            ).strftime("%Y-%m-%d %H:%M"),
                            "provider": self.NAME,
                        }
                    )
            except Exception as e:
                logger.warning(f"yfinance news {sym}: {e}")
        return articles

    def get_analyst_ratings(self, symbols: List[str]) -> Dict[str, Dict]:
        results = {}
        for sym in symbols:
            try:
                t = self._yf.Ticker(self._yf_symbol(sym))
                rec = t.recommendations
                if rec is None or rec.empty:
                    continue
                latest = rec.iloc[-1]
                results[sym] = {
                    "symbol": sym,
                    "period": str(rec.index[-1])[:10],
                    "consensus": str(latest.get("To Grade", latest.get("Action", ""))),
                    "firm": str(latest.get("Firm", "")),
                    "provider": self.NAME,
                }
            except Exception as e:
                logger.warning(f"yfinance analyst {sym}: {e}")
        return results


class NewsAPIProvider:
    """
    NewsAPI.org — news headlines and sentiment only (using newsapi-python).
    Free tier: 100 requests/day. No price data.
    """

    NAME = "newsapi"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("NEWSAPI_KEY")
        if not self.api_key:
            raise ValueError("NEWSAPI_KEY not set")

        # Use requests directly; newsapi-python package is thin wrapper
        self._base = "https://newsapi.org/v2"

    @sleep_and_retry
    @limits(calls=30, period=60)
    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        try:
            params = params or {}
            params["apiKey"] = self.api_key
            r = requests.get(f"{self._base}{path}", params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning(f"NewsAPI {path}: {type(e).__name__}")
            return None

    def get_news(self, symbols: List[str], days: int = 7) -> List[Dict]:
        """News headlines for a list of ticker symbols."""
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        articles = []

        for i in range(0, len(symbols), 5):
            batch = symbols[i : i + 5]
            q = " OR ".join(batch)
            data = self._get(
                "/everything",
                {
                    "q": q,
                    "from": from_date,
                    "sortBy": "publishedAt",
                    "language": "en",
                    "pageSize": 20,
                },
            )
            if not data or data.get("status") != "ok":
                continue

            for item in data.get("articles", []):
                title = (item.get("title") or "").upper()
                matched = next((s for s in batch if s in title), batch[0])
                articles.append(
                    {
                        "symbol": matched,
                        "headline": item.get("title", ""),
                        "summary": item.get("description", ""),
                        "source": item.get("source", {}).get("name", ""),
                        "url": item.get("url", ""),
                        "datetime": (item.get("publishedAt") or "")[:16].replace("T", " "),
                        "provider": self.NAME,
                    }
                )
        return articles

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        raise NotImplementedError("NewsAPI does not provide price data")

    def get_history(self, symbol: str, days: int = 365) -> List[Dict]:
        raise NotImplementedError("NewsAPI does not provide price data")

    def get_analyst_ratings(self, symbols: List[str]) -> Dict[str, Dict]:
        raise NotImplementedError("NewsAPI does not provide analyst ratings")


class MassiveProvider:
    """
    Massive market data provider.
    Starter plan: real-time quotes, full OHLCV history, news.
    Docs: https://massive.com/docs/stocks
    """

    NAME = "massive"
    API_BASE = "https://api.massive.com"

    def __init__(self, api_key: Optional[str] = None):
        try:
            # polygon-api-client is the upstream SDK Massive is built on.
            from polygon import RESTClient
        except ImportError:
            raise ImportError(
                "polygon-api-client not installed. Install with: pip install polygon-api-client"
            )

        self.api_key = api_key or os.getenv("MASSIVE_API_KEY")
        if not self.api_key:
            raise ValueError("MASSIVE_API_KEY not set")

        # Base MUST point at Massive — never fall back to the SDK's default
        # endpoint (legacy domain). If the installed SDK can't honor the base
        # override, fail loudly rather than silently routing off-Massive.
        try:
            self._client = RESTClient(api_key=self.api_key, base=self.API_BASE, trace=False)
        except TypeError as e:
            raise RuntimeError(
                "Installed polygon-api-client does not support base= override; "
                "upgrade so the Massive base endpoint can be enforced."
            ) from e

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """Previous-day close (free tier).

        The SDK renamed `get_previous_close` -> `get_previous_close_agg`
        in 2024. Modern shape: returns a List[PreviousCloseAgg] directly (no
        wrapper object with .results), with full attr names (close/open/high/
        low/volume/timestamp) instead of the legacy single-letter (.c/.o/.h/
        .l/.v/.t).
        """
        try:
            aggs = self._client.get_previous_close_agg(symbol)
            if not aggs:
                return None
            r = aggs[0]
            ts = getattr(r, "timestamp", None) or getattr(r, "t", None)
            return {
                "symbol": symbol,
                "price": getattr(r, "close", None) or getattr(r, "c", None) or 0,
                "open": getattr(r, "open", None) or getattr(r, "o", None) or 0,
                "high": getattr(r, "high", None) or getattr(r, "h", None) or 0,
                "low": getattr(r, "low", None) or getattr(r, "l", None) or 0,
                "volume": getattr(r, "volume", None) or getattr(r, "v", None) or 0,
                "date": datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else None,
                "provider": self.NAME,
            }
        except Exception as e:
            logger.warning(f"Massive quote({symbol}): {e}")
            return None

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """Batch quotes via snapshot-all endpoint (Starter+ plan) or prev-day fallback.

        Modern Massive SDK uses `get_snapshot_all(market_type, tickers)`
        for batch — old `get_snapshot_ticker(symbols=joined)` was a single-
        ticker call with an unsupported batch kwarg.
        """
        results: Dict[str, Dict] = {}

        # Try batch snapshot (Starter+ plan only)
        try:
            # Support stocks and forex via market_type
            market = "forex" if any("/" in s for s in symbols) else "stocks"
            data = self._client.get_snapshot_all(market_type=market, tickers=symbols)
            if data:
                for t in data:
                    day = getattr(t, "day", None)
                    last = getattr(t, "last_trade", None)
                    ticker = getattr(t, "ticker", None)
                    if not ticker:
                        continue
                    price = (
                        (getattr(day, "close", None) or getattr(day, "c", None) if day else None)
                        or (
                            getattr(last, "price", None) or getattr(last, "p", None)
                            if last
                            else None
                        )
                        or 0
                    )
                    results[ticker] = {
                        "symbol": ticker,
                        "price": price,
                        "open": (getattr(day, "open", None) or getattr(day, "o", None))
                        if day
                        else 0,
                        "high": (getattr(day, "high", None) or getattr(day, "h", None))
                        if day
                        else 0,
                        "low": (getattr(day, "low", None) or getattr(day, "l", None)) if day else 0,
                        "volume": (getattr(day, "volume", None) or getattr(day, "v", None))
                        if day
                        else 0,
                        "provider": self.NAME,
                    }
                if results:
                    return results
        except Exception as e:
            logger.debug(f"Massive snapshot_all unavailable: {e}; falling back to sequential")

        # Free-tier fallback: sequential prev-day calls
        logger.info(
            "Massive batch snapshot unavailable; falling back to sequential "
            f"for {len(symbols)} symbols"
        )
        for sym in symbols:
            q = self.get_quote(sym)
            if q:
                results[sym] = q
        return results

    def _apply_dividend_adjustment(self, rows: List[Dict], symbol: str) -> List[Dict]:
        """Apply backward dividend adjustment to OHLC prices.

        Massive list_aggs with adjusted=True only adjusts for splits; this method
        further adjusts close/open/high/low for cash dividends so the resulting
        prices match yfinance Adj Close semantics (total-return basis).

        Two-pass implementation: first pass reads ORIGINAL close-before-ex_date
        for every dividend and computes its factor independently using
        Massive split_adjusted_cash_amount (matching the split-adjusted basis of
        aggs returned with adjusted=True); second pass applies all factors. This
        avoids the compounding bug where computing a later (older-ex_date)
        factor against an already-adjusted close skews cumulative adjustment for
        multi-dividend windows.

        Raises:
            Exception: when list_dividends fails (network/auth/SDK error).
                Caller (get_history) should return [] so PriceProvider falls back
                to the next provider in the routing chain rather than serving
                silently degraded split-only data as dividend-adjusted data.
        """
        if not rows:
            return rows

        divs = list(
            self._client.list_dividends(
                ticker=symbol,
                ex_dividend_date_gte=rows[0]["date"],
                ex_dividend_date_lte=rows[-1]["date"],
                limit=1000,
            )
        )

        if not divs:
            logger.debug(f"Massive list_dividends({symbol}): no dividends in range")
            return rows

        date_to_idx = {r["date"]: i for i, r in enumerate(rows)}

        # PASS 1: collect (idx_before, factor) tuples using ORIGINAL closes.
        # No mutation in this pass.
        factors: List[Tuple[int, float]] = []
        logged_cash_amount_fallback = False
        for div in divs:
            ex_date = getattr(div, "ex_dividend_date", None)
            # Aggs are split-adjusted; prefer the split-adjusted dividend amount so
            # both sides use the same basis. Fall back to cash_amount for older SDK
            # versions that did not expose split_adjusted_cash_amount.
            cash_amount = getattr(div, "split_adjusted_cash_amount", None)
            if cash_amount is None:
                cash_amount = getattr(div, "cash_amount", None)
                if cash_amount is not None and not logged_cash_amount_fallback:
                    logger.debug(
                        f"Massive div adj({symbol}): using cash_amount fallback; "
                        f"dividend basis mismatch may be present"
                    )
                    logged_cash_amount_fallback = True
            if not ex_date or cash_amount is None or cash_amount <= 0:
                continue

            idx = date_to_idx.get(ex_date)
            if idx is not None:
                idx_before = idx - 1
            else:
                idx_before = None
                for i in range(len(rows) - 1, -1, -1):
                    if rows[i]["date"] < ex_date:
                        idx_before = i
                        break
            if idx_before is None or idx_before < 0:
                continue

            close_before = rows[idx_before]["close"]
            if close_before is None or close_before <= cash_amount:
                logger.warning(
                    f"Massive div adj({symbol}): close_before={close_before} <= D={cash_amount} "
                    f"on ex_date={ex_date}; skipping"
                )
                continue

            factor = (close_before - cash_amount) / close_before
            factors.append((idx_before, factor))

        if not factors:
            return rows

        # PASS 2: apply factors. Each row at index i gets multiplied by the
        # product of all factors whose idx_before >= i (i.e., dividends with
        # ex_date AFTER the row date). Compute per-row cumulative factor.
        n = len(rows)
        cum_factor = [1.0] * n
        for idx_before, f in factors:
            for i in range(idx_before + 1):
                cum_factor[i] *= f

        for i, cf in enumerate(cum_factor):
            if cf == 1.0:
                continue
            for fld in ("open", "high", "low", "close"):
                v = rows[i].get(fld)
                if v is not None:
                    rows[i][fld] = v * cf

        logger.debug(f"Massive div adj({symbol}): applied {len(factors)} dividends across {n} rows")
        return rows

    def get_history(self, symbol: str, days: int = 365) -> List[Dict]:
        """Daily OHLCV aggregates."""
        try:
            to_date = datetime.now().strftime("%Y-%m-%d")
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

            aggs = list(
                self._client.list_aggs(
                    ticker=symbol,
                    multiplier=1,
                    timespan="day",
                    from_=from_date,
                    to=to_date,
                    adjusted=True,
                    limit=min(days, 50000),
                    sort="asc",
                )
            )

            if not aggs:
                return []

            rows = [
                {
                    "date": datetime.fromtimestamp(a.timestamp / 1000).strftime("%Y-%m-%d"),
                    "open": a.open,
                    "high": a.high,
                    "low": a.low,
                    "close": a.close,
                    "volume": a.volume,
                    "symbol": symbol,
                    "provider": self.NAME,
                }
                for a in aggs
            ]
            try:
                rows = self._apply_dividend_adjustment(rows, symbol)
            except Exception as e:
                logger.warning(
                    f"Massive dividend adjustment failed for {symbol}: {e}; "
                    f"returning [] to trigger provider fallback"
                )
                return []
            return rows
        except Exception as e:
            logger.warning(f"Massive history({symbol}): {e}")
            return []

    def get_news(self, symbols: List[str], days: int = 7) -> List[Dict]:
        """Massive news API via /v2/reference/news."""
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        articles = []

        for sym in symbols:
            try:
                resp = requests.get(
                    f"{self.API_BASE}/v2/reference/news",
                    params={
                        "ticker": sym,
                        "published_utc.gte": from_date,
                        "limit": 25,
                        "apiKey": self.api_key,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", []) if isinstance(data, dict) else []
                if not results:
                    continue

                for item in results:
                    if not isinstance(item, dict):
                        continue
                    publisher = item.get("publisher") or {}
                    articles.append(
                        {
                            "symbol": sym,
                            "headline": item.get("title") or "",
                            "summary": item.get("description") or "",
                            "source": publisher.get("name", "") if isinstance(publisher, dict) else "",
                            "url": item.get("article_url") or "",
                            "datetime": (item.get("published_utc") or "")[:16].replace("T", " "),
                            "provider": self.NAME,
                        }
                    )
            except Exception as e:
                logger.warning(f"Massive news({sym}): {e}")

        return articles

    def get_analyst_ratings(self, symbols: List[str]) -> Dict[str, Dict]:
        raise NotImplementedError(
            "Massive does not provide analyst recommendations — use Finnhub or yfinance"
        )

    # ── Futures (Massive Futures API, launched 2026) ─────────────────────────
    # CME Globex (CBOT/CME/NYMEX/COMEX). The Massive SDK does not
    # wrap the /futures/vX/ surface yet, so these call the REST endpoints
    # directly. Verified live against the Massive partner key 2026-05-29.
    FUTURES_API_BASE = "https://api.massive.com/futures/vX"

    def _futures_get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET a /futures/vX endpoint; returns the parsed JSON body or None."""
        url = f"{self.FUTURES_API_BASE}{path}"
        q = dict(params or {})
        q["apiKey"] = self.api_key
        try:
            resp = requests.get(url, params=q, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("status") not in (None, "OK", "DELAYED"):
                logger.warning("Massive futures %s: status=%s", path, data.get("status"))
            return data
        except Exception as e:
            logger.warning("Massive futures GET %s: %s", path, e)
            return None

    def get_futures_contracts(
        self,
        product_code: Optional[str] = None,
        active: Optional[bool] = True,
        limit: int = 100,
    ) -> List[Dict]:
        """List futures contracts, newest first.

        ``product_code`` filters to one product family (e.g. ``ES``, ``CL``).
        ``active`` filters to currently-tradeable contracts when True.
        """
        params: dict = {"limit": max(1, min(int(limit), 1000)), "order": "desc"}
        if product_code:
            params["product_code"] = product_code.upper()
        if active is not None:
            params["active"] = "true" if active else "false"
        data = self._futures_get("/contracts", params)
        out: List[Dict] = []
        for r in (data or {}).get("results", []) or []:
            out.append(
                {
                    "ticker": r.get("ticker"),
                    "name": r.get("name"),
                    "product_code": r.get("product_code"),
                    "trading_venue": r.get("trading_venue"),
                    "first_trade_date": r.get("first_trade_date"),
                    "last_trade_date": r.get("last_trade_date"),
                    "active": r.get("active"),
                    "group_code": r.get("group_code"),
                    "provider": self.NAME,
                }
            )
        return out

    def get_futures_snapshot(self, ticker: str) -> Optional[Dict]:
        """Current market snapshot for a futures contract.

        Returns a normalised quote: last/settlement price, session OHLCV,
        change, plus contract details (product_code, settlement_date).
        """
        api_ticker = _canonical_futures_api_ticker(ticker)
        data = self._futures_get("/snapshot", {"ticker": api_ticker})
        results = (data or {}).get("results") or []
        if not results:
            return None
        r = results[0]
        session = r.get("session") or {}
        details = r.get("details") or {}
        close = session.get("close")
        settlement = session.get("settlement_price")
        price = close if close not in (None, 0) else settlement
        return {
            "symbol": api_ticker,
            "price": price or 0,
            "open": session.get("open") or 0,
            "high": session.get("high") or 0,
            "low": session.get("low") or 0,
            "close": close or 0,
            "volume": session.get("volume") or 0,
            "settlement_price": settlement or 0,
            "previous_settlement": session.get("previous_settlement") or 0,
            "change": session.get("change") or 0,
            "change_percent": session.get("change_percent") or 0,
            "product_code": details.get("product_code"),
            "settlement_date": details.get("settlement_date"),
            "provider": self.NAME,
        }

    def get_futures_quote(self, ticker: str) -> Optional[Dict]:
        """Alias for :meth:`get_futures_snapshot` — quote-shaped access."""
        return self.get_futures_snapshot(ticker)

    def get_futures_history(
        self,
        ticker: str,
        days: int = 365,
        resolution: str = "1day",
    ) -> List[Dict]:
        """Historical aggregate bars for a futures contract.

        ``resolution`` is a Massive futures resolution string (``1day``,
        ``1hour``, ``1minute`` ...). Returns oldest-first OHLCV rows; empty when
        the contract has no bars in range or the plan lacks futures history.
        """
        api_ticker = _canonical_futures_api_ticker(ticker)
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        data = self._futures_get(
            f"/aggs/{api_ticker}",
            {"resolution": resolution, "window_start": start, "window_end": end, "limit": 50000},
        )
        rows: List[Dict] = []
        for r in (data or {}).get("results", []) or []:
            ts = r.get("window_start") or r.get("timestamp") or r.get("t")
            dt = None
            if isinstance(ts, (int, float)):
                # futures feed timestamps are nanoseconds since epoch.
                secs = ts / 1e9 if ts > 1e12 else ts / 1000 if ts > 1e10 else ts
                try:
                    dt = datetime.fromtimestamp(secs).strftime("%Y-%m-%d")
                except (OverflowError, OSError, ValueError):
                    dt = None
            rows.append(
                {
                    "date": dt,
                    "open": r.get("open") or r.get("o") or 0,
                    "high": r.get("high") or r.get("h") or 0,
                    "low": r.get("low") or r.get("l") or 0,
                    "close": r.get("close") or r.get("c") or 0,
                    "volume": r.get("volume") or r.get("v") or 0,
                    "provider": self.NAME,
                }
            )
        rows.sort(key=lambda x: x["date"] or "")
        return rows

class AlphaVantageProvider:
    """
    Alpha Vantage REST API provider.
    Free tier: 25 requests/day (500/day with free key registration).
    Docs: https://www.alphavantage.co/documentation/
    """

    NAME = "alpha_vantage"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ALPHA_VANTAGE_KEY")
        if not self.api_key:
            raise ValueError("ALPHA_VANTAGE_KEY not set")
        self._base = "https://www.alphavantage.co/query"
        self._session = requests.Session()

    @sleep_and_retry
    @limits(calls=4, period=60)
    def _get(self, params: dict, timeout: int = 15) -> Optional[dict]:
        try:
            params["apikey"] = self.api_key
            r = self._session.get(self._base, params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if "Information" in data or "Note" in data:
                msg = data.get("Information") or data.get("Note", "")
                logger.warning(f"AlphaVantage API message: {msg[:120]}")
                return None
            return data
        except Exception as e:
            logger.warning(f"AlphaVantage: {e}")
            return None

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """Global Quote endpoint — current price."""
        data = self._get({"function": "GLOBAL_QUOTE", "symbol": symbol})
        if not data:
            return None

        q = data.get("Global Quote", {})
        price_str = q.get("05. price", "0")
        try:
            price = float(price_str)
        except ValueError:
            return None

        if price == 0:
            return None

        return {
            "symbol": symbol,
            "price": price,
            "change": float(q.get("09. change", 0) or 0),
            "pct_change": float((q.get("10. change percent", "0%") or "0%").replace("%", "") or 0),
            "high": float(q.get("03. high", 0) or 0),
            "low": float(q.get("04. low", 0) or 0),
            "open": float(q.get("02. open", 0) or 0),
            "prev_close": float(q.get("08. previous close", 0) or 0),
            "volume": int(q.get("06. volume", 0) or 0),
            "provider": self.NAME,
        }

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """Sequential quotes (no batch endpoint on free tier)."""
        results = {}
        for sym in symbols:
            q = self.get_quote(sym)
            if q:
                results[sym] = q
        return results

    def get_history(self, symbol: str, days: int = 365) -> List[Dict]:
        """Daily adjusted time series."""
        output_size = "full" if days > 100 else "compact"
        data = self._get(
            {
                "function": "TIME_SERIES_DAILY_ADJUSTED",
                "symbol": symbol,
                "outputsize": output_size,
            }
        )
        if not data:
            return []

        ts = data.get("Time Series (Daily)", {})
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        results = []
        for date_str in sorted(ts.keys(), reverse=True):
            if date_str < cutoff:
                break
            row = ts[date_str]
            results.append(
                {
                    "date": date_str,
                    "open": float(row.get("1. open", 0) or 0),
                    "high": float(row.get("2. high", 0) or 0),
                    "low": float(row.get("3. low", 0) or 0),
                    "close": float(row.get("5. adjusted close", row.get("4. close", 0)) or 0),
                    "volume": int(row.get("6. volume", 0) or 0),
                    "symbol": symbol,
                    "provider": self.NAME,
                }
            )
        return results

    def get_news(self, symbols: List[str], days: int = 7) -> List[Dict]:
        """News sentiment endpoint (requires paid plan for most content)."""
        tickers = ",".join(symbols[:5])
        data = self._get(
            {
                "function": "NEWS_SENTIMENT",
                "tickers": tickers,
                "limit": 20,
            }
        )
        if not data or "feed" not in data:
            return []

        articles = []
        for item in data["feed"]:
            ticker_sentiment = item.get("ticker_sentiment", [{}])
            sym = ticker_sentiment[0].get("ticker", symbols[0]) if ticker_sentiment else symbols[0]
            articles.append(
                {
                    "symbol": sym,
                    "headline": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "source": item.get("source", ""),
                    "url": item.get("url", ""),
                    "datetime": (item.get("time_published") or "")[:16].replace("T", " "),
                    "provider": self.NAME,
                }
            )
        return articles

    def get_analyst_ratings(self, symbols: List[str]) -> Dict[str, Dict]:
        """Earnings estimates used as proxy for analyst coverage."""
        results = {}
        for sym in symbols:
            data = self._get({"function": "EARNINGS", "symbol": sym})
            if not data or "annualEarnings" not in data:
                continue
            annual = data["annualEarnings"]
            if not annual:
                continue
            results[sym] = {
                "symbol": sym,
                "period": annual[0].get("fiscalDateEnding", ""),
                "consensus": "covered",
                "provider": self.NAME,
            }
        return results


# ─── Unified PriceProvider facade ─────────────────────────────────────────────

PROVIDER_CLASSES = {
    "finnhub": FinnhubProvider,
    "yfinance": YFinanceProvider,
    "newsapi": NewsAPIProvider,
    "massive": MassiveProvider,
    "alpha_vantage": AlphaVantageProvider,
    # Lazy registration — appended below after the no-key providers are defined.
}


# ─── Frankfurter FX provider (no-key) ────────────────────────────────────────
# https://www.frankfurter.app — free EUR/USD/etc. spot rates. Used when the
# user asks about FX (news-forex). No API key required, ECM data provenance.


class FrankfurterFxProvider:
    """FX spot rates from frankfurter.app (free, no key)."""

    NAME = "frankfurter"
    BASE_URL = "https://api.frankfurter.app"

    def __init__(self, api_key: Optional[str] = None):
        # Frankfurter takes no key; constructor signature parity with peers.
        del api_key

    def get_fx(self, from_ccy: str = "EUR", to_ccy: str = "USD") -> Optional[Dict]:
        """Latest spot rate. Returns {from, to, rate, date, provider}."""
        try:
            import urllib.request, json as _json

            url = f"{self.BASE_URL}/latest?from={from_ccy}&to={to_ccy}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "ic-engine/4.1 (mnemos-ic-runtime)"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())
            rate = (data.get("rates") or {}).get(to_ccy.upper())
            if rate is None:
                return None
            return {
                "from": from_ccy.upper(),
                "to": to_ccy.upper(),
                "rate": rate,
                "date": data.get("date"),
                "provider": self.NAME,
            }
        except Exception as e:
            logger.warning(f"Frankfurter fx({from_ccy}->{to_ccy}): {e}")
            return None

    def get_fx_pairs(self, base: str = "USD") -> Dict[str, float]:
        """Spot rates for every supported quote currency against `base`."""
        try:
            import urllib.request, json as _json

            url = f"{self.BASE_URL}/latest?from={base}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "ic-engine/4.1 (mnemos-ic-runtime)"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())
            return data.get("rates") or {}
        except Exception as e:
            logger.warning(f"Frankfurter fx_pairs({base}): {e}")
            return {}


# ─── Treasury.gov fiscal-data yield provider (no-key FRED fallback) ───────
# https://api.fiscaldata.treasury.gov — public Treasury yield data. Used when
# FRED_API_KEY is not set. No registration required.


class TreasuryFiscalDataProvider:
    """US Treasury yield curve via fiscaldata.treasury.gov (free, no key)."""

    NAME = "treasury_fiscaldata"
    BASE_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"

    def __init__(self, api_key: Optional[str] = None):
        del api_key

    def get_yield_curve(self) -> Dict[str, float]:
        """Latest avg interest rates by security description (Treasury bills,
        notes, bonds, TIPS, etc.). Returns dict keyed by security_desc.
        """
        try:
            import urllib.request, json as _json

            # Latest record per security description
            url = (
                f"{self.BASE_URL}/v2/accounting/od/avg_interest_rates"
                "?fields=record_date,security_desc,avg_interest_rate_amt"
                "&sort=-record_date"
                "&page[size]=200"
            )
            req = urllib.request.Request(
                url, headers={"User-Agent": "ic-engine/4.1 (mnemos-ic-runtime)"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())
            rows = data.get("data") or []
            if not rows:
                return {}
            # Take the most-recent rate per security_desc
            seen = set()
            curve: Dict[str, float] = {}
            for row in rows:
                desc = row.get("security_desc")
                if not desc or desc in seen:
                    continue
                seen.add(desc)
                try:
                    curve[desc] = float(row.get("avg_interest_rate_amt"))
                except (TypeError, ValueError):
                    continue
            return curve
        except Exception as e:
            logger.warning(f"Treasury fiscal_data yield_curve: {e}")
            return {}


PROVIDER_CLASSES["frankfurter"] = FrankfurterFxProvider
PROVIDER_CLASSES["treasury_fiscaldata"] = TreasuryFiscalDataProvider


# ─── Marketaux news provider ──────────────────────────────────────────────
# https://www.marketaux.com — financial news with broader category filters
# than NewsAPI (forex, crypto, M&A, country/sector). Free tier 100/day.


class MarketauxNewsProvider:
    """Financial news via marketaux.com (free tier, MARKETAUX_API_KEY)."""

    NAME = "marketaux"
    BASE_URL = "https://api.marketaux.com/v1"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("MARKETAUX_API_KEY")
        if not self.api_key:
            raise ValueError("MARKETAUX_API_KEY not set")

    def get_news(self, symbols: List[str], days: int = 7) -> List[Dict]:
        """News articles mentioning the given symbols."""
        try:
            import urllib.request, urllib.parse, json as _json

            params = {
                "api_token": self.api_key,
                "symbols": ",".join(symbols[:50]),  # marketaux caps at 50
                "filter_entities": "true",
                "language": "en",
                "limit": "10",
            }
            url = f"{self.BASE_URL}/news/all?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "ic-engine/4.1 (mnemos-ic-runtime)"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())
            articles = []
            for item in data.get("data") or []:
                articles.append(
                    {
                        "title": item.get("title", ""),
                        "summary": item.get("description") or item.get("snippet", ""),
                        "source": item.get("source", ""),
                        "url": item.get("url", ""),
                        "datetime": (item.get("published_at") or "")[:16].replace("T", " "),
                        "provider": self.NAME,
                    }
                )
            return articles
        except Exception as e:
            logger.warning(f"Marketaux news({len(symbols)} symbols): {e}")
            return []

    def get_general_news(self, category: str = "general") -> List[Dict]:
        """Category-filtered news (general / forex / crypto / merger / etc.).

        Marketaux maps:
          general -> all entity types
          forex   -> industries=Forex
          crypto  -> industries=Crypto
          merger  -> entity_types=organization + topic search 'merger acquisition'
        """
        try:
            import urllib.request, urllib.parse, json as _json

            params: Dict[str, str] = {
                "api_token": self.api_key,
                "language": "en",
                "limit": "10",
            }
            cat = (category or "general").lower()
            if cat == "forex":
                params["industries"] = "Currencies"
            elif cat == "crypto":
                params["industries"] = "Cryptocurrency"
            elif cat == "merger":
                params["search"] = "merger OR acquisition"
            # general: no filter — full feed
            url = f"{self.BASE_URL}/news/all?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "ic-engine/4.1 (mnemos-ic-runtime)"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())
            articles = []
            for item in data.get("data") or []:
                articles.append(
                    {
                        "category": cat,
                        "title": item.get("title", ""),
                        "summary": item.get("description") or item.get("snippet", ""),
                        "source": item.get("source", ""),
                        "url": item.get("url", ""),
                        "datetime": (item.get("published_at") or "")[:16].replace("T", " "),
                        "provider": self.NAME,
                    }
                )
            return articles
        except Exception as e:
            logger.warning(f"Marketaux general_news({category}): {e}")
            return []


PROVIDER_CLASSES["marketaux"] = MarketauxNewsProvider


def _build_provider(name: str):
    cls = PROVIDER_CLASSES.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider: {name!r}. Valid: {list(PROVIDER_CLASSES)}")
    try:
        return cls()
    except (ValueError, ImportError) as e:
        logger.warning(f"Cannot initialise {name}: {e}")
        return None


class PriceProvider:
    """
    Data-type-aware, quota-sharding financial data provider facade.

    Different operation types are routed to optimal providers:
      quotes     → yfinance (1 batch call, no quota) → Massive (1 batch call, Starter+)
                   → Finnhub (sequential, 60/min, no daily limit)
      history    → Alpha Vantage (adjusted close, 500/day) → Finnhub (candles, unlimited)
                   → yfinance
      news       → NewsAPI (broad, 100/day) + Finnhub (company-specific) — AGGREGATED
      analyst    → Finnhub (recommendations, unlimited) → yfinance

    For large portfolios, quotes are sharded across providers respecting daily quotas:
      INVESTORCLAW_QUOTA_ALPHAVANTAGE=500   (default, adjust if on paid plan)
      INVESTORCLAW_QUOTA_NEWSAPI=100

    Override routing via env vars:
      INVESTORCLAW_PRICE_PROVIDER=auto|finnhub|yfinance|massive|alpha_vantage
      INVESTORCLAW_FALLBACK_CHAIN=yfinance,massive  (comma-separated)
    """

    # Per-provider daily call budgets (free tier defaults)
    _DEFAULT_QUOTAS: Dict[str, int] = {
        "finnhub": 999_999,
        "yfinance": 999_999,
        "massive": 999_999,
        "alpha_vantage": 500,
        "newsapi": 100,
        "marketaux": 100,  # free tier 100/day
        "frankfurter": 999_999,  # no quota (no key, ECB-sourced)
        "treasury_fiscaldata": 999_999,  # no quota (public Treasury API)
    }

    # Preferred provider order per operation type (first available wins)
    # massive leads history because it is paid + unrate-limited.
    # alpha_vantage's 4-calls/min cap and finnhub's premium-only candle
    # endpoint both collapse under barrage load.
    _OP_ROUTING: Dict[str, List[str]] = {
        # yfinance is intentionally LAST in every chain — Yahoo's anonymous
        # query1 endpoint is rate-limited globally (HTTP 429) and collapses
        # under barrage load on a 200+ position portfolio. Use only as a
        # last-resort fallback when every other provider has failed.
        # For SMALL portfolios (≲50 symbols) yfinance still works fine and
        # is free, so the routing keeps it as a safety net rather than
        # removing it entirely.
        "quotes": ["massive", "finnhub", "alpha_vantage", "yfinance"],
        "history": ["massive", "alpha_vantage", "finnhub", "yfinance"],
        # Futures are CME contract tickers only Massive serves (/futures/vX).
        "futures": ["massive"],
        "news": ["marketaux", "finnhub", "newsapi", "yfinance"],
        "general_news": ["finnhub", "marketaux"],
        "fx": ["frankfurter", "alpha_vantage"],
        "treasury_yields": ["treasury_fiscaldata"],
        "analyst": ["finnhub", "yfinance"],
    }

    def __init__(
        self,
        primary: Optional[str] = None,
        fallback: Optional[List[str]] = None,
    ):
        self._override = os.getenv("INVESTORCLAW_PRICE_PROVIDER", "auto")
        if self._override == "auto":
            self._override = None

        self._fallback_names = [
            f.strip() for f in os.getenv("INVESTORCLAW_FALLBACK_CHAIN", "").split(",") if f.strip()
        ]
        if primary:
            self._override = primary
        if fallback:
            self._fallback_names = fallback

        # Build provider pool
        self._pool: Dict[str, object] = {}
        for name in list(PROVIDER_CLASSES.keys()):
            p = _build_provider(name)
            if p is not None:
                self._pool[name] = p

        # Read per-provider quotas from env
        self._quotas = dict(self._DEFAULT_QUOTAS)
        for name in self._pool:
            env_key = f"INVESTORCLAW_QUOTA_{name.upper()}"
            env_val = os.getenv(env_key)
            if env_val and env_val.isdigit():
                self._quotas[name] = int(env_val)
        self._quota_used: Dict[str, int] = {k: 0 for k in self._quotas}

        available = list(self._pool.keys())
        logger.info(f"PriceProvider: available={available}, override={self._override or 'routing'}")

    def _providers_for_op(self, op_type: str) -> List:
        """Return ordered list of available provider instances for an operation type."""
        if self._override:
            ordered = [self._override] + self._fallback_names
        else:
            ordered = self._OP_ROUTING.get(op_type, ["yfinance"])
        result = []
        for name in ordered:
            p = self._pool.get(name)
            if p and self._quota_used.get(name, 0) < self._quotas.get(name, 0):
                result.append(p)
        return result

    def _use_quota(self, provider_name: str, calls: int = 1) -> None:
        self._quota_used[provider_name] = self._quota_used.get(provider_name, 0) + calls

    @staticmethod
    def _estimate_quota_cost(provider_name: str, method: str, item_count: int = 1) -> int:
        """Estimate outbound request count for quota tracking/failover decisions."""
        if item_count <= 0:
            return 0
        if method == "get_quote" or method == "get_history":
            return 1
        if method == "get_quotes":
            if provider_name in {"alpha_vantage", "finnhub"}:
                return item_count
            return 1
        if method == "get_news":
            if provider_name == "newsapi":
                return math.ceil(item_count / 5)
            if provider_name == "finnhub":
                return item_count
            return 1
        if method == "get_analyst_ratings":
            return item_count if provider_name in {"finnhub", "yfinance", "alpha_vantage"} else 1
        return 1

    def _try_op(self, op_type: str, method: str, *args, **kwargs):
        """Try each provider in routing order; return first successful non-empty result."""
        for provider in self._providers_for_op(op_type):
            fn = getattr(provider, method, None)
            if fn is None:
                continue
            try:
                result = fn(*args, **kwargs)
                if result:
                    item_count = 1
                    if args and isinstance(args[0], list):
                        item_count = len(args[0])
                    self._use_quota(
                        provider.NAME,
                        self._estimate_quota_cost(provider.NAME, method, item_count),
                    )
                    return result
            except NotImplementedError:
                pass
            except Exception as e:
                logger.warning(f"{provider.NAME}.{method} failed: {e}")
        empty: Dict = {}
        return empty if method in ("get_quotes", "get_analyst_ratings") else []

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """Current price for a single symbol."""
        result = self._try_op("quotes", "get_quote", symbol)
        return result if result else None

    def _futures_provider(self):
        """First routed provider exposing the futures snapshot surface."""
        for provider in self._providers_for_op("futures"):
            if hasattr(provider, "get_futures_snapshot"):
                return provider
        return None

    def get_futures_quotes(self, tickers: List[str]) -> Dict[str, Dict]:
        """Batch snapshot quotes for futures contract tickers (Massive)."""
        provider = self._futures_provider()
        out: Dict[str, Dict] = {}
        if provider is None or not tickers:
            return out
        for t in tickers:
            api_ticker = _canonical_futures_api_ticker(t)
            try:
                snap = provider.get_futures_snapshot(api_ticker)
            except Exception as e:
                logger.warning("futures snapshot %s: %s", t, e)
                snap = None
            if snap:
                out[t] = snap
        if tickers:
            self._use_quota(getattr(provider, "NAME", "massive"), len(tickers))
        return out

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """Batch current prices for all symbols.

        Futures contract tickers (CME, e.g. ``ESZ25``) are split out and priced
        via Massive's futures snapshot — the equity quote providers don't carry
        them — then merged back with the equity/ETF/bond quotes.
        """
        if not symbols:
            return {}

        from ic_engine.providers.futures_spec import is_futures_ticker

        clean = [s for s in symbols if s is not None and str(s).strip()]
        futures_syms = [s for s in clean if is_futures_ticker(s)]
        results: Dict[str, Dict] = {}
        if futures_syms:
            results.update(self.get_futures_quotes(futures_syms))
        remaining = [s for s in clean if s not in results]

        for provider in self._providers_for_op("quotes"):
            if not remaining:
                break
            fn = getattr(provider, "get_quotes", None)
            if fn is None:
                continue
            try:
                # Charge quota BEFORE batch dispatch so over-budget requests get properly gated
                cost = self._estimate_quota_cost(provider.NAME, "get_quotes", len(remaining))
                self._use_quota(provider.NAME, cost)

                batch = fn(remaining)
                if batch:
                    results.update(batch)
                    remaining = [s for s in remaining if s not in results]
            except NotImplementedError:
                pass
            except Exception as e:
                logger.warning(f"{provider.NAME}.get_quotes({len(remaining)} syms) failed: {e}")

        if remaining:
            logger.warning(
                f"get_quotes: no price data for {len(remaining)} symbols: "
                f"{remaining[:5]}{'...' if len(remaining) > 5 else ''}"
            )
        return results

    def get_history(self, symbol: str, days: int = 365) -> List[Dict]:
        """Daily OHLCV history.

        Futures contract tickers route to Massive's futures aggregates; equities
        use the standard history chain.
        """
        from ic_engine.providers.futures_spec import is_futures_ticker

        if is_futures_ticker(symbol):
            provider = self._futures_provider()
            if provider is not None and hasattr(provider, "get_futures_history"):
                try:
                    rows = provider.get_futures_history(
                        _canonical_futures_api_ticker(symbol), days=days
                    )
                    self._use_quota(getattr(provider, "NAME", "massive"), 1)
                    if rows:
                        return rows
                except Exception as e:
                    logger.warning("futures history %s: %s", symbol, e)
            return []
        return self._try_op("history", "get_history", symbol, days=days)

    def get_futures_contracts(
        self, product_code: Optional[str] = None, active: Optional[bool] = True, limit: int = 100
    ) -> List[Dict]:
        """List futures contracts via the routed futures provider (Massive)."""
        provider = self._futures_provider()
        if provider is None or not hasattr(provider, "get_futures_contracts"):
            return []
        try:
            return provider.get_futures_contracts(
                product_code=product_code, active=active, limit=limit
            )
        except Exception as e:
            logger.warning("futures contracts: %s", e)
            return []

    def get_news(self, symbols: List[str], days: int = 7) -> List[Dict]:
        """News headlines. Aggregates from NewsAPI AND Finnhub."""
        articles: List[Dict] = []
        seen_urls: set = set()
        for provider in self._providers_for_op("news"):
            fn = getattr(provider, "get_news", None)
            if fn is None:
                continue
            try:
                # Charge quota BEFORE batch dispatch so over-budget requests get properly gated
                cost = self._estimate_quota_cost(provider.NAME, "get_news", len(symbols))
                self._use_quota(provider.NAME, cost)

                for a in fn(symbols, days=days):
                    url = a.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        articles.append(a)
            except (NotImplementedError, Exception) as e:
                logger.warning(f"{provider.NAME}.get_news failed: {e}")
        return articles

    def get_analyst_ratings(self, symbols: List[str]) -> Dict[str, Dict]:
        """Analyst consensus."""
        return self._try_op("analyst", "get_analyst_ratings", symbols)

    def get_general_news(self, category: str = "general") -> List[Dict]:
        """Category-keyed news (general/forex/crypto/merger). Returns the
        first non-empty result from the general_news provider chain. Useful
        for prompts like 'any big M&A news today?' that aren't symbol-keyed.
        """
        seen_urls = set()
        articles: List[Dict] = []
        for provider in self._providers_for_op("general_news"):
            fn = getattr(provider, "get_general_news", None)
            if fn is None:
                continue
            try:
                cost = self._estimate_quota_cost(provider.NAME, "get_news", 1)
                self._use_quota(provider.NAME, cost)
                for a in fn(category):
                    url = a.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        articles.append(a)
                if articles:
                    return articles  # first provider with content wins
            except Exception as e:
                logger.warning(f"{provider.NAME}.get_general_news({category}) failed: {e}")
        return articles

    def get_fx(self, from_ccy: str = "EUR", to_ccy: str = "USD") -> Optional[Dict]:
        """FX spot rate. Returns dict with from/to/rate/date/provider."""
        for provider in self._providers_for_op("fx"):
            fn = getattr(provider, "get_fx", None)
            if fn is None:
                continue
            try:
                result = fn(from_ccy, to_ccy)
                if result:
                    return result
            except Exception as e:
                logger.warning(f"{provider.NAME}.get_fx({from_ccy}->{to_ccy}) failed: {e}")
        return None

    def get_treasury_yields(self) -> Dict[str, float]:
        """US Treasury yield curve. Returns dict keyed by security_desc.
        Used as a no-key fallback when FRED_API_KEY is not configured."""
        for provider in self._providers_for_op("treasury_yields"):
            fn = getattr(provider, "get_yield_curve", None)
            if fn is None:
                continue
            try:
                curve = fn()
                if curve:
                    return curve
            except Exception as e:
                logger.warning(f"{provider.NAME}.get_yield_curve failed: {e}")
        return {}

    def quota_status(self) -> Dict[str, Dict]:
        """Return quota used/remaining per provider (for diagnostics)."""
        return {
            name: {
                "used": self._quota_used.get(name, 0),
                "limit": self._quotas.get(name, 0),
                "remaining": self._quotas.get(name, 0) - self._quota_used.get(name, 0),
                "available": name in self._pool,
            }
            for name in self._DEFAULT_QUOTAS
        }

    @property
    def primary_name(self) -> str:
        """Name of the primary quote provider (for diagnostics)."""
        return self._override or self._OP_ROUTING.get("quotes", ["yfinance"])[0]


# ─── Portfolio update priority engine ────────────────────────────────────────


class PortfolioUpdatePriority:
    """
    Tiers holdings by portfolio weight so high-impact positions get more
    frequent, higher-fidelity price updates while the tail is refreshed cheaply.

    Tier assignment (by cumulative portfolio weight):
      Tier 1 — Core      : Top N positions covering ~50% of portfolio value
                           → real-time provider (Finnhub), short TTL (15 min)
      Tier 2 — Major     : Next positions covering 50-80% of portfolio value
                           → batch provider (yfinance), medium TTL (30 min)
      Tier 3 — Standard  : Remaining (<20% coverage, many small positions)
                           → batch provider (yfinance), session TTL (60 min)

    Usage:
        portfolio = [  # from CDM
            {"symbol": "AAPL", "quantity": 100, "current_price": 150},
            ...
        ]
        tier = PortfolioUpdatePriority.tier_for_symbol("AAPL", portfolio)
        if tier <= 1:
            provider = finnhub
        else:
            provider = yfinance
    """

    @staticmethod
    def tier_for_symbol(symbol: str, portfolio: list) -> int:
        """
        Return tier (1, 2, or 3) for a given symbol based on portfolio allocation.
        portfolio: list of dicts with 'symbol', 'quantity', 'current_price'
        """
        # Compute total portfolio value
        total_value = sum(
            (h.get("quantity", 0) or 0) * (h.get("current_price", 0) or 0) for h in portfolio
        )
        if total_value == 0:
            return 3

        # Sort by holding value descending
        holdings = sorted(
            [
                (
                    h.get("symbol", ""),
                    (h.get("quantity", 0) or 0) * (h.get("current_price", 0) or 0),
                )
                for h in portfolio
            ],
            key=lambda x: x[1],
            reverse=True,
        )

        cumulative = 0.0
        for sym, value in holdings:
            cumulative += value
            pct = cumulative / total_value
            if sym == symbol:
                if pct <= 0.50:
                    return 1
                elif pct <= 0.80:
                    return 2
                else:
                    return 3
        return 3
