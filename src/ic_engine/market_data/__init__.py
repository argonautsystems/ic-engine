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

"""Service-agnostic market-data abstraction.

Three small, independently-testable pieces sit between the engine and the
concrete data-provider classes so a request is routed to a provider that can
actually serve it, using a canonical symbology that every provider translates:

- ``enums``           — :class:`Capability` and :class:`SymbolClass`.
- ``symbology``       — classify a symbol and translate canonical <-> native.
- ``capability_matrix`` — which provider serves which (capability, symbol-class),
  in what fallback order, via which method.

The matrix and symbology maps are plain data — add a provider or an index by
editing a dict, not by touching routing logic.
"""

from .enums import Capability, SymbolClass
from .capability_matrix import (
    PROVIDER_MATRIX,
    ProviderSpec,
    providers_for,
)
from .symbology import (
    classify,
    to_canonical,
    to_native,
)

__all__ = [
    "Capability",
    "SymbolClass",
    "ProviderSpec",
    "PROVIDER_MATRIX",
    "providers_for",
    "classify",
    "to_native",
    "to_canonical",
]
