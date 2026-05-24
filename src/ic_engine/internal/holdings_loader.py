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
Unified Holdings Loader — Phase 1 Architectural Refactor
========================================================

Consolidates eight duplicate CDM-parsing implementations previously scattered
across the command suite into a single canonical loader.

Replaces:
  - commands/optimize.py            : load_holdings()
  - commands/scenario.py            : load_holdings()
  - commands/rebalance_tax.py       : load_holdings_with_lots()
  - commands/cashflow.py            : _load_portfolio()
  - commands/bond_analyzer.py       : load_bonds_from_holdings_json()
  - commands/fetch_portfolio_news.py: XNewsFetcher.load_holdings()
  - commands/portfolio_analyzer.py  : PortfolioAnalyzer.load_portfolio()
  - services/portfolio_utils.py     : load_holdings_list() / load_portfolio_json()

Public surface:
  PortfolioData   -- Pydantic-like dataclass exposing positions + aggregates.
  HoldingsLoader  -- Loads and normalizes CDM 5.x/6.x (and legacy) portfolio
                     JSON, emitting a PortfolioData value object.
  infer_asset_class(symbol) -- Heuristic classifier moved from optimize.py.

Design goals:
  * Support CDM 5.x and CDM 6.x envelopes (both camelCase and snake_case).
  * Tolerate legacy schemas (flat "holdings" lists, "portfolio.<asset_class>"
    dicts, disclaimer-wrapped "data" payloads).
  * Provide a single normalized output type with both dict- and DataFrame-
    oriented views so existing callers can pick the representation they need
    without reimplementing traversal.
  * Preserve lot-level detail so rebalance_tax.py stops owning its own loader.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Asset class inference (canonical home; was previously in optimize.py)
# ---------------------------------------------------------------------------


def infer_asset_class(symbol: str) -> str:
    """Heuristic: infer asset class from a symbol name.

    Returns one of: 'equity' | 'bond' | 'cash' | 'derivative'.

    Kept compatible with the previous implementation in commands/optimize.py
    so that downstream callers depending on the exact classification behaviour
    continue to see identical output.
    """
    if not symbol:
        return "equity"

    symbol_lower = symbol.lower()

    # Cash indicators
    if symbol in ("CASH", "USD", "CASH_USD"):
        return "cash"

    # Bond indicators
    if any(x in symbol for x in ["bond", "note", "tbond", "gnma", "cmbs"]):
        return "bond"
    if " " in symbol and any(
        x in symbol_lower
        for x in ["coupon", "maturity", "20", "21", "22", "23", "24", "25", "26", "27", "28", "29"]
    ):
        return "bond"

    # Derivative indicators
    if any(x in symbol_lower for x in ["call", "put", "option", "fut", "index"]):
        return "derivative"

    return "equity"


# ---------------------------------------------------------------------------
# Low-level parsing helpers (shared across all legacy loaders)
# ---------------------------------------------------------------------------


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Convert any value to float; return default if it fails or is NaN."""
    if value is None:
        return default
    try:
        f = float(value)
        if f != f:  # NaN check
            return default
        return f
    except (TypeError, ValueError):
        return default


def _amount_of(obj: Any) -> Optional[float]:
    """Extract `.amount` from a CDM money/quantity blob, or pass through scalar."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return _safe_float(obj.get("amount"))
    return _safe_float(obj)


def _first(d: Mapping[str, Any], *keys: str) -> Any:
    """Return the first present (non-None) key value in d."""
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return None


