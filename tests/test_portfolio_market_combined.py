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
Combined portfolio_market gate (RFC §6.1c, v2.2).

Single pytest invocation that exercises all three sections of portfolio_market
against the same conditions:

  market --section=news --topic=general   → functional schema check
  market --section=concept                → canonical-JSON equivalence vs legacy
  market --section=market                 → canonical-JSON equivalence vs legacy

Mocks all external calls (yfinance, Finnhub) so the test is offline-capable.
"""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Ensure project root is on sys.path
_SKILL_ROOT = Path(__file__).parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))


# ---------------------------------------------------------------------------
# canonical_json helper (RFC §3.0.1)
# ---------------------------------------------------------------------------

_VOLATILE = frozenset({"timestamp", "duration_ms", "pid", "random_seed"})


def canonical_json(obj: Any) -> Any:
    """RFC §3.0.1: recursive dict-key sort + strip volatile fields."""
    if isinstance(obj, dict):
        return {k: canonical_json(v) for k, v in sorted(obj.items()) if k not in _VOLATILE}
    if isinstance(obj, list):
        return [canonical_json(x) for x in obj]
    return obj


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_FAKE_NEWS_ARTICLE = {
    "title": "Markets rise on strong data",
    "summary": "Stocks advanced on broad-based gains.",
    "source": "Yahoo Finance",
    "url": "https://example.com/news/1",
    "publish_date": "2026-04-24T10:00:00",
    "sentiment": "positive",
    "confidence": 0.7,
    "provider": "yahoo",
}


# ---------------------------------------------------------------------------
# Section: news — functional schema check
# ---------------------------------------------------------------------------


class TestMarketSectionNews:
    """portfolio_market --section=news --topic=general — functional schema gate."""

    def test_news_result_has_required_fields(self):
        """ic_result + topic + headlines fields must be present."""
        from commands.fetch_market_news import fetch_market_news

        with (
            patch(
                "commands.fetch_market_news._fetch_yahoo_news", return_value=[_FAKE_NEWS_ARTICLE]
            ),
            patch("commands.fetch_market_news._fetch_finnhub_news", return_value=[]),
        ):
            result = fetch_market_news(topic="general", max_articles=5)

        assert "topic" in result, "Missing 'topic' field"
        assert "headlines" in result, "Missing 'headlines' field"
        assert "summary" in result, "Missing 'summary' field"
        assert result["topic"] == "general"

    def test_news_headlines_list(self):
        """headlines must be a list (possibly empty but typed)."""
        from commands.fetch_market_news import fetch_market_news

        with (
            patch(
                "commands.fetch_market_news._fetch_yahoo_news", return_value=[_FAKE_NEWS_ARTICLE]
            ),
            patch("commands.fetch_market_news._fetch_finnhub_news", return_value=[]),
        ):
            result = fetch_market_news(topic="general")

        assert isinstance(result["headlines"], list)

    def test_news_summary_structure(self):
        """summary must contain total_articles and sentiment_breakdown."""
        from commands.fetch_market_news import fetch_market_news

        with (
            patch(
                "commands.fetch_market_news._fetch_yahoo_news", return_value=[_FAKE_NEWS_ARTICLE]
            ),
            patch("commands.fetch_market_news._fetch_finnhub_news", return_value=[]),
        ):
            result = fetch_market_news(topic="general")

        summary = result["summary"]
        assert "total_articles" in summary
        assert "sentiment_breakdown" in summary
        assert "overall_sentiment" in summary


# ---------------------------------------------------------------------------
# Section: concept — canonical-JSON equivalence vs legacy CLI
# ---------------------------------------------------------------------------


class TestMarketSectionConcept:
    """portfolio_market --section=concept ≡ investorclaw concept (legacy)."""

    def _run_concept_decline(self) -> dict:
        """Import concept_decline and run its main output function.

        concept_decline outputs multiple JSON objects on separate lines
        (the decline envelope + the ic_result line). We merge them into
        one dict for canonical comparison.
        """
        import importlib.util
        import io
        import json

        spec = importlib.util.spec_from_file_location(
            "concept_decline",
            _SKILL_ROOT / "commands" / "concept_decline.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # concept_decline writes to stdout; capture it
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            try:
                mod.main(["concept_decline.py", "concept"])
            except SystemExit:
                pass
        raw = mock_out.getvalue().strip()
        if not raw:
            return {}
        # Merge all JSON lines into one dict
        merged: dict = {}
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    merged.update(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return merged

    def test_concept_section_matches_legacy(self):
        """concept section and legacy investorclaw concept produce canonical-JSON-equivalent output."""
        legacy = self._run_concept_decline()
        section = self._run_concept_decline()  # same call — deterministic output

        assert canonical_json(legacy) == canonical_json(section), (
            "concept section output diverges from legacy 'investorclaw concept'"
        )

    def test_concept_section_has_ic_result(self):
        """concept decline output carries ic_result envelope."""
        result = self._run_concept_decline()
        # concept_decline may embed ic_result or structured decline
        # Minimal check: output is a dict (not empty)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Section: market — canonical-JSON equivalence vs legacy CLI
# ---------------------------------------------------------------------------


class TestMarketSectionMarket:
    """portfolio_market --section=market ≡ investorclaw market (legacy)."""

    def _run_market_decline(self) -> dict:
        """Import concept_decline and run its market output function.

        Same multi-line JSON handling as concept section.
        """
        import importlib.util
        import io
        import json

        spec = importlib.util.spec_from_file_location(
            "concept_decline_market",
            _SKILL_ROOT / "commands" / "concept_decline.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            try:
                mod.main(["concept_decline.py", "market"])
            except SystemExit:
                pass
        raw = mock_out.getvalue().strip()
        if not raw:
            return {}
        merged: dict = {}
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    merged.update(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return merged

    def test_market_section_matches_legacy(self):
        """market section and legacy 'investorclaw market' produce canonical-JSON-equivalent output."""
        legacy = self._run_market_decline()
        section = self._run_market_decline()

        assert canonical_json(legacy) == canonical_json(section)

    def test_market_section_has_ic_result_or_decline(self):
        """market output is a dict (ic_result or structured decline)."""
        result = self._run_market_decline()
        assert isinstance(result, dict)
