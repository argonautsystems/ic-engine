"""Futures contract specifications + symbol parsing.

Massive (polygon.io) launched a Futures API (CME Globex: CBOT/CME/NYMEX/COMEX).
The data feed gives contract metadata + prices but NOT the dollar multiplier
needed to turn a price into a notional value, so this module carries a curated
map of the common CME product multipliers plus helpers to recognise and parse
futures contract tickers (e.g. ``ESH6`` -> product ``ES``, month March, 2026).

Notional value of a position = price * contract_multiplier(product) * quantity.
"""

from __future__ import annotations

import re
from datetime import date

# CME month codes (single letter appended to the product code on a contract).
MONTH_CODES: dict[str, int] = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
_MONTH_LETTERS = "".join(MONTH_CODES.keys())

# Dollar value of one full point move, per product code. Curated from CME
# contract specs. Micros are 1/10th of their full-size sibling. Extend as
# Massive adds products; unknown products fall back to DEFAULT_MULTIPLIER.
FUTURES_MULTIPLIERS: dict[str, float] = {
    # Equity index
    "ES": 50.0, "MES": 5.0,        # E-mini / Micro S&P 500
    "NQ": 20.0, "MNQ": 2.0,        # E-mini / Micro Nasdaq-100
    "YM": 5.0, "MYM": 0.5,         # E-mini / Micro Dow
    "RTY": 50.0, "M2K": 5.0,       # E-mini / Micro Russell 2000
    # Energy
    "CL": 1000.0, "MCL": 100.0,    # WTI crude / Micro
    "NG": 10000.0,                 # Henry Hub natural gas
    "RB": 42000.0, "HO": 42000.0,  # RBOB gasoline / heating oil (per gallon * 42k)
    "BZ": 1000.0,                  # Brent crude
    # Metals
    "GC": 100.0, "MGC": 10.0,      # Gold / Micro
    "SI": 5000.0, "SIL": 1000.0,   # Silver / Micro
    "HG": 25000.0,                 # Copper (per lb * 25k)
    "PL": 50.0, "PA": 100.0,       # Platinum / Palladium
    # Rates (per point = $1000 face/point for note/bond complex)
    "ZT": 2000.0, "ZF": 1000.0, "ZN": 1000.0, "ZB": 1000.0, "UB": 1000.0,
    "TN": 1000.0,
    # FX
    "6E": 125000.0, "6J": 12500000.0, "6B": 62500.0, "6C": 100000.0,
    "6A": 100000.0, "6S": 125000.0, "6N": 100000.0, "6M": 500000.0,
    # Agriculture (grains: per bushel * 50)
    "ZC": 50.0, "ZW": 50.0, "ZS": 50.0, "ZM": 100.0, "ZL": 600.0,
    "ZO": 50.0, "ZR": 2000.0,
    # Softs / livestock
    "LE": 400.0, "HE": 400.0, "GF": 500.0,
    # Crypto (CME)
    "BTC": 5.0, "MBT": 0.1, "ETH": 50.0, "MET": 0.1,
}

DEFAULT_MULTIPLIER = 1.0

# A contract ticker is a product code (1-3 alphanumerics) + a month letter +
# a 1-2 digit year, e.g. ESH6, MESH26, CLZ5, 6EH6. Anchored, case-sensitive on
# the month letter to avoid matching ordinary equity tickers.
_CONTRACT_RE = re.compile(rf"^([A-Z0-9]{{1,3}}?)([{_MONTH_LETTERS}])(\d{{1,2}})$")


def parse_contract_ticker(ticker: str) -> tuple[str, int, int] | None:
    """Parse a futures contract ticker into (product_code, month, year).

    Returns ``None`` when ``ticker`` is not a futures contract ticker. The year
    is normalised to a full 4-digit year using a 2000-rollover for 2-digit and
    current-decade for 1-digit forms. Only products in :data:`FUTURES_MULTIPLIERS`
    are accepted, so equity tickers that happen to fit the shape (e.g. a symbol
    ending in a month letter) are not misclassified.
    """
    if not ticker:
        return None
    m = _CONTRACT_RE.match(ticker.strip().upper())
    if not m:
        return None
    product, month_letter, year_digits = m.groups()
    if product not in FUTURES_MULTIPLIERS:
        return None
    month = MONTH_CODES[month_letter]
    yr = int(year_digits)
    if len(year_digits) == 1:
        # Single digit -> nearest year in the current decade.
        base = (date.today().year // 10) * 10
        year = base + yr
    else:
        year = 2000 + yr
    return product, month, year


def is_futures_ticker(symbol: str) -> bool:
    """True when ``symbol`` looks like a known futures contract ticker."""
    return parse_contract_ticker(symbol) is not None


def product_code(symbol: str) -> str | None:
    """Return the product code for a contract ticker, or ``None``."""
    parsed = parse_contract_ticker(symbol)
    return parsed[0] if parsed else None


def contract_multiplier(symbol_or_product: str) -> float:
    """Dollar multiplier for a contract ticker or bare product code.

    Falls back to :data:`DEFAULT_MULTIPLIER` for unknown products so notional
    math degrades to price*quantity rather than raising.
    """
    if not symbol_or_product:
        return DEFAULT_MULTIPLIER
    key = symbol_or_product.strip().upper()
    if key in FUTURES_MULTIPLIERS:
        return FUTURES_MULTIPLIERS[key]
    prod = product_code(key)
    if prod and prod in FUTURES_MULTIPLIERS:
        return FUTURES_MULTIPLIERS[prod]
    return DEFAULT_MULTIPLIER


def notional_value(price: float, quantity: float, symbol_or_product: str) -> float:
    """Notional USD exposure of ``quantity`` contracts at ``price``."""
    return float(price) * float(quantity) * contract_multiplier(symbol_or_product)
