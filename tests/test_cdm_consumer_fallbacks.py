# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_eod_email_renders_cdm_camel_summary_and_compact_value_fallbacks():
    from ic_engine.rendering.eod_email_template import render_eod_email

    html = render_eod_email(
        {
            "date": "2026-04-27",
            "holdings": {
                "summary": {
                    "totalPortfolioValue": 100_000.0,
                    "netValue": 90_000.0,
                    "equityValue": 95_000.0,
                    "cashValue": 5_000.0,
                    "marginValue": 10_000.0,
                    "equityPct": 95.0,
                    "cashPct": 5.0,
                    "totalUnrealizedGainLoss": 1_200.0,
                    "totalUnrealizedGainLossPct": 1.2,
                },
                "top_equity": [
                    {
                        "symbol": "AAPL",
                        "sector": "Technology",
                        "marketValue": 25_000.0,
                        "unrealizedGainLossPct": 4.5,
                    }
                ],
            },
            "analyst": {},
            "news": {},
            "bonds": None,
            "performance": {},
            "fa_topics": [],
        }
    )

    assert "Portfolio Value: $100,000" in html
    assert "$90,000" in html
    assert "$10,000" in html
    assert "$25,000" in html
    assert "AAPL" in html


def test_fa_discussion_reads_cdm_camel_summary_fields():
    from ic_engine.commands.fa_discussion import _topics_from_holdings

    topics = _topics_from_holdings(
        {
            "summary": {
                "totalPortfolioValue": 100_000.0,
                "equityValue": 95_000.0,
                "bondValue": 5_000.0,
                "marginValue": 12_500.0,
                "equityPct": 95.0,
                "bondPct": 5.0,
            },
            "top_equity": [{"symbol": "AAPL", "market_value": 15_000.0, "sector": "Technology"}],
        }
    )

    titles = [topic["title"] for topic in topics]
    assert any("95% equities" in title for title in titles)
    assert any("Margin debt balance" in title for title in titles)
    assert any("AAPL represents 15.0%" in title for title in titles)


