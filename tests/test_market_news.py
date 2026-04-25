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
Tests for commands/fetch_market_news.py (v2.2 step 4.1).

Covers:
- All 4 topics dispatch without error
- Free-first: when Yahoo returns content, Finnhub is NOT called
- Free-first fallback: when Yahoo returns empty/error, Finnhub IS called
- ic_result envelope shape (script name, exit_code, duration_ms)
- Default topic when no --topic arg is "general"
- Invalid topic returns ic_result with exit_code=1 and allowed_topics list
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
_SKILL_ROOT = Path(__file__).parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

# ---------------------------------------------------------------------------
# Helper: minimal fake news article from Yahoo
# ---------------------------------------------------------------------------


def _fake_yf_article(title: str = "Market surges higher") -> dict:
    return {
        "content": {
            "title": title,
            "summary": "Markets rose strongly on earnings beat.",
            "provider": {"displayName": "Yahoo Finance"},
            "canonicalUrl": {"url": "https://example.com/news/1"},
            "pubDate": "2026-04-24T10:00:00",
        }
    }


def _fake_ticker(articles=None):
    """Return a mock yfinance Ticker with news attribute set."""
    ticker = MagicMock()
    ticker.news = articles if articles is not None else [_fake_yf_article()]
    return ticker


# ---------------------------------------------------------------------------
# Tests: each of 4 topics dispatches without error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("topic", ["general", "forex", "crypto", "merger"])
def test_all_topics_dispatch(topic):
    """fetch_market_news returns a dict with 'topic', 'headlines', 'summary'."""
    from commands.fetch_market_news import fetch_market_news

    fake_article = {
        "title": f"{topic} headline",
        "summary": "Test summary.",
        "source": "TestSource",
        "url": "https://example.com",
        "publish_date": "2026-04-24T10:00:00",
        "sentiment": "neutral",
        "confidence": 0.3,
        "provider": "yahoo" if topic != "merger" else "finnhub",
    }

    # merger has no Yahoo sources; it goes straight to Finnhub
    yahoo_articles = [fake_article] if topic != "merger" else []
    finnhub_articles = [fake_article] if topic == "merger" else []

    with (
        patch("commands.fetch_market_news._fetch_yahoo_news", return_value=yahoo_articles),
        patch("commands.fetch_market_news._fetch_finnhub_news", return_value=finnhub_articles),
    ):
        result = fetch_market_news(topic=topic)

    assert result["topic"] == topic
    assert "headlines" in result
    assert "summary" in result
    assert len(result["headlines"]) >= 1


# ---------------------------------------------------------------------------
# Tests: free-first — when Yahoo returns content, Finnhub NOT called
# ---------------------------------------------------------------------------


def test_free_first_yahoo_present_finnhub_not_called():
    """When Yahoo returns articles, Finnhub is not called (quota preserved)."""
    from commands.fetch_market_news import fetch_market_news

    yahoo_articles = [
        {
            "title": "S&P 500 reaches all-time high",
            "summary": "Markets climbed on strong data.",
            "source": "Reuters",
            "url": "https://reuters.com/1",
            "publish_date": "2026-04-24T09:00:00",
            "sentiment": "positive",
            "confidence": 0.8,
            "provider": "yahoo",
        }
    ]

    with (
        patch(
            "commands.fetch_market_news._fetch_yahoo_news", return_value=yahoo_articles
        ) as mock_yahoo,
        patch("commands.fetch_market_news._fetch_finnhub_news", return_value=[]) as mock_finnhub,
    ):
        result = fetch_market_news(topic="general")

    mock_yahoo.assert_called_once()
    mock_finnhub.assert_not_called()
    assert result["headlines"][0]["provider"] == "yahoo"


# ---------------------------------------------------------------------------
# Tests: free-first fallback — when Yahoo empty, Finnhub IS called
# ---------------------------------------------------------------------------


def test_free_first_fallback_finnhub_called_when_yahoo_empty():
    """When Yahoo returns empty list, Finnhub is called as fallback."""
    from commands.fetch_market_news import fetch_market_news

    finnhub_articles = [
        {
            "title": "Currency markets steady",
            "summary": "EUR/USD held range.",
            "source": "Finnhub",
            "url": "https://finnhub.io/news/1",
            "publish_date": "2026-04-24T09:00:00",
            "sentiment": "neutral",
            "confidence": 0.3,
            "provider": "finnhub",
        }
    ]

    with (
        patch("commands.fetch_market_news._fetch_yahoo_news", return_value=[]) as mock_yahoo,
        patch(
            "commands.fetch_market_news._fetch_finnhub_news", return_value=finnhub_articles
        ) as mock_finnhub,
    ):
        result = fetch_market_news(topic="forex")

    mock_yahoo.assert_called_once()
    mock_finnhub.assert_called_once_with("forex", 10)
    assert result["headlines"][0]["provider"] == "finnhub"


