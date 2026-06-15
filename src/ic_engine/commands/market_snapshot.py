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

"""Real-time market snapshot — current price + day change for holdings + benchmarks.

Provider-AGNOSTIC: sources quotes through ``PriceProvider.get_quotes`` (Massive
batch snapshot → AlphaVantage/Finnhub/yfinance fallbacks), so the engine — not
the agent — owns the live-data integration. The agent calls this one MCP tool
instead of shelling out to a specific vendor API.

Idempotent within a short window: results are cached for ``SNAPSHOT_TTL_SECS`` so
a 15-minute intraday scan loop (or several agents) does not re-poll providers on
every tick. Output is a signed v2.5 ic_result envelope.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ic_engine.runtime.envelope import (
    attach_hmac,
    new_ic_result,
    portfolio_id_for_holdings,
    utc_now_iso,
)
from ic_engine.services.portfolio_utils import load_holdings_list

import logging

logger = logging.getLogger(__name__)

SNAPSHOT_TTL_SECS = 30

# Whole-market benchmark tickers (Massive/polygon symbology). Best-effort: any
# that a provider can't resolve are simply omitted from the response.
_DEFAULT_BENCHMARKS = (
    "I:SPX",     # S&P 500 index
    "I:NDX",     # Nasdaq 100 index
    "I:DJI",     # Dow Jones Industrial Average
    "I:VIX",     # CBOE Volatility Index
    "X:BTCUSD",  # Bitcoin
    "X:ETHUSD",  # Ethereum
)

# Process-local TTL cache: {key: (expires_at, envelope)}
_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _now() -> float:
    return time.monotonic()


def _as_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _equity_symbols(holdings: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for h in holdings:
        if str(h.get("asset_type") or h.get("asset_class") or "").lower() != "equity":
            continue
        sym = str(h.get("symbol", "")).upper().strip()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _quote_row(symbol: str, q: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "price": _as_float(q.get("price")),
        "change_pct": _as_float(q.get("change_pct")),
        "prev_close": _as_float(q.get("prev_close")),
        "open": _as_float(q.get("open")),
        "high": _as_float(q.get("high")),
        "low": _as_float(q.get("low")),
        "volume": _as_float(q.get("volume")),
        "provider": q.get("provider"),
    }


def build_market_snapshot(
    holdings_file: str | Path | None = None,
    *,
    symbols: list[str] | None = None,
    benchmarks: bool = True,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return a signed real-time market snapshot envelope.

    ``symbols`` overrides the holdings list; otherwise equity holdings from
    ``holdings_file`` are used. ``benchmarks`` adds the standard index/crypto set.
    """
    requested: list[str] = []
    holdings_hash = "market-snapshot"
    if symbols:
        requested = [str(s).upper().strip() for s in symbols if str(s).strip()]
    elif holdings_file is not None:
        holdings_path = Path(holdings_file).expanduser()
        holdings = load_holdings_list(str(holdings_path))
        requested = _equity_symbols(holdings)
        try:
            holdings_hash = portfolio_id_for_holdings(holdings_path)
        except Exception:
            holdings_hash = "market-snapshot"

    bench = list(_DEFAULT_BENCHMARKS) if benchmarks else []
    all_symbols = list(dict.fromkeys([*requested, *bench]))
    if not all_symbols:
        raise ValueError("No symbols to snapshot (no holdings and no explicit symbols)")

    cache_key = f"{holdings_hash}|{','.join(sorted(all_symbols))}|{benchmarks}"
    if use_cache:
        hit = _SNAPSHOT_CACHE.get(cache_key)
        if hit and hit[0] > _now():
            return hit[1]

    # Provider-agnostic quote fetch (Massive → fallbacks), owned by the engine.
    from ic_engine.providers.price_provider import PriceProvider

    quotes: dict[str, dict[str, Any]] = {}
    try:
        quotes = PriceProvider().get_quotes(all_symbols) or {}
    except Exception as exc:
        logger.warning("market-snapshot: get_quotes failed: %s", exc)
        quotes = {}

    holding_rows = [_quote_row(s, quotes[s]) for s in requested if s in quotes]
    benchmark_rows = [_quote_row(s, quotes[s]) for s in bench if s in quotes]
    missing = [s for s in all_symbols if s not in quotes]

    movers = sorted(
        [r for r in holding_rows if r.get("change_pct") is not None],
        key=lambda r: abs(float(r["change_pct"])),
        reverse=True,
    )[:10]

    generated_at = utc_now_iso()
    section = {
        "holdings": holding_rows,
        "benchmarks": benchmark_rows,
        "top_movers": movers,
        "missing_symbols": missing,
        "as_of": generated_at,
        "calculation": "Real-time per-symbol quotes (price, day change%) via the "
        "provider-agnostic PriceProvider quote chain; not a deterministic window.",
        "disclaimer": "Live/last market data is provider-sourced and may be delayed "
        "depending on plan/venue; educational, not advice.",
    }
    envelope = {
        "schema_version": "v2.5.0",
        "generated_at": generated_at,
        "portfolio_id": holdings_hash,
        "ic_result": new_ic_result(command="market-snapshot", run_id=str(uuid.uuid4())),
        "sections": {"market_snapshot": section},
        "section_meta": {
            "market_snapshot": {
                "computed_at": generated_at,
                "ttl_seconds": SNAPSHOT_TTL_SECS,
                "source": "market_snapshot",
                "status": "success" if (holding_rows or benchmark_rows) else "empty",
            }
        },
        "failed_sections": [],
    }
    signed = attach_hmac(envelope)
    if use_cache:
        _SNAPSHOT_CACHE[cache_key] = (_now() + SNAPSHOT_TTL_SECS, signed)
    return signed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real-time market snapshot (holdings + benchmarks)")
    parser.add_argument("holdings_file", nargs="?", default=None)
    parser.add_argument("output_file", nargs="?", help="Optional JSON output path")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols (overrides holdings)")
    parser.add_argument("--no-benchmarks", action="store_true", help="Skip index/crypto benchmarks")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the TTL cache")
    parser.add_argument(
        "--verbose", action="store_true", help="Accepted for router compatibility; no-op"
    )
    args = parser.parse_args(argv)
    started = time.perf_counter()
    try:
        syms = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
        envelope = build_market_snapshot(
            args.holdings_file,
            symbols=syms,
            benchmarks=not args.no_benchmarks,
            use_cache=not args.no_cache,
        )
        if args.output_file:
            out = Path(args.output_file).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        print(json.dumps(envelope, separators=(",", ":")))
        return 0
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        print(
            json.dumps(
                {
                    "ic_result": {
                        "script": "market_snapshot.py",
                        "exit_code": 1,
                        "duration_ms": duration_ms,
                    },
                    "error": str(exc),
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
