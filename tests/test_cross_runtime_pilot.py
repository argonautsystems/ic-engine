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
Smoke tests for harness/run_cross_runtime_pilot.py.

Verifies the scenario catalog + scoring helpers without needing the live
agent runtimes (which only run on TYPHON).
"""

from __future__ import annotations

import sys
from pathlib import Path

_skill_root = Path(__file__).parent.parent
if str(_skill_root) not in sys.path:
    sys.path.insert(0, str(_skill_root))
if str(_skill_root / "harness") not in sys.path:
    sys.path.insert(0, str(_skill_root / "harness"))

from run_cross_runtime_pilot import (
    GATES,
    SCENARIOS,
    aggregate,
    extract_invoked_tools,
    score_response,
)
from run_cross_runtime_pilot import (
    ScenarioResult as _ScenarioResult,
)


def test_scenario_catalog_has_ten_entries():
    assert len(SCENARIOS) == 10


def test_scenario_ids_unique():
    ids = [s["id"] for s in SCENARIOS]
    assert len(set(ids)) == len(ids)


def test_every_scenario_has_required_keys():
    for s in SCENARIOS:
        assert "id" in s
        assert "prompt" in s
        assert "expected_tools" in s
        assert "expected_keywords" in s
        assert "v2_2_routing_note" in s
        assert isinstance(s["expected_tools"], set)
        assert isinstance(s["expected_keywords"], list)


def test_gates_reflect_rfc_thresholds():
    """RFC §6.3 acceptance gates."""
    assert GATES["openclaw"] == 10
    assert GATES["zeroclaw"] == 8
    assert GATES["hermes"] == 6


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def test_extract_invoked_tools_from_ic_result_envelope():
    response = (
        "Here are your holdings:\n"
        '{"ic_result": {"script": "fetch_holdings.py", "exit_code": 0, "duration_ms": 250}}\n'
        "Total value $X."
    )
    invoked = extract_invoked_tools(response)
    assert "holdings" in invoked


def test_extract_invoked_tools_from_slash_command_mention():
    response = "I would run `/portfolio holdings` to fetch your positions."
    invoked = extract_invoked_tools(response)
    assert "holdings" in invoked


def test_extract_invoked_tools_normalizes_analyze_to_performance():
    response = '{"ic_result": {"script": "analyze_performance_polars.py", "exit_code": 0}}'
    invoked = extract_invoked_tools(response)
    assert "performance" in invoked


def test_score_response_routed_correctly():
    response = '{"ic_result": {"script": "fetch_holdings.py", "exit_code": 0}}'
    score = score_response(
        response,
        expected_tools={"holdings", "view"},
        expected_keywords=["holdings", "position"],
    )
    assert score["routed_correctly"] is True


def test_score_response_routed_incorrectly():
    response = "I think your portfolio is doing great. Trust me."
    score = score_response(
        response,
        expected_tools={"holdings"},
        expected_keywords=["holdings", "position"],
    )
    assert score["routed_correctly"] is False
    # Keyword match also low (none of the keywords are in the response)
    assert score["keyword_match"] == 0.0


def test_aggregate_pass_threshold():
    """An OpenClaw run with 10/10 routed correctly should gate-pass."""
    results = [
        _ScenarioResult(
            scenario_id=f"p{i:02d}",
            runtime="openclaw",
            prompt="...",
            expected_tools=["holdings"],
            invoked_tools=["holdings"],
            response_text="",
            routed_correctly=True,
            response_keyword_match=1.0,
            latency_ms=1000.0,
        )
        for i in range(1, 11)
    ]
    scores = aggregate(results)
    assert scores["openclaw"].passed == 10
    assert scores["openclaw"].total == 10
    assert scores["openclaw"].gate_pass is True


def test_aggregate_below_threshold_fails():
    """ZeroClaw with 5/10 should fail the ≥8 gate."""
    results = [
        _ScenarioResult(
            scenario_id=f"p{i:02d}",
            runtime="zeroclaw",
            prompt="...",
            expected_tools=["holdings"],
            invoked_tools=["holdings"] if i <= 5 else [],
            response_text="",
            routed_correctly=i <= 5,
            response_keyword_match=0.5,
            latency_ms=2000.0,
        )
        for i in range(1, 11)
    ]
    scores = aggregate(results)
    assert scores["zeroclaw"].passed == 5
    assert scores["zeroclaw"].gate_pass is False
    assert len(scores["zeroclaw"].failures) == 5
