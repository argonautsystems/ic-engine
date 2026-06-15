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

"""Canonical symbology — one internal name per instrument, translated per provider.

The engine speaks a single canonical dialect (polygon-style): ``I:SPX`` for
indices, ``X:BTCUSD`` for crypto, ``BASE/QUOTE`` for forex, plain tickers for
equities. Each provider gets the symbol in *its* dialect via :func:`to_native`
and results are mapped back with :func:`to_canonical`, so a request never gets
stuck on whichever vendor happens to be primary.

Maps are data: add an index or a provider by editing a dict here, not by
touching the router.
"""

from __future__ import annotations

from typing import Dict

from .enums import SymbolClass

# --- classification --------------------------------------------------------

_INDEX_PREFIX = "I:"
_CRYPTO_PREFIX = "X:"


def classify(symbol: str) -> SymbolClass:
    """Infer the :class:`SymbolClass` of a canonical symbol from its shape."""
    s = (symbol or "").strip().upper()
    if s.startswith(_INDEX_PREFIX):
        return SymbolClass.INDEX
    if s.startswith(_CRYPTO_PREFIX):
        return SymbolClass.CRYPTO
    if "/" in s:
        return SymbolClass.FOREX
    return SymbolClass.STOCK


# --- explicit per-provider translations ------------------------------------
#
# canonical -> native, keyed by provider NAME. Only entries that differ from a
# provider's default rule (below) need listing. Indices in particular have no
# algorithmic mapping, so they are enumerated.

_YF_INDEX: Dict[str, str] = {
    "I:SPX": "^GSPC",   # S&P 500
    "I:NDX": "^NDX",    # Nasdaq 100
    "I:IXIC": "^IXIC",  # Nasdaq Composite
    "I:DJI": "^DJI",    # Dow Jones Industrial Average
    "I:VIX": "^VIX",    # CBOE Volatility Index
    "I:RUT": "^RUT",    # Russell 2000
}

# Explicit canonical -> native, per provider. Defaults handle the rest.
_EXPLICIT: Dict[str, Dict[str, str]] = {
    "yfinance": dict(_YF_INDEX),
    # alpha_vantage exposes a few indices as plain tickers via its quote API.
    "alpha_vantage": {
        "I:SPX": "SPX",
        "I:DJI": "DJI",
        "I:VIX": "VIX",
    },
}

# Reverse maps (native -> canonical) for mapping results back.
_REVERSE: Dict[str, Dict[str, str]] = {
    prov: {native: canon for canon, native in m.items()}
    for prov, m in _EXPLICIT.items()
}


def _yf_default(symbol: str, cls: SymbolClass) -> str:
    s = symbol.strip()
    if cls is SymbolClass.CRYPTO:
        # X:BTCUSD -> BTC-USD
        body = s[len(_CRYPTO_PREFIX):].upper()
        if body.endswith("USD") and len(body) > 3:
            return f"{body[:-3]}-USD"
        return body
    if cls is SymbolClass.INDEX:
        # Unknown index: best-effort ^TICKER (explicit map covers the majors).
        return "^" + s[len(_INDEX_PREFIX):].upper()
    if cls is SymbolClass.STOCK:
        return s.replace(".", "-")  # BRK.B -> BRK-B
    return s


def to_native(symbol: str, provider: str) -> str:
    """Translate a canonical symbol into ``provider``'s native dialect."""
    s = (symbol or "").strip()
    explicit = _EXPLICIT.get(provider, {})
    if s.upper() in explicit:
        return explicit[s.upper()]
    cls = classify(s)
    if provider == "yfinance":
        return _yf_default(s, cls)
    # massive and the polygon-dialect providers already use the canonical form;
    # other equity-only providers take plain tickers unchanged.
    return s


def to_canonical(native: str, provider: str) -> str:
    """Best-effort inverse of :func:`to_native` for a provider's native symbol.

    The router prefers the exact reverse map it builds per batch; this is the
    fallback when only the native symbol is known.
    """
    n = (native or "").strip()
    rev = _REVERSE.get(provider, {})
    if n in rev:
        return rev[n]
    if provider == "yfinance":
        if n.startswith("^"):
            return _INDEX_PREFIX + n[1:]
        if n.upper().endswith("-USD"):
            return _CRYPTO_PREFIX + n.upper().replace("-USD", "USD")
        if "-" in n:
            return n.replace("-", ".")  # BRK-B -> BRK.B
    return n