def test_lookup_symbol_falls_back_to_normalized_cdm_buckets(tmp_path, capsys):
    from ic_engine.commands.lookup import query_holdings_symbol

    raw_dir = tmp_path / ".raw"
    raw_dir.mkdir()
    (raw_dir / "holdings.json").write_text(
        json.dumps(
            {
                "portfolio": {
                    "equity": {
                        "AAPL": {
                            "shares": 10.0,
                            "market_value": 2_500.0,
                            "unrealized_gain_loss_pct": 3.0,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert query_holdings_symbol(tmp_path, "AAPL", None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["positions"][0]["asset_class"] == "equity"
    assert out["positions"][0]["value"] == 2_500.0


def test_news_fetch_planner_uses_market_value_from_normalized_holdings(tmp_path, monkeypatch):
    from ic_engine.commands.news_fetch_planner import NewsFetchPlanner

    holdings_file = tmp_path / "holdings.json"
    holdings_file.write_text(
        json.dumps(
            {
                "portfolio": {
                    "equity": {
                        "BIG": {"market_value": 90_000.0, "shares": 90.0, "current_price": 1000.0},
                        "SMALL": {
                            "market_value": 10_000.0,
                            "shares": 10.0,
                            "current_price": 1000.0,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    def fake_make_plan(cls, holdings, model_id=None):
        del cls, model_id
        captured.update(holdings)
        return "captured"

    monkeypatch.setattr(NewsFetchPlanner, "make_plan", classmethod(fake_make_plan))

    assert NewsFetchPlanner.make_plan_from_holdings_file(str(holdings_file)) == "captured"
    assert captured["BIG"]["value"] == 90_000.0
    assert captured["SMALL"]["value"] == 10_000.0


def test_news_fetch_planner_preserves_non_equity_asset_classes(tmp_path, monkeypatch):
    from ic_engine.commands.news_fetch_planner import NewsFetchPlanner

    holdings_file = tmp_path / "holdings.json"
    holdings_file.write_text(
        json.dumps(
            {
                "portfolio": {
                    "equity": {"AAPL": {"market_value": 80_000.0}},
                    "crypto": {"BTC": {"market_value": 10_000.0}},
                    "metals": {"GLD": {"market_value": 10_000.0}},
                }
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    def fake_make_plan(cls, holdings, model_id=None):
        del cls, model_id
        captured.update(holdings)
        return "captured"

    monkeypatch.setattr(NewsFetchPlanner, "make_plan", classmethod(fake_make_plan))

    assert NewsFetchPlanner.make_plan_from_holdings_file(str(holdings_file)) == "captured"
    assert captured["BTC"]["asset_type"] == "crypto"
    assert captured["GLD"]["asset_type"] == "metals"
    assert [symbol for symbol, *_ in NewsFetchPlanner._concentration_curve(captured)] == ["AAPL"]


def test_news_fetch_planner_aggregates_repeated_loader_symbols(tmp_path, monkeypatch):
    from ic_engine.commands.news_fetch_planner import NewsFetchPlanner

    holdings_file = tmp_path / "holdings.json"
    holdings_file.write_text(
        json.dumps(
            {
                "cdmVersion": "5.0",
                "portfolio": {
                    "portfolioState": {
                        "positions": [
                            {
                                "product": {"productIdentifier": {"identifier": "AAPL"}},
                                "asset": {"securityType": "Equity"},
                                "marketValue": 60_000.0,
                            },
                            {
                                "product": {"productIdentifier": {"identifier": "AAPL"}},
                                "asset": {"securityType": "Equity"},
                                "marketValue": 40_000.0,
                            },
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    def fake_make_plan(cls, holdings, model_id=None):
        del cls, model_id
        captured.update(holdings)
        return "captured"

    monkeypatch.setattr(NewsFetchPlanner, "make_plan", classmethod(fake_make_plan))

    assert NewsFetchPlanner.make_plan_from_holdings_file(str(holdings_file)) == "captured"
    assert captured["AAPL"]["value"] == 100_000.0


def test_analyst_symbol_extraction_uses_market_value_from_normalized_holdings():
    pytest.importorskip("yfinance")

    from ic_engine.commands.fetch_analyst_recommendations_parallel import (
        _extract_symbols_weighted_from_holdings,
    )

    symbols = _extract_symbols_weighted_from_holdings(
        {
            "portfolio": {
                "equity": {
                    "AAPL": {"market_value": 42_000.0},
                    "MSFT": {"value": 21_000.0},
                }
            }
        }
    )

    assert ("AAPL", 42_000.0) in symbols
    assert ("MSFT", 21_000.0) in symbols


def test_model_guardrails_canonical_total_accepts_cdm_summary(tmp_path, monkeypatch):
    from ic_engine.commands import model_guardrails

    holdings_file = tmp_path / "holdings.json"
    holdings_file.write_text(
        json.dumps({"portfolio": {"summary": {"totalPortfolioValue": 123_456.0}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_guardrails, "HOLDINGS_FILE", holdings_file)

    assert model_guardrails._canonical_total() == 123_456.0


def test_summary_extraction_uses_portfolio_summary_when_top_level_summary_is_empty():
    from ic_engine.services.summary_utils import extract_summary_block, normalize_summary_fields

    summary = normalize_summary_fields(
        extract_summary_block(
            {
                "summary": {},
                "portfolio": {
                    "summary": {
                        "totalPortfolioValue": 123_456.0,
                        "equityValue": 60_000.0,
                    }
                },
            }
        )
    )

    assert summary["total_value"] == 123_456.0
    assert summary["equity_value"] == 60_000.0


def test_holdings_artifact_accepts_cdm_summary_and_market_value(tmp_path):
    from ic_engine.commands._artifact_helpers import build_holdings_artifact

    out = tmp_path / "holdings.html"
    build_holdings_artifact(
        {
            "summary": {
                "totalPortfolioValue": 100_000.0,
                "equityValue": 80_000.0,
                "cashValue": 20_000.0,
                "totalUnrealizedGainLossPct": 2.5,
            },
            "top_equity": [
                {
                    "symbol": "AAPL",
                    "sector": "Technology",
                    "marketValue": 25_000.0,
                    "unrealizedGainLossPct": 4.5,
                }
            ],
        },
        str(out),
    )

    html = out.read_text(encoding="utf-8")
    assert "$100,000" in html
    assert "$25,000.00" in html
    assert "AAPL" in html


def test_stonkmode_holdings_summary_accepts_cdm_summary_and_market_value():
    from ic_engine.rendering.stonkmode import _summarize_holdings

    text = _summarize_holdings(
        {
            "summary": {
                "totalPortfolioValue": 100_000.0,
                "equityValue": 80_000.0,
                "cashValue": 20_000.0,
                "totalUnrealizedGainLossPct": 2.5,
            },
            "top_equity": [
                {
                    "symbol": "AAPL",
                    "marketValue": 25_000.0,
                    "weight_pct": 25.0,
                    "unrealizedGainLossPct": 4.5,
                }
            ],
        }
    )

    assert "Total portfolio: $100,000" in text
    assert "AAPL: $25,000" in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for PWA JS smoke")
def test_pwa_charts_top_equities_accepts_compact_value_fields():
    charts_path = Path("src/ic_engine/rendering/pwa/assets/charts.js").resolve()
    script = f"""
const fs = require('fs');
global.window = global;
let captured = '';
global.document = {{
  querySelector: () => ({{
    set innerHTML(value) {{ captured = value; }},
    get innerHTML() {{ return captured; }}
  }})
}};
eval(fs.readFileSync({json.dumps(str(charts_path))}, 'utf8'));
window.Charts.renderTopEquities({{
  summary: {{ total_value: 100000 }},
  top_equity: [{{ symbol: 'AAPL', value: 25000, weight_pct: 25, gl_pct: 4.5 }}]
}});
if (!captured.includes('$25,000') || !captured.includes('25.00%') || !captured.includes('4.50%')) {{
  throw new Error(captured);
}}
"""
    subprocess.run(["node", "-e", script], check=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for PWA JS smoke")
def test_pwa_app_holdings_accepts_top_equity_and_position_count_dict():
    app_path = Path("src/ic_engine/rendering/pwa/assets/app.js").resolve()
    script = f"""
const fs = require('fs');
const elements = {{}};
global.DASHBOARD_DATA = {{}};
global.document = {{
  addEventListener: () => {{}},
  querySelectorAll: () => [],
  querySelector: () => null,
  getElementById: (id) => elements[id] || (elements[id] = {{ innerHTML: '' }})
}};
const source = fs.readFileSync({json.dumps(str(app_path))}, 'utf8') + `
IC.data = {{
  holdings: {{
    summary: {{ totalPortfolioValue: 100000, position_count: {{ equity: 1, bond: 0 }} }},
    top_equity: [{{ symbol: 'AAPL', value: 25000, weight_pct: 25 }}],
    sector_weights: {{ Technology: 100 }}
  }}
}};
IC.renderHoldings();
const html = elements['holdings-content'].innerHTML;
if (!html.includes('$25,000.00') || !html.includes('25.00%') || html.includes('[object Object]')) {{
  throw new Error(html);
}}
`;
eval(source);
"""
    subprocess.run(["node", "-e", script], check=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for PWA JS smoke")
def test_pwa_integrated_cash_allocation_treats_compact_pct_as_points():
    dashboard_path = Path("src/ic_engine/rendering/pwa/dashboard-integrated.html").resolve()
    script = r"""
const fs = require('fs');
const html = fs.readFileSync(__DASHBOARD_PATH__, 'utf8');
const match = html.match(/<script>\s*([\s\S]*class DashboardApp[\s\S]*?)<\/script>/);
if (!match) {
  throw new Error('DashboardApp script not found');
}

const elements = {};
const makeElement = () => ({
  innerHTML: '',
  addEventListener: () => {},
  classList: { add: () => {}, remove: () => {} },
  value: '',
  textContent: '',
  appendChild: () => {},
  scrollTop: 0,
  scrollHeight: 0,
});

global.navigator = {};
global.window = {
  location: { protocol: 'http:', host: 'localhost' },
  addEventListener: () => {},
};
global.document = {
  querySelectorAll: () => [],
  querySelector: (selector) => {
    if (selector === '#holdings-table tbody') {
      return elements['holdings-table-tbody'] || (elements['holdings-table-tbody'] = makeElement());
    }
    return null;
  },
  getElementById: (id) => elements[id] || (elements[id] = makeElement()),
};
global.Plotly = { newPlot: () => {} };

eval(match[1] + `
const app = new DashboardApp();
app.renderHoldings({
  summary: { total_value: 100000, cash_pct: 0.5 },
  top_equity: [{ symbol: 'CASH', value: 500, quantity: 500, price: 1 }]
});
const metrics = elements['holdings-metrics'].innerHTML;
if (!metrics.includes('0.50%') || metrics.includes('50.00%')) {
  throw new Error(metrics);
}
`);
""".replace("__DASHBOARD_PATH__", json.dumps(str(dashboard_path)))
    subprocess.run(["node", "-e", script], check=True)
