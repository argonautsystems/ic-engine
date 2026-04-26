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
Model ID normalization tests for lib/context_budget.get_model_context_window().

Verifies that the lookup handles edge cases correctly: provider prefixes,
mixed case, whitespace, partial names, and that previously incorrect or
speculative values have been fixed to match public specifications.
"""

import sys
from pathlib import Path

_skill_root = Path(__file__).parent.parent
if str(_skill_root) not in sys.path:
    sys.path.insert(0, str(_skill_root))

import pytest

from ic_engine.config.schema import normalize_portfolio
from ic_engine.config.schema_v2_pydantic import convert_cdm_to_canonical
from ic_engine.models.context_budget import get_model_context_window

# ---------------------------------------------------------------------------
# Provider-prefix stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefixed, bare",
    [
        ("openai/gpt-4.1", "gpt-4.1"),
        ("openai/gpt-4.1-mini", "gpt-4.1-mini"),
        ("openai/gpt-4.1-nano", "gpt-4.1-nano"),
        ("xai/grok-4-1-fast", "grok-4-1-fast"),
        ("xai/grok-4-1-fast-reasoning", "grok-4-1-fast-reasoning"),
    ],
)
def test_provider_prefix_gives_same_result_as_bare(prefixed, bare):
    assert get_model_context_window(prefixed) == get_model_context_window(bare)


# ---------------------------------------------------------------------------
# Case normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "GPT-4.1",
        "Gpt-4.1",
        "gpt-4.1",
        "CLAUDE-SONNET-4-6",
        "Claude-Sonnet-4-6",
        "GEMINI-2.5-FLASH",
    ],
)
def test_case_insensitive_lookup(model_id):
    lower = model_id.lower()
    assert get_model_context_window(model_id) == get_model_context_window(lower)


# ---------------------------------------------------------------------------
# Whitespace stripping
# ---------------------------------------------------------------------------


def test_leading_trailing_whitespace_stripped():
    assert get_model_context_window("  gpt-4.1  ") == get_model_context_window("gpt-4.1")


# ---------------------------------------------------------------------------
# Corrections from public-spec audit (Apr 2026)
# ---------------------------------------------------------------------------


def test_gemini_2_5_flash_lite_is_1m_not_4m():
    """gemini-2.5-flash-lite was incorrectly set to 4M; spec says 1_048_576."""
    assert get_model_context_window("gemini-2.5-flash-lite") == 1_048_576


def test_claude_sonnet_4_6_is_1m():
    """claude-sonnet-4-6 context upgraded to 1M with the 4.6 generation."""
    assert get_model_context_window("claude-sonnet-4-6") == 1_048_576


def test_claude_opus_4_6_is_1m():
    assert get_model_context_window("claude-opus-4-6") == 1_048_576


def test_claude_4_5_family_is_200k():
    for model in ("claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"):
        assert get_model_context_window(model) == 200_000, f"Failed for {model}"


def test_grok_4_is_256k():
    """grok-4 (not grok-4-1-fast) has 256K context per xAI docs."""
    assert get_model_context_window("grok-4") == 262_144


def test_sonar_pro_is_200k():
    """sonar-pro has 200K context vs sonar at 128K."""
    assert get_model_context_window("sonar") == 128_000
    assert get_model_context_window("sonar-pro") == 200_000


# ---------------------------------------------------------------------------
# Fallback behaviour
# ---------------------------------------------------------------------------


def test_completely_unknown_model_falls_back_to_128k():
    assert get_model_context_window("zz-fake-model-9999") == 128_000


def test_none_like_empty_string_falls_back():
    assert get_model_context_window("") == 128_000


def test_custom_default_respected():
    assert get_model_context_window("zz-fake-model-9999", default=32_000) == 32_000


def test_cdm_bond_market_value_preserved_in_pydantic_conversion():
    portfolio = convert_cdm_to_canonical(
        {
            "cdmVersion": "5.x",
            "portfolio": {
                "portfolioState": {
                    "positions": [
                        {
                            "product": {"productIdentifier": {"identifier": "912797LS8"}},
                            "asset": {
                                "productIdentifier": {"identifier": "912797LS8"},
                                "securityType": "Bond",
                            },
                            "priceQuantity": {
                                "quantity": {"amount": 100000.0},
                                "currentPrice": {"amount": 99.769},
                            },
                            "marketValue": 99769.0,
                        }
                    ]
                }
            },
        }
    )

    assert portfolio.holdings[0].asset_type == "bond"
    assert portfolio.holdings[0].current_value == pytest.approx(99769.0)


def test_normalize_portfolio_only_adds_price_percent_for_bonds():
    normalized = normalize_portfolio(
        {
            "cdmVersion": "5.x",
            "portfolio": {
                "portfolioState": {
                    "positions": [
                        {
                            "product": {"productIdentifier": {"identifier": "AAPL"}},
                            "asset": {
                                "productIdentifier": {"identifier": "AAPL"},
                                "securityType": "Equity",
                                "securityName": "Apple Inc.",
                            },
                            "priceQuantity": {
                                "quantity": {"amount": 10.0},
                                "currentPrice": {"amount": 150.0},
                            },
                            "marketValue": 1500.0,
                        },
                        {
                            "product": {"productIdentifier": {"identifier": "912797LS8"}},
                            "asset": {
                                "productIdentifier": {"identifier": "912797LS8"},
                                "securityType": "Bond",
                                "securityName": "UST",
                            },
                            "priceQuantity": {
                                "quantity": {"amount": 100000.0},
                                "currentPrice": {"amount": 99.769},
                            },
                            "marketValue": 99769.0,
                        },
                    ]
                }
            },
        }
    )

    assert "price_percent" not in normalized["portfolio"]["equity"]["AAPL"]
    assert normalized["portfolio"]["bond"]["912797LS8"]["price_percent"] == pytest.approx(99.769)


def test_cdm_equity_market_value_preserved_in_pydantic_conversion():
    """Regression: Equity positions must preserve explicit marketValue if present (not recompute)."""
    portfolio = convert_cdm_to_canonical(
        {
            "cdmVersion": "5.x",
            "portfolio": {
                "portfolioState": {
                    "positions": [
                        {
                            "product": {"productIdentifier": {"identifier": "AAPL"}},
                            "asset": {
                                "productIdentifier": {"identifier": "AAPL"},
                                "securityType": "Equity",
                                "securityName": "Apple Inc.",
                            },
                            "priceQuantity": {
                                "quantity": {"amount": 100.0},
                                "currentPrice": {"amount": 150.0},
                            },
                            "marketValue": 14900.0,  # Explicit: 14,900 (realistic: post-split adjustment lag)
                        }
                    ]
                }
            },
        }
    )

    assert len(portfolio.holdings) == 1
    assert portfolio.holdings[0].asset_type == "equity"
    # Must preserve explicit marketValue, not recompute as 100*150=15,000
    assert portfolio.holdings[0].current_value == pytest.approx(14900.0), (
        f"Equity marketValue should be 14900 (explicit), got {portfolio.holdings[0].current_value}"
    )
    # Sanity: divergence indicates real-world data (post-split, staleness, etc)
    assert portfolio.holdings[0].current_value != (100 * 150.0), (
        "Test fixture requires explicit marketValue ≠ quantity*price to validate preservation"
    )
