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

"""Capabilities and asset classes used to route market-data requests."""

from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    """A unit of market-data work a provider may offer.

    ``str`` mixin so the value is JSON/log friendly and stable across runs.
    """

    QUOTES = "quotes"
    HISTORY = "history"
    NEWS = "news"
    ANALYST = "analyst"
    OPTIONS = "options"


class SymbolClass(str, Enum):
    """Asset class of a ticker — drives both routing and symbology."""

    STOCK = "stock"
    INDEX = "index"
    CRYPTO = "crypto"
    FOREX = "forex"
    FUTURES = "futures"