def _get_nested(d: Mapping[str, Any], *paths: str) -> Any:
    """Return the first matching nested value from dotted paths (shallow)."""
    for path in paths:
        cur: Any = d
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok:
            return cur
    return None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Position:
    """A single normalized portfolio position.

    Carries the union of fields previously extracted by the various
    command-specific loaders. Callers pick the subset they care about via
    attribute access or via PortfolioQuery filters.
    """

    symbol: str
    asset_class: str = "equity"  # equity | bond | cash | crypto | futures | metals
    security_type: Optional[str] = None  # CDM securityType as reported by the envelope
    sector: Optional[str] = None

    # Position sizing
    shares: Optional[float] = None  # Quantity / par value
    current_price: Optional[float] = None
    cost_basis_price: Optional[float] = None

    # Computed / reported
    market_value: Optional[float] = None
    cost_basis: Optional[float] = None
    unrealized_gain_loss: Optional[float] = None
    unrealized_gain_loss_pct: Optional[float] = None

    # Identifiers
    cusip: Optional[str] = None
    isin: Optional[str] = None

    # Bond analytics (optional, populated when present in envelope)
    modified_duration: Optional[float] = None
    macaulay_duration: Optional[float] = None
    convexity: Optional[float] = None
    coupon_rate: Optional[float] = None
    years_to_maturity: Optional[float] = None
    maturity_date: Optional[str] = None
    is_corporate: bool = False

    # Account / lot metadata
    account: Optional[str] = None
    lots: List[Dict[str, Any]] = field(default_factory=list)
    tradable: bool = True  # Set False for bonds/CUSIPs/non-ticker symbols

    # Pass-through raw dict for callers that want extra fields not explicitly
    # promoted to attributes. This preserves compatibility with legacy loaders
    # that exposed every field from the source envelope.
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a flat dict representation (legacy-loader-shaped).

        Emits normalized keys matching the shape returned by the old
        per-command loaders. The original CDM envelope fields are not
        re-emitted here; callers that need them should go through `.raw`.
        """
        out: Dict[str, Any] = {
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            # Legacy alias — some callers (portfolio_utils, bond_analyzer)
            # read `asset_type` instead of `asset_class`.
            "asset_type": self.asset_class,
            "sector": self.sector or "Unknown",
            "shares": self.shares,
            "current_price": self.current_price,
            "cost_basis_price": self.cost_basis_price,
            # Legacy alias — the Holding dataclass requires a
            # `purchase_price` field which we populate from cost basis.
            "purchase_price": self.cost_basis_price,
            "market_value": self.market_value,
            "cost_basis": self.cost_basis,
            "unrealized_gain_loss": self.unrealized_gain_loss,
            "unrealized_gain_loss_pct": self.unrealized_gain_loss_pct,
            "cusip": self.cusip,
            "isin": self.isin,
            "modified_duration": self.modified_duration,
            "macaulay_duration": self.macaulay_duration,
            "convexity": self.convexity,
            "coupon_rate": self.coupon_rate,
            "years_to_maturity": self.years_to_maturity,
            "maturity_date": self.maturity_date,
            "is_corporate": self.is_corporate,
            "account": self.account,
            "tradable": self.tradable,
        }
        # Drop None-valued keys for readability
        return {k: v for k, v in out.items() if v is not None}


@dataclass
class PortfolioData:
    """Unified output of the HoldingsLoader.

    Attributes:
        positions:    Normalized list of Position objects.
        total_value:  Sum of market values across all positions.
        as_of_date:   Snapshot date, if reported by the envelope.
        cdm_version:  CDM version string, if reported.
        source_path:  Filesystem path the data was loaded from, if any.
        raw:          The original parsed JSON, for callers that need
                      access to fields not yet promoted to attributes.
    """

    positions: List[Position] = field(default_factory=list)
    total_value: float = 0.0
    as_of_date: Optional[str] = None
    cdm_version: Optional[str] = None
    source_path: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    # ----- Convenience views (no heavy deps unless requested) ---------------

    def to_dicts(self) -> List[Dict[str, Any]]:
        """Flat list-of-dicts view — drop-in replacement for the
        `holdings: List[Dict]` shape returned by the old loaders."""
        return [p.to_dict() for p in self.positions]

    def to_dataframe(self):
        """Pandas DataFrame view — drop-in for optimize.py / rebalance_tax.py.

        Adds derived `value` and `weight` columns mirroring the old loaders.
        Imports pandas lazily so modules that don't need a DataFrame view
        don't pay the import cost.
        """
        import pandas as pd

        if not self.positions:
            return pd.DataFrame()
        df = pd.DataFrame(self.to_dicts())
        if "shares" in df.columns and "current_price" in df.columns:
            # Use existing market_value when available, else compute
            if "market_value" in df.columns:
                df["value"] = df["market_value"].fillna(df["shares"] * df["current_price"])
            else:
                df["value"] = df["shares"] * df["current_price"]
            total = float(df["value"].sum())
            df["weight"] = df["value"] / total if total > 0 else 0.0
        return df

    def by_symbol(self) -> Dict[str, Position]:
        """Index positions by symbol (first-wins for duplicates)."""
        out: Dict[str, Position] = {}
        for p in self.positions:
            out.setdefault(p.symbol, p)
        return out

    def filter_asset_class(self, asset_class: str) -> List[Position]:
        """Return positions matching a given asset class label."""
        return [p for p in self.positions if p.asset_class == asset_class]

    def symbols(self) -> List[str]:
        """Ordered list of symbols in the portfolio."""
        return [p.symbol for p in self.positions]

    # ----- Aggregate views for commands that need them ----------------------

    def lot_records(self) -> List[Dict[str, Any]]:
        """Flatten lots across positions (for rebalance_tax.py)."""
        out: List[Dict[str, Any]] = []
        for p in self.positions:
            for lot in p.lots:
                out.append(
                    {
                        "symbol": p.symbol,
                        "account": p.account or "default",
                        **lot,
                        "current_price": p.current_price,
                    }
                )
        return out


# ---------------------------------------------------------------------------
# HoldingsLoader — the consolidated entry point
# ---------------------------------------------------------------------------


class HoldingsLoader:
    """Load and normalize portfolio holdings from CDM JSON (or legacy formats).

    Typical usage::

        loader = HoldingsLoader()
        portfolio = loader.load("holdings.json")
        df, total = loader.as_dataframe(portfolio)   # convenience

    The loader does *not* mutate source data and is thread-safe (no instance
    state is written during `load()`).

    Supported envelopes:
      1. CDM 5.x / 6.x:
           {cdmVersion, portfolio: {portfolioState: {positions: [...]}}}
      2. Bare portfolioState:
           {portfolioState: {positions: [...]}}
      3. Legacy flat list:
           {holdings: [ {symbol, shares, current_price, asset_type, ...} ]}
      4. Legacy keyed portfolio:
           {portfolio: {equity: {AAPL: {...}}, bond: {CUSIP: {...}}, ...}}
      5. Disclaimer-wrapped:
           {data: <any of the above>}
    """

    # Envelope paths probed when extracting positions from CDM payloads.
    _POSITION_PATHS: Tuple[str, ...] = (
        "data.portfolio.portfolioState.positions",
        "data.portfolio.portfolio_state.positions",
        "data.portfolioState.positions",
        "data.portfolio_state.positions",
        "portfolio.portfolioState.positions",
        "portfolio.portfolio_state.positions",
        "portfolioState.positions",
        "portfolio_state.positions",
        "positions",
    )

    # ----- Public API -------------------------------------------------------

    def load(self, source: Union[str, Path, Mapping[str, Any]]) -> PortfolioData:
        """Load a portfolio from a filesystem path or already-parsed dict."""
        if isinstance(source, Mapping):
            raw = dict(source)
            source_path = None
        else:
            path = Path(source).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"Portfolio file not found: {path}")
            with open(path, "r") as f:
                raw = json.load(f)
            source_path = str(path)

        return self._parse(raw, source_path=source_path)

    def load_from_dict(self, data: Mapping[str, Any]) -> PortfolioData:
        """Load directly from an already-parsed dict (no filesystem access)."""
        return self._parse(dict(data), source_path=None)

    # ----- Convenience bridges to legacy loader return shapes ---------------

    def as_dataframe(self, portfolio: PortfolioData):
        """Return (df, total_value) — drop-in for optimize.py.load_holdings()."""
        df = portfolio.to_dataframe()
        return df, portfolio.total_value

    def as_holdings_list(self, portfolio: PortfolioData) -> Tuple[List[Dict[str, Any]], float]:
        """Return (list_of_dicts, total_value) — drop-in for scenario.py.load_holdings()."""
        return portfolio.to_dicts(), portfolio.total_value

    def as_lots(self, portfolio: PortfolioData):
        """Return (df, lot_records, total_value) — drop-in for
        rebalance_tax.py.load_holdings_with_lots()."""
        return portfolio.to_dataframe(), portfolio.lot_records(), portfolio.total_value

    # ----- Internal parsing -------------------------------------------------

    def _parse(self, data: Mapping[str, Any], source_path: Optional[str]) -> PortfolioData:
        """Dispatch to the appropriate schema parser based on envelope shape."""
        # Unwrap disclaimer envelope
        inner = data
        if isinstance(inner, dict) and "data" in inner and isinstance(inner["data"], dict):
            # Only unwrap if the inner object looks like a portfolio envelope —
            # not every top-level "data" key is a disclaimer wrapper.
            inner_data = inner["data"]
            if any(
                k in inner_data
                for k in (
                    "portfolio",
                    "portfolioState",
                    "portfolio_state",
                    "positions",
                    "holdings",
                    "cdmVersion",
                )
            ):
                inner = inner_data

        # Metadata
        cdm_version = None
        if isinstance(inner, dict):
            cdm_version = inner.get("cdmVersion") or inner.get("cdm_version")

        as_of_date = None
        if isinstance(inner, dict):
            as_of_date = _get_nested(
                inner,
                "portfolio.aggregationParameters.asOfDate",
                "portfolio.aggregation_parameters.as_of_date",
                "portfolio.portfolioState.timestamp",
                "portfolio.portfolio_state.timestamp",
            )

        positions: List[Position] = []

        # Path 1: CDM positions list (all envelope variants)
        positions_raw = self._extract_positions_list(inner)
        if positions_raw:
            for pos in positions_raw:
                parsed = self._parse_cdm_position(pos)
                if parsed is not None:
                    positions.append(parsed)

        # Path 2: legacy flat "holdings" list
        if not positions and isinstance(inner, dict) and "holdings" in inner:
            holdings = inner.get("holdings") or []
            if isinstance(holdings, list):
                for entry in holdings:
                    parsed = self._parse_flat_holding(entry)
                    if parsed is not None:
                        positions.append(parsed)

        # Path 3: legacy keyed portfolio (portfolio.equity.AAPL = {...})
        if (
            not positions
            and isinstance(inner, dict)
            and "portfolio" in inner
            and isinstance(inner["portfolio"], dict)
        ):
            portfolio_dict = inner["portfolio"]
            # Avoid mis-parsing CDM portfolios already handled above
            if not any(
                k in portfolio_dict
                for k in (
                    "portfolioState",
                    "portfolio_state",
                    "aggregationParameters",
                    "aggregation_parameters",
                )
            ):
                for asset_class_key, assets in portfolio_dict.items():
                    if not isinstance(assets, dict):
                        continue
                    if asset_class_key in ("summary",):
                        continue
                    for symbol, entry in assets.items():
                        if not isinstance(entry, dict):
                            continue
                        flat = {"symbol": symbol, "asset_type": asset_class_key, **entry}
                        parsed = self._parse_flat_holding(flat)
                        if parsed is not None:
                            positions.append(parsed)

        # Aggregate total value
        total_value = 0.0
        for p in positions:
            if p.market_value is not None:
                total_value += float(p.market_value)
            elif p.shares is not None and p.current_price is not None:
                total_value += float(p.shares) * float(p.current_price)

        if not positions:
            logger.warning(
                "HoldingsLoader: no positions extracted from envelope (schema=%s, keys=%s)",
                type(inner).__name__,
                list(inner.keys()) if isinstance(inner, dict) else None,
            )

        return PortfolioData(
            positions=positions,
            total_value=total_value,
            as_of_date=str(as_of_date) if as_of_date else None,
            cdm_version=cdm_version,
            source_path=source_path,
            raw=dict(data) if isinstance(data, dict) else {},
        )

    def _extract_positions_list(self, data: Any) -> List[Dict[str, Any]]:
        """Walk CDM envelopes to locate the positions list, if any."""
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for path in self._POSITION_PATHS:
            val = _get_nested(data, path)
            if isinstance(val, list) and val:
                return val
        return []

    def _parse_cdm_position(self, pos: Any) -> Optional[Position]:
        """Parse a single CDM 5.x/6.x position node into a Position."""
        if not isinstance(pos, dict):
            return None

        try:
            product = pos.get("product") or {}
            asset = pos.get("asset") or {}

            # Symbol — try product, then asset, then top-level fallbacks.
            pid = (
                _first(product, "productIdentifier", "product_identifier")
                or _first(asset, "productIdentifier", "product_identifier")
                or {}
            )
            if not isinstance(pid, dict):
                pid = {}
            symbol = (
                pid.get("identifier")
                or _first(asset, "securityName", "security_name")
                or _first(pos, "symbol", "ticker")
            )
            if not symbol:
                return None
            symbol = str(symbol)

            # Price quantity block (both cases)
            pq = _first(pos, "priceQuantity", "price_quantity") or {}
            if not isinstance(pq, dict):
                pq = {}

            qty_obj = pq.get("quantity") or pos.get("quantity")
            shares = _amount_of(qty_obj)

            current_price = _amount_of(
                _first(pq, "currentPrice", "current_price")
                or _first(pos, "currentPrice", "current_price")
                or pos.get("price")
            )
            cost_basis_price = _amount_of(
                _first(pq, "costBasisPrice", "cost_basis_price")
                or _first(pos, "costBasisPrice", "cost_basis_price")
            )

            # Market value — prefer reported, fallback to qty * price
            mv_reported = _first(pos, "marketValue", "market_value")
            market_value = _safe_float(mv_reported)
            if market_value is None and shares is not None and current_price is not None:
                market_value = float(shares) * float(current_price)

            # Identifiers
            cusip = asset.get("cusip") if isinstance(asset, dict) else None
            isin = asset.get("isin") if isinstance(asset, dict) else None

            # Classify asset
            sec_type_raw = ""
            asset_class_raw = ""
            if isinstance(asset, dict):
                sec_type_raw = asset.get("securityType") or asset.get("security_type") or ""
                asset_class_raw = asset.get("assetClass") or asset.get("asset_class") or ""
            top_type = pos.get("asset_type") or pos.get("assetType") or ""
            asset_class = self._classify_asset_class(
                sec_type_raw, asset_class_raw, top_type, symbol
            )

            sector = None
            if isinstance(asset, dict):
                sector = asset.get("sector")
            if sector is None:
                sector = pos.get("sector")

            # Tradable heuristic — used by rebalance_tax.py to exclude bonds/CUSIPs
            id_type_raw = ""
            if isinstance(pid, dict):
                id_type_raw = (
                    pid.get("identifierType") or pid.get("identifier_type") or ""
                ).upper()
            tradable = sec_type_raw.lower() in (
                "",
                "equity",
                "etf",
                "mutual fund",
                "mutualfund",
                "stocks",
            ) and id_type_raw in ("", "TICKER", "SYMBOL", "RIC")

            # Corporate-bond flag for scenario.py credit shocks
            is_corporate = False
            if asset_class == "bond" and isinstance(asset, dict):
                sec_name = (asset.get("securityName") or asset.get("security_name") or "").lower()
                sector_lc = (sector or "").lower()
                haystack = f"{sec_name} {sector_lc}"
                if "treasury" in haystack or "t-bill" in haystack or "t-note" in haystack:
                    is_corporate = False
                elif "muni" in haystack or "municipal" in haystack:
                    is_corporate = False
                elif "corporate" in haystack or "corp" in haystack:
                    is_corporate = True
                else:
                    # Conservative default: assume credit spread risk present.
                    is_corporate = True

            position = Position(
                symbol=symbol,
                asset_class=asset_class,
                security_type=sec_type_raw or None,
                sector=sector,
                shares=_safe_float(shares),
                current_price=_safe_float(current_price),
                cost_basis_price=_safe_float(cost_basis_price),
                market_value=_safe_float(market_value),
                cost_basis=_safe_float(_first(pos, "costBasis", "cost_basis")),
                unrealized_gain_loss=_safe_float(
                    _first(pos, "unrealizedGainLoss", "unrealized_gain_loss")
                ),
                unrealized_gain_loss_pct=_safe_float(
                    _first(pos, "unrealizedGainLossPct", "unrealized_gain_loss_pct")
                ),
                cusip=cusip,
                isin=isin,
                modified_duration=_safe_float(_first(pos, "modifiedDuration", "modified_duration")),
                macaulay_duration=_safe_float(_first(pos, "macaulayDuration", "macaulay_duration")),
                convexity=_safe_float(pos.get("convexity")),
                coupon_rate=_safe_float(_first(pos, "couponRate", "coupon_rate", "coupon")),
                years_to_maturity=_safe_float(_first(pos, "yearsToMaturity", "years_to_maturity")),
                maturity_date=(
                    _first(pos, "maturityDate", "maturity_date")
                    or (asset.get("maturityDate") if isinstance(asset, dict) else None)
                    or (asset.get("maturity_date") if isinstance(asset, dict) else None)
                ),
                is_corporate=is_corporate,
                account=_first(pos, "accountId", "account_id", "account") or None,
                lots=self._extract_lots(pos, shares, cost_basis_price),
                tradable=tradable,
                raw=dict(pos),
            )
            return position
        except Exception as e:
            logger.debug("Skipping CDM position due to parse error: %s", e)
            return None

    def _parse_flat_holding(self, entry: Any) -> Optional[Position]:
        """Parse a legacy flat holding entry into a Position."""
        if not isinstance(entry, dict):
            return None
        symbol = entry.get("symbol") or entry.get("ticker") or entry.get("cusip")
        if not symbol:
            return None
        symbol = str(symbol)

        asset_type_raw = (entry.get("asset_type") or entry.get("assetType") or "").lower()
        security_type_raw = (entry.get("security_type") or entry.get("securityType") or "").lower()
        asset_class = self._classify_asset_class(
            security_type_raw, asset_type_raw, asset_type_raw, symbol
        )

        shares = _safe_float(entry.get("shares") or entry.get("quantity"))
        current_price = _safe_float(
            entry.get("current_price") or entry.get("currentPrice") or entry.get("price")
        )
        cost_basis_price = _safe_float(
            entry.get("cost_basis_price")
            or entry.get("costBasisPrice")
            or entry.get("purchase_price")
        )
        market_value = _safe_float(
            entry.get("market_value") or entry.get("marketValue") or entry.get("value")
        )
        if market_value is None and shares is not None and current_price is not None:
            market_value = float(shares) * float(current_price)

        return Position(
            symbol=symbol,
            asset_class=asset_class,
            security_type=(entry.get("security_type") or entry.get("securityType") or None),
            sector=entry.get("sector"),
            shares=shares,
            current_price=current_price,
            cost_basis_price=cost_basis_price,
            market_value=market_value,
            cost_basis=_safe_float(entry.get("cost_basis") or entry.get("costBasis")),
            unrealized_gain_loss=_safe_float(
                entry.get("unrealized_gain_loss") or entry.get("unrealizedGainLoss")
            ),
            unrealized_gain_loss_pct=_safe_float(
                entry.get("unrealized_gain_loss_pct") or entry.get("unrealizedGainLossPct")
            ),
            cusip=entry.get("cusip"),
            isin=entry.get("isin"),
            modified_duration=_safe_float(entry.get("modified_duration")),
            macaulay_duration=_safe_float(entry.get("macaulay_duration")),
            convexity=_safe_float(entry.get("convexity")),
            coupon_rate=_safe_float(entry.get("coupon_rate") or entry.get("coupon")),
            years_to_maturity=_safe_float(entry.get("years_to_maturity")),
            maturity_date=entry.get("maturity_date"),
            is_corporate=bool(entry.get("is_corporate")),
            account=entry.get("account") or entry.get("accountId"),
            lots=[],  # legacy flat holdings don't carry lot detail
            tradable=bool(entry.get("tradable", True)),
            raw=dict(entry),
        )

    @staticmethod
    def _classify_asset_class(
        security_type: str, asset_class: str, top_type: str, symbol: str
    ) -> str:
        """Unified asset-class classifier replacing per-loader heuristics."""
        haystack = f"{security_type} {asset_class} {top_type}".lower()
        # Margin checked before cash/equity fallbacks: legacy keyed
        # portfolios store positions under portfolio.margin, and CDM
        # positions can carry asset_type='margin' / security_type='margin
        # loan'. Without an explicit branch, those positions fall through
        # to the symbol-based infer_asset_class() heuristic and typically
        # end up classified as 'cash' (CASH/USD short-circuit) or 'equity'
        # (symbol heuristic), which understates leverage and distorts
        # downstream risk + news + allocation consumers.
        if "margin" in haystack:
            return "margin"
        if "cash" in haystack:
            return "cash"
        if symbol.upper() in ("CASH", "USD", "CASH_USD"):
            return "cash"
        if (
            "bond" in haystack
            or "treasury" in haystack
            or "muni" in haystack
            or "fixed" in haystack
        ):
            return "bond"
        if "crypto" in haystack or "cryptocurrency" in haystack:
            return "crypto"
        if "future" in haystack:
            return "futures"
        if "commodity" in haystack or "metal" in haystack:
            return "metals"
        if "equity" in haystack or "stock" in haystack or "etf" in haystack or "fund" in haystack:
            return "equity"
        if "option" in haystack or "derivative" in haystack:
            return "derivative"
        # Fall back to symbol-based heuristic
        return infer_asset_class(symbol)

    @staticmethod
    def _extract_lots(
        pos: Mapping[str, Any],
        fallback_shares: Optional[float],
        fallback_cost_basis: Optional[float],
    ) -> List[Dict[str, Any]]:
        """Extract lot-level detail (or synthesize a single aggregate lot).

        Reproduces the behaviour of rebalance_tax.py._extract_lots() so that
        lot-aware commands can switch to the unified loader without losing
        tax-lot fidelity.
        """
        lots_raw = _first(pos, "lots", "taxLots", "tax_lots")
        out: List[Dict[str, Any]] = []
        if isinstance(lots_raw, list) and lots_raw:
            for lot in lots_raw:
                if not isinstance(lot, dict):
                    continue
                lot_shares = _safe_float(_amount_of(lot.get("quantity")) or lot.get("shares"))
                if lot_shares is None:
                    continue
                lot_cb = _safe_float(
                    _amount_of(
                        _first(lot, "costBasisPrice", "cost_basis_price", "costBasis", "cost_basis")
                    )
                    or lot.get("price")
                )
                lot_date = _first(
                    lot,
                    "acquisitionDate",
                    "acquisition_date",
                    "purchaseDate",
                    "purchase_date",
                    "tradeDate",
                    "trade_date",
                    "openDate",
                    "open_date",
                )
                out.append(
                    {
                        "shares": lot_shares,
                        "cost_basis_price": lot_cb,
                        "acquisition_date": lot_date,
                    }
                )
            if out:
                return out

        # Fallback: synthesize a single aggregate lot from position-level data
        if fallback_shares is None:
            return []
        agg_date = _first(
            pos,
            "acquisitionDate",
            "acquisition_date",
            "purchaseDate",
            "purchase_date",
            "tradeDate",
            "trade_date",
        )
        return [
            {
                "shares": float(fallback_shares),
                "cost_basis_price": fallback_cost_basis,
                "acquisition_date": agg_date,
            }
        ]


# ---------------------------------------------------------------------------
# Module-level convenience (thin wrapper — legacy function-style callers)
# ---------------------------------------------------------------------------

_default_loader = HoldingsLoader()


def load_portfolio(source: Union[str, Path, Mapping[str, Any]]) -> PortfolioData:
    """Load a portfolio using the default HoldingsLoader.

    Thin functional wrapper for callers that don't want to construct the
    loader explicitly. Equivalent to::

        HoldingsLoader().load(source)
    """
    return _default_loader.load(source)


__all__ = [
    "HoldingsLoader",
    "PortfolioData",
    "Position",
    "infer_asset_class",
    "load_portfolio",
]
