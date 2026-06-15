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

"""Tests for the service-agnostic market-data abstraction."""

from ic_engine.market_data import (
    Capability,
    SymbolClass,
    classify,
    providers_for,
    to_canonical,
    to_native,
)
from ic_engine.market_data.router import resolve_quotes


# --- symbology -------------------------------------------------------------

def test_classify():
    assert classify("I:SPX") is SymbolClass.INDEX
    assert classify("X:BTCUSD") is SymbolClass.CRYPTO
    assert classify("EUR/USD") is SymbolClass.FOREX
    assert classify("AAPL") is SymbolClass.STOCK


def test_to_native_indices_yfinance():
    assert to_native("I:SPX", "yfinance") == "^GSPC"
    assert to_native("I:VIX", "yfinance") == "^VIX"
    assert to_native("I:DJI", "yfinance") == "^DJI"


def test_to_native_crypto_and_stock_yfinance():
    assert to_native("X:BTCUSD", "yfinance") == "BTC-USD"
    assert to_native("X:ETHUSD", "yfinance") == "ETH-USD"
    assert to_native("BRK.B", "yfinance") == "BRK-B"


def test_to_native_massive_is_identity():
    # Massive already speaks the canonical (polygon) dialect.
    assert to_native("I:SPX", "massive") == "I:SPX"
    assert to_native("X:BTCUSD", "massive") == "X:BTCUSD"
    assert to_native("AAPL", "massive") == "AAPL"


def test_to_canonical_roundtrip_yfinance():
    for canon in ("I:SPX", "I:VIX", "X:BTCUSD", "BRK.B"):
        assert to_canonical(to_native(canon, "yfinance"), "yfinance") == canon


# --- capability matrix -----------------------------------------------------

def test_indices_route_skips_massive():
    order = providers_for(Capability.QUOTES, SymbolClass.INDEX)
    assert order == ["yfinance", "alpha_vantage"]
    assert "massive" not in order  # no index entitlement


def test_stock_route_prefers_massive():
    order = providers_for(Capability.QUOTES, SymbolClass.STOCK)
    assert order[0] == "massive"
    assert "yfinance" in order


def test_crypto_route():
    order = providers_for(Capability.QUOTES, SymbolClass.CRYPTO)
    assert order == ["massive", "yfinance"]


# --- router ----------------------------------------------------------------

class _FakeMassive:
    """Serves stocks/crypto in canonical dialect; no indices."""

    def get_quotes(self, syms):
        out = {}
        for s in syms:
            if not s.startswith("I:"):
                out[s] = {"price": 100.0, "change_pct": 1.0}
        return out


class _FakeYF:
    """Serves whatever native symbol it is handed (incl ^GSPC)."""

    def get_quotes(self, syms):
        return {s: {"price": 5000.0, "change_pct": 0.5} for s in syms}


def test_router_index_falls_through_to_yfinance():
    pool = {"massive": _FakeMassive(), "yfinance": _FakeYF()}
    out = resolve_quotes(["AAPL", "I:SPX", "X:BTCUSD"], pool)
    # canonical keys, never native
    assert set(out) == {"AAPL", "I:SPX", "X:BTCUSD"}
    assert out["AAPL"]["provider"] == "massive"
    assert out["I:SPX"]["provider"] == "yfinance"   # massive can't, yfinance can
    assert out["I:SPX"]["symbol"] == "I:SPX"        # mapped back from ^GSPC
    assert out["X:BTCUSD"]["provider"] == "massive"


def test_router_respects_availability_gate():
    pool = {"massive": _FakeMassive(), "yfinance": _FakeYF()}
    # massive gated off -> stocks must fall to yfinance
    out = resolve_quotes(["AAPL"], pool, is_available=lambda n: n == "yfinance")
    assert out["AAPL"]["provider"] == "yfinance"


def test_router_override_order_still_applies_symbology():
    # Operator forces yfinance; it must still receive native ^GSPC/BTC-USD and
    # the results map back to canonical (no I:/X: leak to the provider).
    seen = []  # router dispatches once per symbol-class group; accumulate all

    class _RecordingYF:
        def get_quotes(self, syms):
            seen.extend(syms)
            return {s: {"price": 1.0} for s in syms}

    pool = {"yfinance": _RecordingYF(), "massive": _FakeMassive()}
    out = resolve_quotes(
        ["I:SPX", "X:BTCUSD", "AAPL"], pool, provider_order=["yfinance"]
    )
    assert set(out) == {"I:SPX", "X:BTCUSD", "AAPL"}
    assert "^GSPC" in seen and "BTC-USD" in seen
    assert "I:SPX" not in seen and "X:BTCUSD" not in seen  # canonical never leaked
    assert all(r["provider"] == "yfinance" for r in out.values())


def test_router_override_skips_incapable_provider():
    # Forcing massive for an index yields nothing (no entitlement, no fallback).
    pool = {"massive": _FakeMassive(), "yfinance": _FakeYF()}
    out = resolve_quotes(["I:SPX"], pool, provider_order=["massive"])
    assert out == {}


def test_router_empty():
    assert resolve_quotes([], {}) == {}
