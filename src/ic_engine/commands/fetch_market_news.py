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
Fetch market-wide news by topic (general / forex / crypto / merger).

Unlike fetch_portfolio_news.py, this command iterates over topic-specific
source sets — NOT held-position tickers.  It does not load portfolio holdings.

v2.2 ships 4 functional topics (matching Finnhub native categories):
  general   — Yahoo index tickers (^GSPC/^DJI/^IXIC) + Finnhub general
  forex     — Yahoo FX tickers (EURUSD=X/DX-Y.NYB) + Finnhub forex
  crypto    — Yahoo crypto tickers (BTC-USD/ETH-USD) + Finnhub crypto
  merger    — Finnhub merger PRIMARY (TODO: GDELT is RFC primary for v2.2
               step 6 but the GDELT adapter does not exist yet; Finnhub
               promoted to primary for initial ship — see RFC r2.3 fix #6)

Free-first chain: Yahoo (keyless) → Finnhub (key required).
When Yahoo returns content Finnhub is NOT called (quota preserved).

Sentiment classifier reuses the keyword tables from PortfolioNewsAnalyzer
(fetched via import; no code duplication).

Output JSON:
  {
    "topic": "...",
    "headlines": [...],
    "summary": {...},
    "ic_result": {"script": "fetch_market_news.py", "exit_code": 0,
                  "duration_ms": int}
  }

CLI:
  python3 fetch_market_news.py [--topic {general|forex|crypto|merger}]
                               [--max-articles N]
                               [--verbose]
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Bootstrap project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_TOPICS = ("general", "forex", "crypto", "merger")

# Yahoo Finance proxy tickers for each topic (free, keyless)
_YAHOO_SOURCES: Dict[str, List[str]] = {
    "general": ["^GSPC", "^DJI", "^IXIC"],
    "forex": ["EURUSD=X", "DX-Y.NYB"],
    "crypto": ["BTC-USD", "ETH-USD"],
    "merger": [],  # GDELT is RFC primary; adapter not yet implemented; Finnhub used
}

# ---------------------------------------------------------------------------
# Sentiment helpers (reused from PortfolioNewsAnalyzer — import, don't copy)
# ---------------------------------------------------------------------------

try:
    from ic_engine.commands.fetch_portfolio_news import PortfolioNewsAnalyzer as _PNA

    _POSITIVE_KEYWORDS = _PNA.POSITIVE_KEYWORDS
    _NEGATIVE_KEYWORDS = _PNA.NEGATIVE_KEYWORDS
except Exception:
    # Fallback: minimal keyword tables so the module is importable in test
    # environments that may not have the full dependency tree installed.
    _POSITIVE_KEYWORDS = [
        "beat",
        "surge",
        "gain",
        "profit",
        "growth",
        "strong",
        "record",
        "rise",
        "rally",
        "upgrade",
        "outperform",
        "bullish",
        "approved",
    ]
    _NEGATIVE_KEYWORDS = [
        "drop",
        "decline",
        "loss",
        "fall",
        "crash",
        "weak",
        "downgrade",
        "underperform",
        "bearish",
        "warning",
        "miss",
        "shortfall",
    ]


def _score_sentiment(text: str) -> Tuple[str, float]:
    """Keyword-based sentiment scorer.

    Returns (sentiment, confidence) where sentiment is 'positive', 'negative',
    or 'neutral'.  Identical logic to PortfolioNewsAnalyzer.simple_sentiment.
    """
    if not text:
        return "neutral", 0.0
    text_lower = text.lower()
    pos = sum(text_lower.count(kw) for kw in _POSITIVE_KEYWORDS)
    neg = sum(text_lower.count(kw) for kw in _NEGATIVE_KEYWORDS)
    total = pos + neg
    if total == 0:
        return "neutral", 0.3
    if pos > neg:
        return "positive", min(1.0, pos / max(10, total))
    if neg > pos:
        return "negative", min(1.0, neg / max(10, total))
    return "neutral", 0.5


# ---------------------------------------------------------------------------
# Yahoo news fetcher (free, keyless)
# ---------------------------------------------------------------------------


def _fetch_yahoo_news(symbols: List[str], max_articles: int) -> List[Dict]:
    """Fetch news via yfinance Ticker.news for a list of proxy symbols."""
    articles: List[Dict] = []
    seen_urls: set = set()

    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not available; skipping Yahoo news fetch")
        return []

    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            raw_news = ticker.news or []
            for item in raw_news:
                _content = item.get("content", {})
                if isinstance(_content, dict) and _content:
                    title = _content.get("title", "") or item.get("title", "")
                    summary = (
                        _content.get("summary", "")
                        or _content.get("description", "")
                        or item.get("summary", "")
                    )
                    source = _content.get("provider", {}).get("displayName", "") or item.get(
                        "source", "Unknown"
                    )
                    url = (
                        _content.get("canonicalUrl", {}).get("url", "")
                        or _content.get("clickThroughUrl", {}).get("url", "")
                        or item.get("link", "")
                    )
                    pub = _content.get("pubDate", "") or _content.get("displayTime", "")
                    pub_date = pub[:19] if pub else ""
                else:
                    title = item.get("title", "")
                    summary = item.get("summary", "")
                    source = item.get("source", "Unknown")
                    url = item.get("link", "")
                    ts = item.get("providerPublishTime", 0)
                    from datetime import datetime

                    pub_date = datetime.fromtimestamp(ts).isoformat() if ts else ""

                if not title:
                    continue
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)

                sentiment, confidence = _score_sentiment(f"{title} {summary}")
                articles.append(
                    {
                        "title": title,
                        "summary": (summary or "")[:300],
                        "source": source,
                        "url": url,
                        "publish_date": pub_date,
                        "sentiment": sentiment,
                        "confidence": round(confidence, 3),
                        "provider": "yahoo",
                    }
                )
        except Exception as e:
            logger.warning(f"Yahoo news fetch for {sym}: {e}")

        if len(articles) >= max_articles:
            break

    return articles[:max_articles]


