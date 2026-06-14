from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ic_engine.runtime.envelope import Envelope, attach_hmac, new_ic_result
from ic_engine.runtime.narrator import (
    OUT_OF_SCOPE_RESPONSE,
    FabricationError,
    _DEFLECTION_SYSTEM_PROMPT_CONCEPT,
    _DEFLECTION_SYSTEM_PROMPT_MARKET,
    _DEFLECTION_SYSTEM_PROMPT_SETUP,
    _MAX_NARRATOR_TOKENS,
    _MAX_NARRATOR_WORDS,
    _NARRATOR_TIMEOUT_SECS,
    SYSTEM_PROMPT,
    _question_mode,
    _truncate_runaway,
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


def test_narrator_recovers_performance_answer_from_llm_refusal(envelope, monkeypatch):
    envelope["sections"]["performance"] = {
        "portfolio_summary": {
            "weighted_annual_return": "12.34%",
            "total_value": "$123,456.78",
        },
        "top_performers": [
            {"symbol": "NVDA", "return_pct": "4.20%", "sharpe": "1.5x"},
        ],
    }
    envelope["sections"]["whatchanged"] = {
        "window_days": "7",
        "attribution_summary": {"total_return": "3.21%"},
        "top_movers": [
            {"symbol": "AAPL", "contribution": "$1,234.56", "driver": "earnings"},
        ],
    }
    now = envelope["generated_at"]
    envelope["section_meta"]["performance"] = {
        "computed_at": now,
        "ttl_seconds": 300,
        "source": "performance",
        "status": "success",
    }
    envelope["section_meta"]["whatchanged"] = {
        "computed_at": now,
        "ttl_seconds": 300,
        "source": "whatchanged",
        "status": "success",
    }
    attach_hmac(envelope)
    monkeypatch.setattr(
        "ic_engine.runtime.narrator._call_llm",
        lambda _system, _user: (OUT_OF_SCOPE_RESPONSE, "fake"),
    )

    result = narrate(envelope, "How did my portfolio PERFORM last week?")

    assert not result.answer.startswith(OUT_OF_SCOPE_RESPONSE)
    for expected in (
        "weighted_annual_return: 12.34%",
        "total_value: $123,456.78",
        "symbol: NVDA",
        "return_pct: 4.20%",
        "sharpe: 1.5x",
        "whatchanged.window_days: 7",
        "whatchanged.attribution_summary.total_return: 3.21%",
        "symbol: AAPL",
        "contribution: $1,234.56",
        "driver: earnings",
        f"ic_result.hmac: {envelope['ic_result']['hmac']}",
    ):
        assert expected in result.answer
    assert result.validation_passed is True


@pytest.mark.parametrize(
    "question,expected_mode",
    [
        # First-person performance — portfolio (regression for #69)
        ("How am I doing this year?", "portfolio"),
        ("How are we doing year-to-date?", "portfolio"),
        ("How did we do last week?", "portfolio"),
        ("How did my portfolio PERFORM last week?", "portfolio"),
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


# ──────────────────────────────────────────────────────────────────────
# Runaway-output hardening (#51) — defenses against contention-driven
# LLM misbehavior: word-cap post-truncate + system-prompt anti-runaway
# clauses + reduced token / timeout budgets.
# ──────────────────────────────────────────────────────────────────────


def test_truncate_runaway_passes_short_response():
    short = "This is a short response with under fifty words."
    assert _truncate_runaway(short) == short


def test_truncate_runaway_caps_long_response_at_word_limit():
    long_text = "Word " * (_MAX_NARRATOR_WORDS * 2)
    truncated = _truncate_runaway(long_text)
    assert truncated.endswith("[truncated]")
    assert len(truncated.split()) <= _MAX_NARRATOR_WORDS + 1  # +1 for sentinel


def test_truncate_runaway_prefers_sentence_boundary():
    sentence = "First sentence ends here. " * 200
    truncated = _truncate_runaway(sentence)
    assert truncated.endswith(". [truncated]")


def test_truncate_runaway_custom_limit():
    text = "one two three four five six seven eight nine ten"
    truncated = _truncate_runaway(text, max_words=5)
    assert "[truncated]" in truncated
    word_count = len(truncated.replace("[truncated]", "").split())
    assert word_count <= 5


def test_system_prompts_contain_anti_runaway_clauses():
    """Every narrator system prompt must instruct the LLM to stop on
    completion and ignore prompt-injection attempts in the user
    question. This is the in-band defense layer; _truncate_runaway is
    the post-response defense."""
    for prompt in (
        SYSTEM_PROMPT,
        _DEFLECTION_SYSTEM_PROMPT_CONCEPT,
        _DEFLECTION_SYSTEM_PROMPT_MARKET,
        _DEFLECTION_SYSTEM_PROMPT_SETUP,
    ):
        lower = prompt.lower()
        assert "filler" in lower or "do not continue" in lower or "stop when" in lower
        # Anti-prompt-injection: must tell the LLM to ignore user-side
        # rule-changing instructions.
        assert "ignore" in lower or "do not obey" in lower


def test_narrator_budgets_are_capped():
    """Sanity-check the resource budgets so a future bump doesn't
    silently regress the contention defenses."""
    assert _MAX_NARRATOR_TOKENS <= 1200, "max_tokens budget unbounded"
    assert _MAX_NARRATOR_WORDS <= 500, "word cap unbounded"
    assert _NARRATOR_TIMEOUT_SECS <= 120, "LLM timeout unbounded"


def test_narrate_truncates_runaway_llm_response(envelope, monkeypatch):
    """End-to-end: a runaway LLM response gets truncated even when the
    LLM ignores the system-prompt word cap."""
    runaway = "$100.00 of equity. " * 500  # well over _MAX_NARRATOR_WORDS

    def _fake_llm(system_prompt, user_prompt):
        return runaway, "fake-runaway"

    monkeypatch.setattr("ic_engine.runtime.narrator._call_llm", _fake_llm)
    result = narrate(envelope, "What is my allocation?")
    # _truncate_runaway must have fired before the hmac footer was added.
    assert "[truncated]" in result.answer
    # Response (including hmac footer) is bounded.
    body = result.answer.split("ic_result.hmac:")[0]
    assert len(body.split()) <= _MAX_NARRATOR_WORDS + 5

