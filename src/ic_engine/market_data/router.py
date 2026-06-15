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

"""Capability- and symbol-class-aware routing for batch quotes.

:func:`resolve_quotes` groups canonical symbols by asset class, then for each
class walks the matrix's fallback order, translating symbols into each
provider's dialect and the results back to canonical. A symbol is dropped from
the work-set the moment any capable provider returns it, so indices that the
primary cannot serve are picked up by the next capable provider instead of
silently going missing (and tempting the LLM to fabricate).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Dict, List, Mapping, Optional

from .capability_matrix import PROVIDER_MATRIX, providers_for
from .enums import Capability, SymbolClass
from .symbology import classify, to_canonical, to_native

logger = logging.getLogger(__name__)


def _order_for(symbol_class: SymbolClass, provider_order: Optional[List[str]]) -> List[str]:
    """Capable provider names for a class — an operator override (if given) wins.

    Either way the list is filtered to providers that declare the capability in
    the matrix, so an override naming a provider that can't serve the class (or
    isn't matrix-known) is honored where valid and skipped where not.
    """
    if provider_order is None:
        return providers_for(Capability.QUOTES, symbol_class)
    return [
        n
        for n in provider_order
        if n in PROVIDER_MATRIX
        and PROVIDER_MATRIX[n].supports(Capability.QUOTES, symbol_class)
    ]


def resolve_quotes(
    symbols: List[str],
    pool: Mapping[str, object],
    *,
    is_available: Optional[Callable[[str], bool]] = None,
    on_dispatch: Optional[Callable[[str, int], None]] = None,
    provider_order: Optional[List[str]] = None,
) -> Dict[str, Dict]:
    """Resolve canonical ``symbols`` to quote rows via the capability matrix.

    ``pool`` maps provider name -> instance exposing the method named in the
    matrix' ``method_map`` (here ``get_quotes(native_symbols) -> {native: row}``).
    ``is_available`` gates a provider (quota/health); defaults to "in pool".
    ``on_dispatch(name, n)`` fires before each provider batch (quota charging).
    ``provider_order`` forces the provider preference for ALL classes (operator
    override); when ``None`` the matrix' per-class fallback order is used. Either
    way symbols are translated canonical<->native per provider, so a forced
    yfinance still gets ``^GSPC``/``BTC-USD`` rather than canonical ``I:``/``X:``.
    Returns ``{canonical_symbol: row}`` with ``symbol`` rewritten to canonical.
    """
    if not symbols:
        return {}
    avail = is_available or (lambda name: name in pool)

    groups: Dict[SymbolClass, List[str]] = defaultdict(list)
    for s in symbols:
        if s and str(s).strip():
            groups[classify(s)].append(s)

    results: Dict[str, Dict] = {}
    for cls, syms in groups.items():
        pending = list(dict.fromkeys(syms))  # de-dup, preserve order
        for name in _order_for(cls, provider_order):
            if not pending:
                break
            provider = pool.get(name)
            if provider is None or not avail(name):
                continue
            method_name = PROVIDER_MATRIX[name].method_map[Capability.QUOTES]
            fn = getattr(provider, method_name, None)
            if fn is None:
                continue

            # canonical -> native for this batch, and the reverse to map back.
            native_of = {s: to_native(s, name) for s in pending}
            reverse = {nat: canon for canon, nat in native_of.items()}
            if on_dispatch:
                on_dispatch(name, len(pending))
            try:
                batch = fn(list(native_of.values())) or {}
            except NotImplementedError:
                continue
            except Exception as exc:  # provider hiccup must not abort the class
                logger.warning("router: %s.%s failed for %s: %s", name, method_name, cls.value, exc)
                continue

            for nat, row in batch.items():
                canon = reverse.get(nat) or to_canonical(nat, name)
                if canon in results or not isinstance(row, dict):
                    continue
                merged = dict(row)
                merged["symbol"] = canon
                merged.setdefault("provider", name)
                results[canon] = merged
            pending = [s for s in pending if s not in results]

    return results
