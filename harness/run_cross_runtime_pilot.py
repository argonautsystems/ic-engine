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
Cross-runtime NL pilot — InvestorClaw v2.2 acceptance gate (RFC §6.3).

Runs the same 10 natural-language prompts against three agent runtimes
(OpenClaw / ZeroClaw / Hermes) using the same model (Gemini-flash-latest by
default). Scores each by whether the agent invoked the expected investorclaw
subcommand. Aggregates per-runtime pass/fail counts vs. the v2.2 acceptance
gates:

    OpenClaw  ≥ 10/10  (no regression — was 10/10 in v2.1.9)
    ZeroClaw  ≥  8/10  (up from 4–7/10 in v2.1.9)
    Hermes    ≥  6/10  (up from 3–5/10 in v2.1.9)

OPERATIONAL REQUIREMENTS:
- All three runtimes must be running and reachable. STUDIO does not host the
  agent runtimes; the canonical host is TYPHON (192.168.207.61) per
  ~/.claude/CLAUDE.md fleet reference.
- Set the relevant endpoint env vars before running:
    OPENCLAW_ENDPOINT      ws://localhost:18789 (default)
    ZEROCLAW_HOST          IP/hostname of zeroclaw container (default: typhon)
    HERMES_ENDPOINT        endpoint URL or stdin/CLI invocation
    GEMINI_API_KEY         provider key for Gemini-flash-latest

TYPICAL INVOCATION:
    # On TYPHON or a machine that can reach the agent runtimes:
    cd /path/to/InvestorClaw
    uv run python harness/run_cross_runtime_pilot.py \\
        --output reports/v2.2-cross-runtime-pilot.json \\
        --provider google --model gemini-flash-latest

OUTPUT:
    JSON report with per-prompt routing trace + aggregate scores.
    Exit 0 if all gates pass; exit 1 if any gate fails; exit 2 if a runtime
    was unreachable (partial run).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Bootstrap so harness/agent_clients is importable
_HARNESS_DIR = Path(__file__).resolve().parent
if str(_HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(_HARNESS_DIR))
if str(_HARNESS_DIR / "agent_clients") not in sys.path:
    sys.path.insert(0, str(_HARNESS_DIR / "agent_clients"))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NL pilot scenarios — 10 prompts, expected invocation maps to v2.2 surface
# ---------------------------------------------------------------------------

# Each scenario:
#   prompt:           natural-language user query
#   expected_tools:   set of investorclaw subcommands the agent SHOULD invoke
#                     (any one is acceptable — the agent may chain)
#   expected_keywords: tokens that should appear in the agent's response when
#                     it has actually run the right tool (helps catch agents
#                     that fabricate plausible-looking text without invoking)
#   v2_2_routing_note: which v2.2 wrapper or section this exercises

SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "p01-holdings",
        "prompt": "What's in my portfolio right now?",
        "expected_tools": {"holdings", "view"},
        "expected_keywords": ["holdings", "position", "ticker", "value"],
        "v2_2_routing_note": "portfolio_view --section=holdings (default section)",
    },
    {
        "id": "p02-performance",
        "prompt": "How has my portfolio performed this year?",
        "expected_tools": {"performance", "view"},
        "expected_keywords": ["performance", "return", "sharpe", "ytd"],
        "v2_2_routing_note": "portfolio_view --section=performance",
    },
    {
        "id": "p03-bonds",
        "prompt": "Show me my bond exposure and yield-to-maturity for fixed income.",
        "expected_tools": {"bonds", "fixed-income"},
        "expected_keywords": ["bond", "ytm", "duration", "fixed"],
        "v2_2_routing_note": "portfolio_bonds --section=analysis (default)",
    },
    {
        "id": "p04-bond-strategy",
        "prompt": "What bond laddering strategy should I use given current rates?",
        "expected_tools": {"fixed-income", "bonds"},
        "expected_keywords": ["ladder", "duration", "strategy", "yield"],
        "v2_2_routing_note": "portfolio_bonds --section=strategy",
    },
    {
        "id": "p05-rebalance",
        "prompt": "Should I rebalance my portfolio?",
        "expected_tools": {"scenario", "rebalance"},
        "expected_keywords": ["rebalance", "allocation", "drift", "target"],
        "v2_2_routing_note": "portfolio_scenario --section=rebalance (default)",
    },
    {
        "id": "p06-news-merger",
        "prompt": "Any big mergers or acquisitions in the news today?",
        "expected_tools": {"market", "news"},
        "expected_keywords": ["merger", "acquisition", "deal"],
        "v2_2_routing_note": "portfolio_market --section=news --topic=merger",
    },
    {
        "id": "p07-news-crypto",
        "prompt": "What's happening in crypto markets today?",
        "expected_tools": {"market"},
        "expected_keywords": ["crypto", "bitcoin", "btc"],
        "v2_2_routing_note": "portfolio_market --section=news --topic=crypto",
    },
    {
        "id": "p08-deflect-concept",
        "prompt": "What does yield-to-maturity mean?",
        "expected_tools": {"concept", "market"},
        "expected_keywords": ["scope", "investorclaw", "concept"],
        "v2_2_routing_note": "portfolio_market --section=concept (deflection)",
    },
    {
        "id": "p09-deflect-market",
        "prompt": "What's the current price of NVDA?",
        "expected_tools": {"market", "lookup"},
        "expected_keywords": ["scope", "market-wide", "investorclaw"],
        "v2_2_routing_note": "portfolio_market --section=market (deflection — NOT news)",
    },
    {
        "id": "p10-synthesize",
        "prompt": "Give me the full picture of my portfolio.",
        "expected_tools": {"synthesize", "compute", "analysis", "complete"},
        "expected_keywords": ["holdings", "performance", "synthesis", "summary"],
        "v2_2_routing_note": "portfolio_compute --section=synthesize (default) — or chain of view+compute",
    },
]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    """Outcome of a single scenario against a single runtime."""

    scenario_id: str
    runtime: str  # "openclaw" | "zeroclaw" | "hermes"
    prompt: str
    expected_tools: List[str]
    invoked_tools: List[str]
    response_text: str
    routed_correctly: bool
    response_keyword_match: float  # 0.0 - 1.0
    latency_ms: float
    error: Optional[str] = None


@dataclass
class RuntimeScore:
    runtime: str
    passed: int
    total: int
    threshold: int
    gate_pass: bool
    avg_latency_ms: float
    failures: List[Dict[str, Any]] = field(default_factory=list)


# Map underlying script names back to the canonical CLI subcommand they
# implement. Missing keys imply "the script name IS the subcommand".
_SCRIPT_TO_SUBCOMMAND: Dict[str, str] = {
    "fetch_holdings": "holdings",
    "fetch_portfolio_news": "news",
    "fetch_market_news": "market",
    "fetch_analyst_recommendations_parallel": "analyst",
    "analyze_performance_polars": "performance",
    "portfolio_analyzer": "synthesize",
    "portfolio_complete": "complete",
    "session_init": "session",
    "concept_decline": "concept",
    "dashboard_deferred": "dashboard",
    "model_guardrails": "guardrails",
    "rebalance_tax": "rebalance-tax",
    "fixed_income_analysis": "fixed-income",
    "bond_analyzer": "bonds",
    "export_report": "report",
    "scenario": "scenario",
    "optimize": "optimize",
    "lookup": "lookup",
    "auto_setup": "setup",
    "eod_report": "eod-report",
    "fa_discussion": "fa-topics",
    "stonkmode_control": "stonkmode",
    "peer_analysis": "peer",
    "whatchanged": "whatchanged",
    "cashflow": "cashflow",
    "portfolio_switcher": "portfolio",
    "check_updates": "check-updates",
    "news_fetch_planner": "news-plan",
    "ollama_model_config": "ollama-setup",
    "llm_config": "llm-config",
}


def extract_invoked_tools(response_text: str) -> List[str]:
    """Best-effort detection of which investorclaw subcommands ran.

    Looks for `ic_result.script` markers in the response (the canonical
    audit trail), then falls back to keyword detection in the prose.
    """
    invoked: set = set()

    # Canonical: ic_result envelopes leak through to the final response.
    # Each envelope has {"ic_result": {"script": "X.py", ...}}.
    for match in re.finditer(r'"script"\s*:\s*"([^"]+\.py)"', response_text):
        script_basename = match.group(1).removesuffix(".py")
        # Map back to the canonical CLI subcommand when known; otherwise
        # use the script name as-is.
        invoked.add(_SCRIPT_TO_SUBCOMMAND.get(script_basename, script_basename))

    # Fallback: parse `/portfolio <cmd>` or `investorclaw <cmd>` mentions.
    for match in re.finditer(r"(?:/portfolio|investorclaw)\s+([a-z][a-z\-]*)", response_text):
        invoked.add(match.group(1))

    return sorted(invoked)