# ---------------------------------------------------------------------------
# Finnhub general-news fetcher (freemium, key required)
# ---------------------------------------------------------------------------


def _fetch_finnhub_news(category: str, max_articles: int) -> List[Dict]:
    """Fetch general market news from Finnhub by category.

    Requires FINNHUB_KEY / FINNHUB_API_KEY environment variable.
    """
    try:
        from ic_engine.providers.price_provider import FinnhubProvider

        provider = FinnhubProvider()
    except (ImportError, ValueError) as e:
        logger.info(f"Finnhub unavailable ({e}); skipping Finnhub news")
        return []

    raw = provider.get_general_news(category)
    articles: List[Dict] = []
    for item in raw[:max_articles]:
        text = f"{item.get('headline', '')} {item.get('summary', '')}"
        sentiment, confidence = _score_sentiment(text)
        articles.append(
            {
                "title": item.get("headline", ""),
                "summary": (item.get("summary", "") or "")[:300],
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "publish_date": item.get("datetime", ""),
                "sentiment": sentiment,
                "confidence": round(confidence, 3),
                "provider": "finnhub",
            }
        )
    return articles


# ---------------------------------------------------------------------------
# Massive market movers (additive market context; best-effort)
# ---------------------------------------------------------------------------

# Process-lifetime memo so repeated topic fetches don't multiply API calls
# (2 calls total per process: gainers + losers).
_MOVERS_CACHE: Optional[Dict[str, List[Dict]]] = None


def _fetch_market_movers(top: int = 5) -> Dict[str, List[Dict]]:
    """Top US-equity gainers + losers via Massive (memoized per process).

    Best-effort: returns {} when MASSIVE_API_KEY is unset or any failure
    occurs — callers degrade to the existing payload without movers.
    """
    global _MOVERS_CACHE
    if _MOVERS_CACHE is not None:
        return _MOVERS_CACHE

    import os

    if not os.getenv("MASSIVE_API_KEY"):
        _MOVERS_CACHE = {}
        return _MOVERS_CACHE
    try:
        from ic_engine.providers.price_provider import MassiveProvider

        provider = MassiveProvider()
    except Exception as e:
        logger.debug(f"Massive unavailable for market movers: {e}")
        _MOVERS_CACHE = {}
        return _MOVERS_CACHE

    movers: Dict[str, List[Dict]] = {}
    for direction in ("gainers", "losers"):
        try:
            rows = provider.get_market_movers(direction, top=top)
            if rows:
                movers[direction] = rows
        except Exception as e:
            logger.debug(f"market_movers({direction}) failed: {e}")
    _MOVERS_CACHE = movers
    return _MOVERS_CACHE


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------


