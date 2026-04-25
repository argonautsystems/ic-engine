#!/usr/bin/env python3
"""
Type-safe schema validation using Pydantic v2.

Replaces hand-coded format detection with declarative models that:
- Validate input data automatically
- Convert between formats safely
- Provide clear error messages
- Enable IDE autocomplete and type checking

Supports: Legacy (equity/bond/cash), Disclaimer-wrapped, FINOS CDM, InvestorClaw canonical.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator

try:
    from typing import Annotated  # noqa: F401
except ImportError:
    pass  # Python < 3.9


# ─── Input Models (for parsing) ─────────────────────────────────────────────


class CDMProductIdentifier(BaseModel):
    """FINOS CDM product identifier."""

    identifier: str
    identifierType: Optional[str] = None
    identifierIssuer: Optional[str] = None


class CDMProduct(BaseModel):
    """FINOS CDM product structure."""

    productIdentifier: Optional[CDMProductIdentifier] = None
    securityType: Optional[str] = None


class CDMQuantity(BaseModel):
    """FINOS CDM quantity with value."""

    amount: Optional[float] = None
    unit: Optional[str] = None


class CDMPrice(BaseModel):
    """FINOS CDM price structure."""

    amount: Optional[float] = None
    currency: Optional[str] = None


class CDMPriceQuantity(BaseModel):
    """FINOS CDM price-quantity pair."""

    quantity: Optional[CDMQuantity] = None
    currentPrice: Optional[CDMPrice] = None


class CDMPosition(BaseModel):
    """FINOS CDM position (holding)."""

    product: Optional[CDMProduct] = None
    asset: Optional[CDMProduct] = None
    priceQuantity: Optional[CDMPriceQuantity] = None
    quantity: Optional[float] = None
    currentPrice: Optional[float] = None
    marketValue: Optional[float] = None


class CDMPortfolioState(BaseModel):
    """FINOS CDM portfolio state."""

    positions: List[CDMPosition] = Field(default_factory=list)


class CDMPortfolio(BaseModel):
    """FINOS CDM portfolio structure."""

    portfolioState: Optional[CDMPortfolioState] = None
    aggregationParameters: Optional[Dict[str, Any]] = None


class CDMInput(BaseModel):
    """Top-level FINOS CDM input."""

    cdmVersion: str
    portfolio: CDMPortfolio


# ─── Output Models (canonical) ──────────────────────────────────────────────


class Holding(BaseModel):
    """Single portfolio holding (canonical format)."""

    symbol: str = Field(..., description="Ticker symbol (e.g., AAPL, BND)")
    quantity: float = Field(..., description="Number of shares/units")
    current_price: Optional[float] = Field(None, description="Current price per unit")
    current_value: Optional[float] = Field(
        None, description="Total current value (quantity * price)"
    )
    purchase_price: Optional[float] = Field(None, description="Original purchase price")
    purchase_date: Optional[str] = Field(None, description="Date purchased (YYYY-MM-DD)")
    asset_type: str = Field(
        "equity", description="Asset class: equity, bond, cash, crypto, futures, metals"
    )
    account: Optional[str] = Field(None, description="Account name/type")

    @field_validator("purchase_date")
    @classmethod
    def validate_date(cls, v: Optional[str]) -> Optional[str]:
        """Ensure date is YYYY-MM-DD format."""
        if v and isinstance(v, str):
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                try:
                    parsed = datetime.fromisoformat(v).date()
                    return parsed.strftime("%Y-%m-%d")
                except ValueError:
                    raise ValueError(f"Invalid date format: {v}")
        return v

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        """Normalize symbol: uppercase, no whitespace."""
        return v.strip().upper()


class Portfolio(BaseModel):
    """Portfolio structure (canonical format)."""

    holdings: List[Holding] = Field(default_factory=list)
    account_name: Optional[str] = Field(None)
    currency: str = Field("USD")
    timestamp: Optional[datetime] = Field(default_factory=datetime.utcnow)

    def total_value(self) -> float:
        """Calculate total portfolio value."""
        return sum(h.current_value or 0.0 for h in self.holdings)

    def by_asset_type(self) -> Dict[str, List[Holding]]:
        """Group holdings by asset type."""
        grouped: Dict[str, List[Holding]] = {}
        for holding in self.holdings:
            if holding.asset_type not in grouped:
                grouped[holding.asset_type] = []
            grouped[holding.asset_type].append(holding)
        return grouped


# ─── Conversion Functions ──────────────────────────────────────────────────


def convert_cdm_to_canonical(cdm_data: Union[Dict[str, Any], CDMInput]) -> Portfolio:
    """
    Convert FINOS CDM structure to canonical Portfolio model.

    Type-safe conversion with validation at each step.
    """
    # Parse input
    if isinstance(cdm_data, dict):
        cdm = CDMInput(**cdm_data)
    else:
        cdm = cdm_data

    # Extract holdings from CDM positions
    holdings = []
    if cdm.portfolio.portfolioState and cdm.portfolio.portfolioState.positions:
        for position in cdm.portfolio.portfolioState.positions:
            # Determine symbol
            symbol = None
            if position.product and position.product.productIdentifier:
                symbol = position.product.productIdentifier.identifier
            elif position.asset and position.asset.productIdentifier:
                symbol = position.asset.productIdentifier.identifier

            if not symbol:
                continue  # Skip positions without symbol

            # Determine asset type
            asset_type = "equity"
            if position.asset:
                sec_type = (position.asset.securityType or "").lower()
                if "bond" in sec_type:
                    asset_type = "bond"
                elif "cash" in sec_type:
                    asset_type = "cash"
                elif "crypto" in sec_type:
                    asset_type = "crypto"
                elif "future" in sec_type:
                    asset_type = "futures"
                elif "commodity" in sec_type or "metal" in sec_type:
                    asset_type = "metals"

            # Extract quantity and price
            quantity = position.quantity or 0.0
            current_price = position.currentPrice
            if position.priceQuantity:
                if position.priceQuantity.quantity:
                    quantity = position.priceQuantity.quantity.amount or quantity
                if position.priceQuantity.currentPrice:
                    current_price = position.priceQuantity.currentPrice.amount

            current_value = position.marketValue
            if current_value is None and current_price is not None:
                current_value = quantity * current_price

            # Create holding with validation
            holding = Holding(
                symbol=symbol,
                quantity=quantity,
                current_price=current_price,
                current_value=current_value,
                asset_type=asset_type,
            )
            holdings.append(holding)

    return Portfolio(holdings=holdings)


def validate_portfolio(data: Dict[str, Any]) -> Portfolio:
    """
    Validate and normalize portfolio data.

    Auto-detects format (legacy, CDM, etc.) and returns canonical Portfolio.
    """
    # Try CDM first
    if "cdmVersion" in data and "portfolio" in data:
        return convert_cdm_to_canonical(data)

    # Try legacy format (direct holdings dict)
    if "holdings" in data:
        holdings_data = data["holdings"]
        if isinstance(holdings_data, dict):
            # Convert dict to list
            holdings_list = [{**h, "symbol": sym} for sym, h in holdings_data.items()]
        else:
            holdings_list = holdings_data

        return Portfolio(holdings=[Holding(**h) for h in holdings_list])

    # Try disclaimer-wrapped format
    if "data" in data and "portfolio" in data["data"]:
        return validate_portfolio(data["data"]["portfolio"])

    # Fallback: treat as Portfolio directly
    return Portfolio(**data)


def portfolio_to_dict(portfolio: Portfolio, include_timestamp: bool = True) -> Dict[str, Any]:
    """Convert canonical Portfolio to dict for serialization."""
    return portfolio.model_dump(
        exclude_none=True, exclude={"timestamp"} if not include_timestamp else set()
    )


# ─── Backward Compatibility ────────────────────────────────────────────────


def normalize_portfolio_compat(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Backward-compatible function matching old schema.normalize_portfolio signature.

    Validates with pydantic, returns dict in old format.
    """
    portfolio = validate_portfolio(data)
    return portfolio_to_dict(portfolio)