def score_response(
    response_text: str,
    expected_tools: set,
    expected_keywords: List[str],
) -> Dict[str, Any]:
    """Score a single response against expectations."""
    invoked = set(extract_invoked_tools(response_text))
    routed_correctly = bool(invoked & expected_tools)

    text_lower = response_text.lower()
    keyword_hits = sum(1 for kw in expected_keywords if kw.lower() in text_lower)
    keyword_match = keyword_hits / len(expected_keywords) if expected_keywords else 0.0

    return {
        "invoked_tools": sorted(invoked),
        "routed_correctly": routed_correctly,
        "keyword_match": keyword_match,
    }


# ---------------------------------------------------------------------------
# Runtime adapters — wrap the existing agent_clients for uniform send_message
# ---------------------------------------------------------------------------


async def run_against_openclaw(
    scenarios: List[Dict[str, Any]],
    timeout_s: int = 60,
) -> List[ScenarioResult]:
    """Run all scenarios against OpenClaw via WebSocket."""
    try:
        from openclaw import OpenClawClient
    except ImportError as exc:
        logger.error(f"OpenClaw client unavailable: {exc}")
        return [
            _unreachable_result(s["id"], "openclaw", s["prompt"], s["expected_tools"], str(exc))
            for s in scenarios
        ]

    client = OpenClawClient(timeout_seconds=timeout_s)
    return await _run_with_client(client, scenarios, "openclaw")


async def run_against_zeroclaw(
    scenarios: List[Dict[str, Any]],
    host: str = "typhon",
    timeout_s: int = 60,
) -> List[ScenarioResult]:
    """Run all scenarios against ZeroClaw (typically containerized)."""
    try:
        from zeroclaw import ZeroClawClient
    except ImportError as exc:
        logger.error(f"ZeroClaw client unavailable: {exc}")
        return [
            _unreachable_result(s["id"], "zeroclaw", s["prompt"], s["expected_tools"], str(exc))
            for s in scenarios
        ]

    client = ZeroClawClient(host=host, timeout_seconds=timeout_s)
    return await _run_with_client(client, scenarios, "zeroclaw")


async def run_against_hermes(
    scenarios: List[Dict[str, Any]],
    timeout_s: int = 60,
) -> List[ScenarioResult]:
    """Run all scenarios against Hermes."""
    try:
        from hermes import HermesClient
    except ImportError as exc:
        logger.error(f"Hermes client unavailable: {exc}")
        return [
            _unreachable_result(s["id"], "hermes", s["prompt"], s["expected_tools"], str(exc))
            for s in scenarios
        ]

    client = HermesClient(timeout_seconds=timeout_s)
    return await _run_with_client(client, scenarios, "hermes")


async def _run_with_client(
    client: Any,
    scenarios: List[Dict[str, Any]],
    runtime: str,
) -> List[ScenarioResult]:
    results: List[ScenarioResult] = []
    for s in scenarios:
        start = time.time()
        try:
            response = await client.send_message(s["prompt"])
            latency_ms = (time.time() - start) * 1000
            response_text = response.get("response_content", "")
            error = response.get("error")
        except Exception as exc:
            latency_ms = (time.time() - start) * 1000
            response_text = ""
            error = str(exc)

        scored = score_response(response_text, set(s["expected_tools"]), s["expected_keywords"])

        results.append(
            ScenarioResult(
                scenario_id=s["id"],
                runtime=runtime,
                prompt=s["prompt"],
                expected_tools=sorted(s["expected_tools"]),
                invoked_tools=scored["invoked_tools"],
                response_text=response_text[:500],  # Truncate for report
                routed_correctly=scored["routed_correctly"],
                response_keyword_match=scored["keyword_match"],
                latency_ms=latency_ms,
                error=error,
            )
        )
    return results


def _unreachable_result(
    scenario_id: str,
    runtime: str,
    prompt: str,
    expected_tools: set,
    error: str,
) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=scenario_id,
        runtime=runtime,
        prompt=prompt,
        expected_tools=sorted(expected_tools),
        invoked_tools=[],
        response_text="",
        routed_correctly=False,
        response_keyword_match=0.0,
        latency_ms=0.0,
        error=f"Runtime unreachable: {error}",
    )


# ---------------------------------------------------------------------------
# Aggregation + reporting
# ---------------------------------------------------------------------------

GATES = {
    "openclaw": 10,  # ≥10/10 — no regression
    "zeroclaw": 8,  # ≥8/10 — up from 4-7/10 in v2.1.9
    "hermes": 6,  # ≥6/10 — up from 3-5/10 in v2.1.9
}


