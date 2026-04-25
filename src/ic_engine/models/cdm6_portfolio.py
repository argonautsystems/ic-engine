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
CDM 6.0 Unified Portfolio Model — Single Canonical Data Structure

FINOS Common Domain Model 6.0 compliant representation of all portfolio products.
Replaces CDM 5.x by unifying all asset types (equity, bond, crypto, futures, commodity)
under a single Trade → Payout abstraction.

Key improvements:
1. **Unified Trade Model**: Single Trade class represents all product types
2. **Lifecycle Events**: Corporate actions, dividends, coupons, settlements modeled as events
3. **Counterparty Tracking**: Custodian, clearing member, advisor relationships explicit
4. **Regulatory Fields**: MiFID II, EMIR, SEC reporting fields built-in
5. **Audit Trail Ready**: Events are first-class, integrate with enterprise audit ledger

Architecture:
- Trade: Core contract (what was bought, at what price, when)
- Product: What was bought (equity, bond, futures, crypto, etc.)
- Payout: How it pays out (dividend, coupon, interest, settlement)
- Event: What happened (corporate action, dividend paid, maturity reached, etc.)
- Party: Counterparties (custodian, advisor, transfer agent, etc.)

References:
- https://github.com/finos/common-domain-model
- EMIR Article 57 (trade reporting)
- MiFID II Annex 1 (detailed product info)
- SEC Form 13F (portfolio holdings)
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Enumerations (CDM 6.0 standard values)
# ─────────────────────────────────────────────────────────────────────────────


class ProductType(Enum):
    """CDM 6.0 product types (unified classification)."""

    EQUITY = "Equity"
    BOND = "Bond"
    FUTURE = "Futures"
    OPTION = "Option"
    CRYPTO = "Crypto"
    COMMODITY = "Commodity"
    CASH = "Cash"
    FUND = "Fund"  # Mutual fund or ETF


class PayoutType(Enum):
    """CDM 6.0 payout types (how the product pays)."""

    EQUITY_PAYOUT = "EquityPayout"  # Dividends, stock splits
    FIXED_RATE_PAYOUT = "FixedRatePayout"  # Bonds, fixed coupons
    FLOATING_RATE_PAYOUT = "FloatingRatePayout"  # Floating rate bonds
    INTEREST_RATE_PAYOUT = "InterestRatePayout"  # Swaps, interest rate futures
    COMMODITY_PAYOUT = "CommodityPayout"  # Futures, forwards, commodity spots
    CRYPTO_PAYOUT = "CryptoPayout"  # Cryptocurrency
    CASH_PAYOUT = "CashPayout"  # Cash, money market


class EventType(Enum):
    """CDM 6.0 lifecycle events (what happens to a trade)."""

    TRADE_CAPTURED = "TradeCaptured"
    CORPORATE_ACTION = "CorporateAction"  # Stock split, dividend
    DIVIDEND_PAID = "DividendPaid"
    COUPON_PAID = "CouponPaid"
    SETTLEMENT = "Settlement"
    MATURITY = "Maturity"
    EXERCISE = "Exercise"  # Options
    EXPIRY = "Expiry"  # Futures
    TRANSFER = "Transfer"  # Custody transfer
    CORRECTION = "Correction"  # EMIR correction


class CorporateActionType(Enum):
    """Types of corporate actions."""

    DIVIDEND_CASH = "CashDividend"
    DIVIDEND_STOCK = "StockDividend"
    STOCK_SPLIT = "StockSplit"
    REVERSE_SPLIT = "ReverseSplit"
    RIGHTS_OFFERING = "RightsOffering"
    SPINOFF = "Spinoff"
    MERGER = "Merger"
    NAME_CHANGE = "NameChange"


# ─────────────────────────────────────────────────────────────────────────────
# Core CDM 6.0 Data Structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ProductIdentifier:
    """Uniquely identifies a product (security, instrument)."""

    identifier_type: str  # "TICKER", "ISIN", "CUSIP", "FIGI", "CRYPTO_SYMBOL"
    identifier: str  # "AAPL", "US0378691033", "912810CH0", "BBADF00D", "BTC"
    exchange: Optional[str] = None  # "NYSE", "NASDAQ", "LSE"
    currency: Optional[str] = None  # "USD", "EUR"


