# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 InvestorClaw Contributors
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ic_engine.runtime.envelope import Envelope, attach_hmac, new_ic_result
from ic_engine.runtime.narrator import (
    OUT_OF_SCOPE_RESPONSE,
    FabricationError,
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
        assert "Show me my allocation." in user_prompt
        return "Total value is $100.00 and equity is 60.0% with coverage 1.2x.", "fake"

    monkeypatch.setattr("ic_engine.runtime.narrator._call_llm", _fake_llm)

    result = narrate(envelope, "Show me my allocation.")

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

