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

"""Provider capability matrix + per-(capability, symbol-class) fallback order.

The matrix is the single source of truth for *what each provider can do*. The
router asks :func:`providers_for` for the ordered list of providers that can
serve a given (capability, symbol-class) and tries them in turn. Editing this
file — not the router — is how you onboard a provider, grant it an asset class,
or change fallback priority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from .enums import Capability, SymbolClass


@dataclass(frozen=True)
class ProviderSpec:
    """Immutable declaration of one provider's reach.

    ``method_map`` ties a capability to the concrete method name on the provider
    class, so the router can dispatch without knowing provider internals.
    """

    name: str
    capabilities: Set[Capability]
    symbol_classes: Set[SymbolClass]
    method_map: Dict[Capability, str] = field(default_factory=dict)

    def supports(self, capability: Capability, symbol_class: SymbolClass) -> bool:
        return (
            capability in self.capabilities
            and symbol_class in self.symbol_classes
            and capability in self.method_map
        )


# =============================================================================
# CAPABILITY MATRIX — edit here to add/grant/remove provider capabilities.
# =============================================================================
PROVIDER_MATRIX: Dict[str, ProviderSpec] = {
    # Primary override provider: equities + crypto + INDICES + forex. Massive
    # DOES serve indices via get_quotes(["I:SPX","I:DJI",...]) — verified — so it
    # is the index source of record too; yfinance is only the free fallback.
    "massive": ProviderSpec(
        name="massive",
        capabilities={Capability.QUOTES, Capability.HISTORY, Capability.ANALYST},
        symbol_classes={
            SymbolClass.STOCK,
            SymbolClass.CRYPTO,
            SymbolClass.FOREX,
            SymbolClass.INDEX,
        },
        method_map={
            Capability.QUOTES: "get_quotes",
            Capability.HISTORY: "get_history",
            Capability.ANALYST: "get_analyst_ratings",
        },
    ),
    # Free, no key. The index source of record (^GSPC/^VIX) + crypto + equities.
    "yfinance": ProviderSpec(
        name="yfinance",
        capabilities={
            Capability.QUOTES,
            Capability.HISTORY,
            Capability.NEWS,
            Capability.ANALYST,
        },
        symbol_classes={SymbolClass.STOCK, SymbolClass.INDEX, SymbolClass.CRYPTO},
        method_map={
            Capability.QUOTES: "get_quotes",
            Capability.HISTORY: "get_history",
            Capability.NEWS: "get_news",
            Capability.ANALYST: "get_analyst_ratings",
        },
    ),
    "alpha_vantage": ProviderSpec(
        name="alpha_vantage",
        capabilities={Capability.QUOTES, Capability.HISTORY},
        symbol_classes={SymbolClass.STOCK, SymbolClass.INDEX},
        method_map={
            Capability.QUOTES: "get_quotes",
            Capability.HISTORY: "get_history",
        },
    ),
    "finnhub": ProviderSpec(
        name="finnhub",
        capabilities={Capability.QUOTES, Capability.NEWS, Capability.ANALYST},
        symbol_classes={SymbolClass.STOCK},
        method_map={
            Capability.QUOTES: "get_quotes",
            Capability.NEWS: "get_news",
            Capability.ANALYST: "get_analyst_ratings",
        },
    ),
}

# Global provider preference, applied when no explicit override exists below.
_DEFAULT_ORDER: List[str] = ["massive", "finnhub", "alpha_vantage", "yfinance"]

# Explicit per-(capability, symbol-class) fallback order. Names that do not
# declare support in PROVIDER_MATRIX are filtered out, so this stays honest.
_FALLBACK_ORDER: Dict[Tuple[Capability, SymbolClass], List[str]] = {
    # Indices: massive FIRST (it is the override/premium provider and serves
    # I:SPX/I:DJI/I:NDX/I:VIX), yfinance only as the free fallback.
    (Capability.QUOTES, SymbolClass.INDEX): ["massive", "yfinance", "alpha_vantage"],
    (Capability.QUOTES, SymbolClass.CRYPTO): ["massive", "yfinance"],
    (Capability.QUOTES, SymbolClass.STOCK): [
        "massive",
        "finnhub",
        "alpha_vantage",
        "yfinance",
    ],
}


def providers_for(capability: Capability, symbol_class: SymbolClass) -> List[str]:
    """Ordered provider names that can serve ``(capability, symbol_class)``.

    An explicit fallback order wins; otherwise the global default order, in
    both cases filtered to providers that actually declare the support.
    """
    order = _FALLBACK_ORDER.get((capability, symbol_class), _DEFAULT_ORDER)
    return [
        name
        for name in order
        if name in PROVIDER_MATRIX
        and PROVIDER_MATRIX[name].supports(capability, symbol_class)
    ]