@dataclass
class Product:
    """CDM 6.0 product (the thing we hold)."""

    product_type: ProductType
    identifiers: List[ProductIdentifier]
    product_name: str
    description: Optional[str] = None

    # Product-specific metadata
    class_: Optional[str] = None  # e.g., "Equity", "Fixed Income" (classification)
    sector: Optional[str] = None  # "Technology", "Healthcare"
    industry: Optional[str] = None  # "Semiconductors"

    # For fixed income
    issuer_name: Optional[str] = None
    credit_rating: Optional[str] = None  # "AAA", "BBB", etc.

    # For futures
    underlier: Optional[str] = None  # What the future is on (e.g., "ES" for E-mini S&P)
    contract_size: Optional[float] = None

    # For crypto
    blockchain: Optional[str] = None  # "Ethereum", "Bitcoin"

    # For commodities
    commodity_type: Optional[str] = None  # "PRECIOUS_METALS", "ENERGY"


@dataclass
class Quantity:
    """Amount held (shares, units, contracts)."""

    amount: float
    unit: str = "shares"


@dataclass
class Price:
    """Price per unit with currency."""

    amount: float
    currency: str = "USD"
    as_of_date: str = ""  # ISO 8601 date


@dataclass
class Party:
    """Counterparty (custodian, advisor, transfer agent, clearing member)."""

    party_id: str  # Unique identifier
    party_name: str
    party_role: str  # "Custodian", "Advisor", "ClearingMember", "TransferAgent"
    party_type: str  # "Organization", "Individual"
    contact_info: Optional[Dict[str, str]] = None


@dataclass
class CostBasis:
    """Tax lot information (for wash sale, capital gains calculation)."""

    lot_id: str  # Unique identifier within position
    acquisition_date: str  # ISO 8601
    quantity: float
    unit_cost: float
    total_cost: float
    holding_period: Optional[str] = None  # "Long-term", "Short-term"
    gain_loss: Optional[float] = None  # Unrealized gain/loss
    markup_notes: Optional[str] = None  # For inherited lots, etc.


@dataclass
class Trade:
    """CDM 6.0 Trade — Core holding (unified for all product types)."""

    trade_id: str  # Unique identifier
    product: Product
    quantity: Quantity
    price: Price
    trade_date: str  # ISO 8601 (when acquired)
    settlement_date: Optional[str] = None  # ISO 8601
    counterparty: Optional[Party] = None
    account_id: Optional[str] = None

    # Current valuation
    current_price: Optional[Price] = None
    valuation_date: Optional[str] = None  # ISO 8601

    # Tax lot tracking
    cost_basis_lots: List[CostBasis] = field(default_factory=list)

    # Metadata
    source_system: str = "investorclaw"
    last_updated: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class Payout:
    """CDM 6.0 Payout — How the product pays (dividends, coupons, interest)."""

    payout_type: PayoutType
    payout_date: Optional[str] = None  # ISO 8601
    payout_amount: Optional[float] = None

    # For dividends
    dividend_yield: Optional[float] = None
    dividend_frequency: Optional[str] = None  # "Quarterly", "Annual"

    # For bonds
    coupon_rate: Optional[float] = None
    coupon_frequency: Optional[str] = None  # "Semi-annual", "Monthly"
    maturity_date: Optional[str] = None  # ISO 8601

    # For cash/interest
    interest_rate: Optional[float] = None
    accrued_interest: Optional[float] = None

    # Regulatory fields (EMIR, MiFID II)
    is_regulated_payout: bool = False
    underlier_currency: Optional[str] = None