def test_merger_topic_uses_finnhub_primary_no_yahoo_symbols():
    """merger topic has no Yahoo symbols; Finnhub is called directly."""
    from commands.fetch_market_news import _YAHOO_SOURCES, fetch_market_news

    assert _YAHOO_SOURCES.get("merger", []) == [], "merger should have no Yahoo sources"

    finnhub_articles = [
        {
            "title": "Big corp acquires startup",
            "summary": "Deal valued at $5B.",
            "source": "Finnhub",
            "url": "https://finnhub.io/news/2",
            "publish_date": "2026-04-24T09:30:00",
            "sentiment": "positive",
            "confidence": 0.6,
            "provider": "finnhub",
        }
    ]

    with (
        patch("commands.fetch_market_news._fetch_yahoo_news") as mock_yahoo,
        patch("commands.fetch_market_news._fetch_finnhub_news", return_value=finnhub_articles),
    ):
        result = fetch_market_news(topic="merger")

    mock_yahoo.assert_not_called()
    assert result["headlines"][0]["provider"] == "finnhub"


# ---------------------------------------------------------------------------
# Tests: ic_result envelope shape
# ---------------------------------------------------------------------------


def test_ic_result_envelope_present():
    """Result from main() includes ic_result with required fields."""
    import io

    from commands.fetch_market_news import main as market_news_main

    fake_articles = [
        {
            "title": "Crypto soars",
            "summary": "BTC up 5%.",
            "source": "CryptoNews",
            "url": "https://example.com/c",
            "publish_date": "2026-04-24T11:00:00",
            "sentiment": "positive",
            "confidence": 0.7,
            "provider": "yahoo",
        }
    ]

    with (
        patch("sys.argv", ["fetch_market_news.py", "--topic", "crypto"]),
        patch("commands.fetch_market_news._fetch_yahoo_news", return_value=fake_articles),
        patch("commands.fetch_market_news._fetch_finnhub_news", return_value=[]),
        patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
    ):
        import json as _json

        exit_code = market_news_main()
        output = mock_stdout.getvalue()

    result = _json.loads(output)
    assert "ic_result" in result
    ic = result["ic_result"]
    assert ic["script"] == "fetch_market_news.py"
    assert ic["exit_code"] == 0
    assert isinstance(ic["duration_ms"], int)
    assert exit_code == 0


def test_ic_result_exit_code_zero_on_success():
    """Successful fetch produces exit_code=0."""
    from commands.fetch_market_news import fetch_market_news

    with (
        patch(
            "commands.fetch_market_news._fetch_yahoo_news",
            return_value=[
                {
                    "title": "OK",
                    "summary": "",
                    "source": "",
                    "url": "",
                    "publish_date": "",
                    "sentiment": "neutral",
                    "confidence": 0.3,
                    "provider": "yahoo",
                }
            ],
        ),
        patch("commands.fetch_market_news._fetch_finnhub_news", return_value=[]),
    ):
        result = fetch_market_news(topic="general")

    # fetch_market_news returns raw result dict; ic_result added by main()
    assert "error" not in result


# ---------------------------------------------------------------------------
# Tests: default topic = "general"
# ---------------------------------------------------------------------------


def test_default_topic_is_general():
    """Calling fetch_market_news() with no topic arg uses 'general'."""
    from commands.fetch_market_news import fetch_market_news

    with (
        patch(
            "commands.fetch_market_news._fetch_yahoo_news",
            return_value=[
                {
                    "title": "Market update",
                    "summary": "",
                    "source": "",
                    "url": "",
                    "publish_date": "",
                    "sentiment": "neutral",
                    "confidence": 0.3,
                    "provider": "yahoo",
                }
            ],
        ),
        patch("commands.fetch_market_news._fetch_finnhub_news", return_value=[]),
    ):
        result = fetch_market_news()

    assert result["topic"] == "general"


# ---------------------------------------------------------------------------
# Tests: invalid topic
# ---------------------------------------------------------------------------


def test_invalid_topic_returns_error_and_allowed_topics():
    """An unknown topic returns an error dict with allowed_topics list."""
    from commands.fetch_market_news import VALID_TOPICS, fetch_market_news

    result = fetch_market_news(topic="astrology")
    assert "error" in result
    assert "allowed_topics" in result
    assert set(result["allowed_topics"]) == set(VALID_TOPICS)


def test_invalid_topic_exit_code_one_in_main():
    """main() returns exit_code=1 for invalid topic."""
    import io
    import json as _json

    from commands.fetch_market_news import main as market_news_main

    with (
        patch("sys.argv", ["fetch_market_news.py", "--topic", "general"]),
        # Inject error by patching fetch_market_news to return error dict
        patch(
            "commands.fetch_market_news.fetch_market_news",
            return_value={
                "topic": "bad",
                "headlines": [],
                "summary": {},
                "error": "Unknown topic",
                "allowed_topics": ["general", "forex", "crypto", "merger"],
            },
        ),
        patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
    ):
        exit_code = market_news_main()
        output = mock_stdout.getvalue()

    result = _json.loads(output)
    assert exit_code == 1
    assert result["ic_result"]["exit_code"] == 1
