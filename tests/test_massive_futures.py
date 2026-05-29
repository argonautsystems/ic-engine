"""Massive futures support — contract spec parsing + notional valuation.

Covers providers/futures_spec.py (pure, deterministic) and the futures
branches of models/holdings.Holding (notional value, cost basis, unrealized
P&L). Live provider calls are exercised separately against the partner key;
these are offline unit tests.
"""

from __future__ import annotations

import pytest

from ic_engine.models.holdings import Holding
from ic_engine.providers import futures_spec as fs

# ---- futures_spec ----------------------------------------------------------


@pytest.mark.parametrize(
    "ticker,product,month,year",
    [
        ("ESH6", "ES", 3, 2026),
        ("MESH26", "MES", 3, 2026),
        ("CLZ5", "CL", 12, 2025),
        ("6EH6", "6E", 3, 2026),
        ("GCM6", "GC", 6, 2026),
        ("BTCQ6", "BTC", 8, 2026),
        ("ZNH6", "ZN", 3, 2026),
    ],
)
def test_parse_contract_ticker(ticker, product, month, year):
    assert fs.parse_contract_ticker(ticker) == (product, month, year)
    assert fs.is_futures_ticker(ticker)
    assert fs.product_code(ticker) == product


@pytest.mark.parametrize("sym", ["AAPL", "ESZ", "", "BRK.B", "GOOGL"])
def test_non_futures_rejected(sym):
    assert fs.parse_contract_ticker(sym) is None
    assert not fs.is_futures_ticker(sym)


@pytest.mark.parametrize(
    "sym,expected_product,expected_multiplier",
    [
        (None, None, fs.DEFAULT_MULTIPLIER),
        ("ESZ25", "ES", 50.0),
        ("/ESZ25", "ES", 50.0),
        ("@GCZ25", "GC", 100.0),
        ("AAPL", None, fs.DEFAULT_MULTIPLIER),
    ],
)
def test_futures_prefix_parse_classify_and_multiplier(sym, expected_product, expected_multiplier):
    assert fs.product_code(sym) == expected_product
    assert fs.is_futures_ticker(sym) is (expected_product is not None)
    assert fs.contract_multiplier(sym) == expected_multiplier


def test_multipliers_and_notional():
    assert fs.contract_multiplier("ESH6") == 50.0
    assert fs.contract_multiplier("MES") == 5.0
    assert fs.contract_multiplier("CLZ5") == 1000.0
    # Unknown product degrades to 1.0, not an error.
    assert fs.contract_multiplier("ZZZ9") == 1.0
    assert fs.notional_value(5000.0, 2, "ESH6") == 500000.0
    assert fs.notional_value(70.0, 1, "CLZ5") == 70000.0


# ---- Holding futures valuation ---------------------------------------------


def _future(**kw):
    base = dict(
        symbol="ESZ25",
        asset_type="futures",
        shares=2,
        current_price=5000.0,
        purchase_price=4800.0,
        contract_symbol="ESZ25",
    )
    base.update(kw)
    return Holding(**base)


def test_futures_value_is_notional_via_spec_fallback():
    h = _future()  # no explicit contract_size -> ES multiplier 50
    assert h.value == 2 * 5000.0 * 50.0  # 500_000
    assert h.cost_basis == 2 * 4800.0 * 50.0  # 480_000
    assert h.unrealized_gain_loss == pytest.approx(20000.0)
    assert h.unrealized_gain_loss_pct == pytest.approx(20000.0 / 480000.0)


def test_explicit_contract_size_wins_over_spec():
    h = _future(contract_size=10.0)  # micro-ish override
    assert h.value == 2 * 5000.0 * 10.0
    assert h.cost_basis == 2 * 4800.0 * 10.0


def test_explicit_market_value_short_circuits():
    h = _future(market_value=123456.0)
    assert h.value == 123456.0


def test_equity_unaffected():
    eq = Holding(
        symbol="AAPL",
        asset_type="equity",
        shares=10,
        current_price=200.0,
        purchase_price=150.0,
    )
    assert eq.value == 2000.0  # plain shares*price, no multiplier
    assert eq.cost_basis == 1500.0