@dataclass
class LifecycleEvent:
    """CDM 6.0 Lifecycle Event — Something that happened to the trade."""

    event_id: str
    event_type: EventType
    trade_id: str  # Links to Trade
    event_date: str  # ISO 8601
    effective_date: str  # ISO 8601 (business date)

    # Event details
    details: Dict[str, Any] = field(default_factory=dict)

    # For dividends/coupons
    amount: Optional[float] = None
    number_of_shares: Optional[float] = None
    per_share_amount: Optional[float] = None

    # For corporate actions
    corporate_action_type: Optional[CorporateActionType] = None

    # Audit trail
    recorded_by: Optional[str] = None
    recorded_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class CDM6Portfolio:
    """CDM 6.0 Portfolio — Complete holding with lifecycle events."""

    portfolio_id: str
    portfolio_name: str
    account_id: Optional[str] = None
    custodian: Optional[Party] = None
    advisor: Optional[Party] = None

    # Core positions
    trades: List[Trade] = field(default_factory=list)

    # Lifecycle history
    events: List[LifecycleEvent] = field(default_factory=list)

    # Payouts (expected and historical)
    payouts: List[Payout] = field(default_factory=list)

    # Metadata
    as_of_date: str = field(default_factory=lambda: date.today().isoformat())
    cdm_version: str = "6.0"
    creation_timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def total_value(self) -> float:
        """Calculate total portfolio value."""
        total = 0.0
        for trade in self.trades:
            if trade.current_price:
                total += trade.quantity.amount * trade.current_price.amount
        return total

    def position_count_by_type(self) -> Dict[ProductType, int]:
        """Count positions by product type."""
        counts = {}
        for trade in self.trades:
            counts[trade.product.product_type] = counts.get(trade.product.product_type, 0) + 1
        return counts


# ─────────────────────────────────────────────────────────────────────────────
# Migration Utilities (CDM 5.x → CDM 6.0)
# ─────────────────────────────────────────────────────────────────────────────


def cdm5_holding_to_cdm6_trade(holding) -> Trade:
    """
    Migrate CDM 5.x Holding to CDM 6.0 Trade.

    Maps CDM 5.x asset_type to CDM 6.0 ProductType + PayoutType.
    Preserves all tax lot information.
    """
    # Map asset_type to ProductType
    asset_type_map = {
        "equity": ProductType.EQUITY,
        "bond": ProductType.BOND,
        "municipal_bond": ProductType.BOND,
        "crypto": ProductType.CRYPTO,
        "futures": ProductType.FUTURE,
        "commodity": ProductType.COMMODITY,
        "metals": ProductType.COMMODITY,
        "cash": ProductType.CASH,
        "etf": ProductType.EQUITY,
        "mutual_fund": ProductType.FUND,
    }

    product_type = asset_type_map.get(holding.asset_type.lower(), ProductType.EQUITY)

    # Create ProductIdentifier
    identifiers = [
        ProductIdentifier(
            identifier_type="TICKER",
            identifier=holding.symbol,
            currency=getattr(holding, "currency", "USD"),
        )
    ]
    if hasattr(holding, "cusip") and holding.cusip:
        identifiers.append(ProductIdentifier(identifier_type="CUSIP", identifier=holding.cusip))

    # Create Product
    product = Product(
        product_type=product_type,
        identifiers=identifiers,
        product_name=holding.symbol,
        sector=holding.sector if hasattr(holding, "sector") else None,
        issuer_name=getattr(holding, "bond_name", None),
        credit_rating=getattr(holding, "credit_quality", None),
        contract_size=getattr(holding, "contract_size", None),
        blockchain=getattr(holding, "blockchain", None),
    )

    # Create Trade
    trade = Trade(
        trade_id=f"trade-{holding.symbol}-{int(datetime.utcnow().timestamp())}",
        product=product,
        quantity=Quantity(amount=holding.shares, unit="shares"),
        price=Price(amount=holding.purchase_price, currency="USD"),
        trade_date=holding.purchase_date
        if holding.purchase_date != "N/A"
        else date.today().isoformat(),
        account_id=getattr(holding, "account", None),
        current_price=Price(amount=holding.current_price, currency="USD"),
        valuation_date=date.today().isoformat(),
    )

    # Add cost basis lot
    if hasattr(holding, "cost_basis_lots") and holding.cost_basis_lots:
        trade.cost_basis_lots = holding.cost_basis_lots
    else:
        # Create single cost basis lot from holding
        trade.cost_basis_lots.append(
            CostBasis(
                lot_id=f"lot-{holding.symbol}-0",
                acquisition_date=holding.purchase_date
                if holding.purchase_date != "N/A"
                else date.today().isoformat(),
                quantity=holding.shares,
                unit_cost=holding.purchase_price,
                total_cost=holding.shares * holding.purchase_price,
            )
        )

    return trade
