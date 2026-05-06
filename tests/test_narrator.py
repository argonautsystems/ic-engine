from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ic_engine.runtime.envelope import Envelope, attach_hmac, new_ic_result
from ic_engine.runtime.narrator import (
    OUT_OF_SCOPE_RESPONSE,
    FabricationError,
    _question_mode,
    narrate,
    validate_narration,
)


@pytest.fixture(autouse=True)
def _hmac_key(monkeypatch):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_HMAC_KEY", "narrator-test-key")


@pytest.fixture
def envelope() -> Envelope:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    env: Envelope = {
        "schema_version": "v2.5.0",
        "generated_at": now,
        "portfolio_id": "portfolio-test",
        "ic_result": new_ic_result("run_full", "run-id"),
        "sections": {
            "holdings": {
                "summary": {
                    "total_value": "$100.00",
                    "equity_pct": "60.0%",
                    "coverage_ratio": "1.2x",
                }
            }
        },
        "section_meta": {
            "holdings": {
                "computed_at": now,
                "ttl_seconds": 300,
                "source": "holdings",
                "status": "success",
            }
        },
        "failed_sections": [],
    }
    return attach_hmac(env)


def test_narrator_accepts_verbatim_numbers(envelope, monkeypatch):
    def _fake_llm(system_prompt, user_prompt):
        assert "Use ONLY data from this JSON envelope" in system_prompt
        assert "What is my allocation?" in user_prompt
        return "Total value is $100.00 and equity is 60.0% with coverage 1.2x.", "fake"

    monkeypatch.setattr("ic_engine.runtime.narrator._call_llm", _fake_llm)

    result = narrate(envelope, "What is my allocation?")

    assert "$100.00" in result.answer
    assert envelope["ic_result"]["hmac"] in result.answer
    assert result.validation_passed is True


def test_narrator_rejects_fabricated_numbers(envelope):
    with pytest.raises(FabricationError):
        validate_narration("Total value is $999.00.", envelope)


def test_narrator_out_of_scope_refusal_gets_hmac_footer(envelope, monkeypatch):
    monkeypatch.setattr(
        "ic_engine.runtime.narrator._call_llm",
        lambda _system, _user: (OUT_OF_SCOPE_RESPONSE, "fake"),
    )

    result = narrate(envelope, "What is the price of oil?")

    assert result.answer.startswith(OUT_OF_SCOPE_RESPONSE)
    assert envelope["ic_result"]["hmac"] in result.answer


@pytest.mark.parametrize(
    "question,expected_mode",
    [
        # First-person performance — portfolio (regression for #69)
        ("How am I doing this year?", "portfolio"),
        ("How are we doing year-to-date?", "portfolio"),
        ("Am I up or down?", "portfolio"),
        ("Have I made money this year?", "portfolio"),
        ("Where do I stand right now?", "portfolio"),
        # Setup-style help — setup (regression for #70)
        ("How do I install ic-engine?", "setup"),
        ("How do I install InvestorClaw on Linux?", "setup"),
        ("How do I set up my portfolio file?", "setup"),
        ("How do I configure the narrator?", "setup"),
        ("How do I use the dashboard?", "setup"),
        ("What can you do for me?", "setup"),
        ("List commands.", "setup"),
        # Strong-ownership beats concept-stem
        ("What is in my portfolio?", "portfolio"),
        ("What is my Sharpe ratio?", "portfolio"),
        ("What is my allocation?", "portfolio"),
        # Concept fallback (no ownership, no setup, no first-person-perf)
        ("What is asset allocation?", "concept"),
        ("What is the Sharpe ratio?", "concept"),
        ("Explain duration risk.", "concept"),
        ("How does dollar-cost averaging work?", "concept"),
        # Concept-stem still wins over loose ownership when no strong marker
        ("How do I optimize my taxes?", "concept"),
        # Market-wide
        ("How is the S&P doing?", "market"),
        ("Inflation is rising — should I be worried?", "market"),
        # NA-metric
        ("What is my hedge effectiveness?", "concept"),
        # Loose ownership without a stem
        ("Show me my positions.", "portfolio"),
    ],
)
def test_question_mode_routes_correctly(question, expected_mode):
    assert _question_mode(question) == expected_mode