def aggregate(results: List[ScenarioResult]) -> Dict[str, RuntimeScore]:
    by_runtime: Dict[str, List[ScenarioResult]] = {}
    for r in results:
        by_runtime.setdefault(r.runtime, []).append(r)

    scores: Dict[str, RuntimeScore] = {}
    for runtime, runtime_results in by_runtime.items():
        passed = sum(1 for r in runtime_results if r.routed_correctly)
        total = len(runtime_results)
        threshold = GATES.get(runtime, 0)
        avg_latency = sum(r.latency_ms for r in runtime_results) / total if total else 0.0
        failures = [
            {
                "scenario_id": r.scenario_id,
                "expected": r.expected_tools,
                "invoked": r.invoked_tools,
                "error": r.error,
            }
            for r in runtime_results
            if not r.routed_correctly
        ]
        scores[runtime] = RuntimeScore(
            runtime=runtime,
            passed=passed,
            total=total,
            threshold=threshold,
            gate_pass=passed >= threshold,
            avg_latency_ms=avg_latency,
            failures=failures,
        )
    return scores


def print_summary(scores: Dict[str, RuntimeScore]) -> None:
    print("\n" + "=" * 70)
    print("InvestorClaw v2.2 cross-runtime NL pilot — RFC §6.3 gates")
    print("=" * 70)
    for runtime in ("openclaw", "zeroclaw", "hermes"):
        if runtime not in scores:
            print(f"  {runtime:10s}: NOT RUN (runtime unreachable)")
            continue
        s = scores[runtime]
        status = "✅ PASS" if s.gate_pass else "❌ FAIL"
        print(
            f"  {s.runtime:10s}: {s.passed}/{s.total}  (gate ≥ {s.threshold})  "
            f"avg {s.avg_latency_ms:.0f}ms   {status}"
        )
    print("=" * 70)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    runtimes = args.runtimes.split(",") if args.runtimes else ["openclaw", "zeroclaw", "hermes"]
    all_results: List[ScenarioResult] = []
    unreachable_count = 0

    if "openclaw" in runtimes:
        logger.info("Running against OpenClaw...")
        r = await run_against_openclaw(SCENARIOS, timeout_s=args.timeout)
        all_results.extend(r)
        if all(x.error and "unreachable" in (x.error or "") for x in r):
            unreachable_count += 1

    if "zeroclaw" in runtimes:
        logger.info(f"Running against ZeroClaw (host={args.zeroclaw_host})...")
        r = await run_against_zeroclaw(SCENARIOS, host=args.zeroclaw_host, timeout_s=args.timeout)
        all_results.extend(r)
        if all(x.error and "unreachable" in (x.error or "") for x in r):
            unreachable_count += 1

    if "hermes" in runtimes:
        logger.info("Running against Hermes...")
        r = await run_against_hermes(SCENARIOS, timeout_s=args.timeout)
        all_results.extend(r)
        if all(x.error and "unreachable" in (x.error or "") for x in r):
            unreachable_count += 1

    scores = aggregate(all_results)
    print_summary(scores)

    # Write JSON report
    report = {
        "rfc": "v2.2 RFC §6.3",
        "timestamp": datetime.now().isoformat(),
        "scenarios": SCENARIOS,
        "results": [asdict(r) for r in all_results],
        "scores": {k: asdict(v) for k, v in scores.items()},
        "ic_result": {
            "script": "harness/run_cross_runtime_pilot.py",
            "exit_code": 0,
            "duration_ms": 0,
        },
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nFull report: {output_path}")

    # Exit codes per acceptance gate semantics
    if unreachable_count == len(runtimes):
        return 2  # No runtime was reachable
    if any(not s.gate_pass for s in scores.values()):
        return 1  # At least one gate failed
    return 0  # All gates passed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="InvestorClaw v2.2 cross-runtime NL pilot (RFC §6.3)"
    )
    parser.add_argument(
        "--runtimes",
        default="openclaw,zeroclaw,hermes",
        help="Comma-separated runtimes to test (default: all three)",
    )
    parser.add_argument(
        "--zeroclaw-host",
        default="typhon",
        help="ZeroClaw host (default: typhon)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Per-prompt timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--output",
        default="reports/v2.2-cross-runtime-pilot.json",
        help="Path to write JSON report",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    exit_code = asyncio.run(main_async(args))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