def fetch_market_news(topic: str = "general", max_articles: int = 10) -> Dict:
    """Fetch market-wide news for *topic*.

    Returns a result dict suitable for JSON serialization.
    Raises ValueError for invalid topics.
    """
    if topic not in VALID_TOPICS:
        return {
            "topic": topic,
            "headlines": [],
            "summary": {},
            "error": f"Unknown topic: {topic!r}",
            "allowed_topics": list(VALID_TOPICS),
        }

    headlines: List[Dict] = []

    # Free-first: try Yahoo
    yahoo_symbols = _YAHOO_SOURCES.get(topic, [])
    if yahoo_symbols:
        logger.info(f"Fetching Yahoo news for topic={topic} ({yahoo_symbols})")
        yahoo_articles = _fetch_yahoo_news(yahoo_symbols, max_articles)
        if yahoo_articles:
            logger.info(f"Yahoo returned {len(yahoo_articles)} articles; skipping Finnhub")
            headlines = yahoo_articles
        else:
            logger.info("Yahoo returned no articles; falling back to Finnhub")
            headlines = _fetch_finnhub_news(topic, max_articles)
    else:
        # No Yahoo symbols defined for this topic (e.g. merger) → Finnhub directly
        logger.info(f"No Yahoo sources for topic={topic}; fetching Finnhub")
        headlines = _fetch_finnhub_news(topic, max_articles)

    # Trim to max_articles
    headlines = headlines[:max_articles]

    # Compute summary
    pos = sum(1 for h in headlines if h.get("sentiment") == "positive")
    neg = sum(1 for h in headlines if h.get("sentiment") == "negative")
    neu = len(headlines) - pos - neg
    overall = "positive" if pos > neg else ("negative" if neg > pos else "neutral")

    summary = {
        "total_articles": len(headlines),
        "sentiment_breakdown": {
            "positive": pos,
            "negative": neg,
            "neutral": neu,
        },
        "overall_sentiment": overall,
        "providers_used": list({h.get("provider", "") for h in headlines} - {""}),
    }

    result = {
        "topic": topic,
        "headlines": headlines,
        "summary": summary,
    }

    # Additive Massive market context: top 5 gainers + losers. Best-effort —
    # omitted entirely when Massive is absent or the fetch fails.
    try:
        movers = _fetch_market_movers(top=5)
        if movers:
            result["market_movers"] = movers
    except Exception as e:
        logger.debug(f"market_movers enrichment skipped: {e}")

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """CLI entry point. Returns exit code (0 = success, 1 = error)."""
    parser = argparse.ArgumentParser(
        description="Fetch market-wide news by topic (InvestorClaw v2.2)"
    )
    parser.add_argument(
        "--topic",
        choices=list(VALID_TOPICS),
        default="general",
        help="News topic: general | forex | crypto | merger (default: general)",
    )
    parser.add_argument(
        "--max-articles",
        type=int,
        default=10,
        metavar="N",
        help="Maximum number of headlines to return (default: 10)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed article list to stderr",
    )
    args = parser.parse_args()

    t0 = time.time()
    exit_code = 0

    try:
        result = fetch_market_news(topic=args.topic, max_articles=args.max_articles)
        if "error" in result:
            exit_code = 1
    except Exception as exc:
        logger.error(f"fetch_market_news failed: {exc}")
        result = {
            "topic": getattr(args, "topic", "general"),
            "headlines": [],
            "summary": {},
            "error": str(exc),
        }
        exit_code = 1

    duration_ms = int((time.time() - t0) * 1000)

    # Build ic_result envelope
    ic_result = {
        "script": "fetch_market_news.py",
        "exit_code": exit_code,
        "duration_ms": duration_ms,
    }
    result["ic_result"] = ic_result

    if args.verbose:
        for item in result.get("headlines", []):
            print(
                f"[{item.get('sentiment', 'neutral').upper()}] {item.get('title', '')}",
                file=sys.stderr,
            )
            if item.get("summary"):
                print(f"  {item['summary'][:120]}", file=sys.stderr)

    print(json.dumps(result, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
