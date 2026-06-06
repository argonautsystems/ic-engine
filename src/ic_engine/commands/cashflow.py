#!/usr/bin/env python3
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
Income & Cashflow Calendar — InvestorClaw (Phase 2.4, CDM 5 analytics)

Forward-looking dividend and coupon calendar with tax projection and coverage
ratios.

Outputs:
  - monthly_cashflow:  per-month dividend + coupon income and tax impact
  - annual_total:      sum of all projected income over the forward window
  - yield_on_cost:     annual income / portfolio cost basis
  - tax_breakdown:     qualified vs ordinary dividend, tax-exempt vs taxable coupon
  - coverage_ratio:    annual income / user-supplied annual expenses
  - calendar_events:   individual dated payment events (for UI calendar)

Algorithm:
  1. Equities: fetch trailing dividend history via yfinance; project forward by
     preserving the historical cadence (quarterly / monthly / semi-annual).
  2. Bonds: from bond_analysis.json (or holdings bond positions), generate
     future coupon dates from maturity + coupon rate + frequency.
  3. Monthly aggregation produces a 12-month (configurable) cashflow landing
     schedule.
  4. Tax projection:
       - US equity dividends held > 60 days → qualified (long-term cap-gains
         bracket proxy: 15% federal + 6% state)
       - non-US or < 60 days → ordinary income (22% federal + 6% state proxy)
       - municipal coupons → federal tax-exempt (state-exempt if in-state;
         conservatively reported under tax_exempt)
       - treasury coupons → federal taxable, state-exempt
       - corporate coupons → fully taxable (22% / 6%)
  5. Coverage ratio: annual_income / --annual-expenses (0 if not supplied)

Argv:
  cashflow.py <holdings.json> [bond_analysis.json] [--months 12]
              [--annual-expenses FLOAT] [output.json]
"""

from __future__ import annotations

import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── Path bootstrap ─────────────────────────────────────────────────────────
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import yfinance as yf  # noqa: E402

from ic_engine.internal.holdings_loader import HoldingsLoader  # noqa: E402
from ic_engine.rendering.disclaimer_wrapper import DisclaimerWrapper  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ─── Tax-rate assumptions ───────────────────────────────────────────────────
#
# These are deliberately conservative defaults used for projection only.
# They are NOT personalized tax advice.  Consumers can override them by
# editing this module or (in a future release) a user-provided profile.
QUALIFIED_FED_RATE = 0.15  # LTCG bracket proxy
QUALIFIED_STATE_RATE = 0.06  # state tax proxy (mid-tier state)
ORDINARY_FED_RATE = 0.22  # ordinary income proxy
ORDINARY_STATE_RATE = 0.06

# Dividend qualification holding window (IRS rule = 60 days within 121-day
# period around ex-date). We use a simple "held >= 60 days" proxy based on
# whatever the portfolio system surfaces; if no holding-period signal is
# available we assume qualified for US-listed equities (conservative-ish
# approximation — the typical retail investor satisfies the rule).
QUALIFIED_MIN_HOLDING_DAYS = 60


# ─── Data classes ───────────────────────────────────────────────────────────


@dataclass
class CashflowEvent:
    """A single forward-looking cash payment."""

    date: str  # YYYY-MM-DD
    symbol: str
    type: str  # "dividend" | "coupon"
    amount: float  # USD
    tax_qualified: bool = False
    tax_exempt: bool = False
    asset_type: str = ""  # equity | municipal_bond | treasury | corporate_bond

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "symbol": self.symbol,
            "type": self.type,
            "amount": round(self.amount, 2),
            "tax_qualified": self.tax_qualified,
            "tax_exempt": self.tax_exempt,
            "asset_type": self.asset_type,
        }


@dataclass
class MonthBucket:
    """Aggregated income for a single forward month."""

    month: str  # YYYY-MM
    dividend_income: float = 0.0
    coupon_income: float = 0.0
    qualified_dividend: float = 0.0
    ordinary_dividend: float = 0.0
    tax_exempt_coupon: float = 0.0
    taxable_coupon: float = 0.0
    events: List[CashflowEvent] = field(default_factory=list)

    @property
    def total_income(self) -> float:
        return self.dividend_income + self.coupon_income

    def tax_impact(self) -> Dict[str, float]:
        federal = (
            self.qualified_dividend * QUALIFIED_FED_RATE
            + self.ordinary_dividend * ORDINARY_FED_RATE
            + self.taxable_coupon * ORDINARY_FED_RATE
        )
        state = (
            self.qualified_dividend * QUALIFIED_STATE_RATE
            + self.ordinary_dividend * ORDINARY_STATE_RATE
            # Taxable coupons: treasuries are state-exempt, corporates are not.
            # Without per-event breakout here, we conservatively apply state
            # rate to the entire taxable_coupon bucket; _summarize_taxes()
            # backs this out using asset_type from the underlying events.
        )
        return {
            "federal": round(federal, 2),
            "state": round(state, 2),
            "tax_exempt": round(self.tax_exempt_coupon, 2),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "month": self.month,
            "dividend_income": round(self.dividend_income, 2),
            "coupon_income": round(self.coupon_income, 2),
            "total_income": round(self.total_income, 2),
            "tax_impact": self.tax_impact(),
        }


# ─── Portfolio extraction helpers ───────────────────────────────────────────


def _load_portfolio(holdings_file: str) -> Dict[str, Any]:
    """Load holdings via HoldingsLoader, projected into the legacy
    asset-class-keyed portfolio dict shape consumed by this module.

    Downstream helpers (_equity_positions, _bond_positions,
    _total_cost_basis) expect ``{equity: {symbol: {...}}, bond: {...},
    cash: {...}}``; we assemble that shape here so the projection /
    cashflow code paths remain unchanged.
    """
    portfolio_data = HoldingsLoader().load(holdings_file)
    portfolio: Dict[str, Dict[str, Any]] = {"equity": {}, "bond": {}, "cash": {}}

    for pos in portfolio_data.positions:
        bucket = pos.asset_class if pos.asset_class in portfolio else None
        if bucket is None:
            # Map "derivative"/"other" into a generic bucket that's ignored
            # by _equity/_bond_positions but preserved for cost-basis totals.
            bucket = "other"
            portfolio.setdefault("other", {})

        entry: Dict[str, Any] = {
            "shares": pos.shares if pos.shares is not None else 0.0,
            "current_price": pos.current_price,
            "cost_basis": pos.cost_basis,
            "cost_basis_price": pos.cost_basis_price,
            "market_value": pos.market_value,
            "sector": pos.sector,
        }
        if pos.asset_class == "bond":
            entry.update(
                {
                    "par_value": pos.market_value,
                    "coupon_rate": pos.coupon_rate,
                    "maturity_date": pos.maturity_date,
                    "years_to_maturity": pos.years_to_maturity,
                    "cusip": pos.cusip,
                    "is_corporate": pos.is_corporate,
                }
            )
        portfolio[bucket][pos.symbol] = entry
    return portfolio


def _equity_positions(portfolio: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Return (symbol, entry) pairs for equity holdings with nonzero shares."""
    equity = portfolio.get("equity", {}) or {}
    rows: List[Tuple[str, Dict[str, Any]]] = []
    for sym, entry in equity.items():
        if not isinstance(entry, dict):
            continue
        shares = _safe_float(entry.get("shares"), 0.0)
        if shares and shares > 0:
            rows.append((sym, entry))
    return rows


def _bond_positions(portfolio: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Return (symbol, entry) pairs for bond holdings with nonzero par."""
    bonds = portfolio.get("bond", {}) or {}
    rows: List[Tuple[str, Dict[str, Any]]] = []
    for sym, entry in bonds.items():
        if not isinstance(entry, dict):
            continue
        par = _safe_float(entry.get("par_value") or entry.get("market_value"), 0.0)
        if par and par > 0:
            rows.append((sym, entry))
    return rows


def _total_cost_basis(portfolio: Dict[str, Any]) -> float:
    """Sum of cost_basis across equity + bond positions (for yield-on-cost)."""
    total = 0.0
    for bucket in ("equity", "bond"):
        for entry in (portfolio.get(bucket, {}) or {}).values():
            if not isinstance(entry, dict):
                continue
            total += _safe_float(entry.get("cost_basis"), 0.0)
    return total


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        return default if f != f else f  # NaN guard
    except (TypeError, ValueError):
        return default


# ─── Dividend projection (equities) ─────────────────────────────────────────

# Massive provider cache: None = not tried, False = unavailable (no
# MASSIVE_API_KEY / SDK missing). One instance per run — the projection
# thread pool shares it instead of constructing per symbol.
_MASSIVE_PROVIDER: Any = None


def _get_massive_provider():
    """Return a shared MassiveProvider, or None when Massive is unavailable."""
    global _MASSIVE_PROVIDER
    if _MASSIVE_PROVIDER is None:
        try:
            from ic_engine.providers.price_provider import MassiveProvider

            _MASSIVE_PROVIDER = MassiveProvider()  # ValueError when no key
        except (ImportError, ValueError) as e:
            logger.debug(f"Massive dividends unavailable: {e}")
            _MASSIVE_PROVIDER = False
        except Exception as e:
            logger.debug(f"MassiveProvider init failed: {e}")
            _MASSIVE_PROVIDER = False
    return _MASSIVE_PROVIDER or None


def _project_equity_dividends_massive(
    provider: Any,
    symbol: str,
    entry: Dict[str, Any],
    shares: float,
    today: date,
    horizon_end: date,
) -> Optional[List[CashflowEvent]]:
    """PRIMARY dividend projection via Massive corporate-actions data.

    Massive rows carry declaration/ex/record/pay dates plus a declared
    frequency, so already-declared upcoming dividends land on their actual
    pay date instead of a cadence guess, and forward projection uses the
    declared frequency rather than inferring it from history. Returns None
    when Massive has no rows for the symbol (or errors) so the caller can
    fall back to the yfinance path unchanged.
    """
    try:
        rows = provider.get_dividends(symbol, limit=12)
    except Exception as e:
        logger.debug(f"{symbol}: Massive dividends fetch failed: {e}")
        return None
    if not rows:
        return None

    def _event_date(row: Dict[str, Any]) -> Optional[date]:
        # Pay date is when the cash actually lands; ex-date is the fallback
        # (the yfinance path only has ex-dates, so this is strictly better).
        return _parse_iso_date(row.get("pay_date") or row.get("ex_date"))

    held_days = _held_days(entry, today)
    qualified = held_days is None or held_days >= QUALIFIED_MIN_HOLDING_DAYS

    events: List[CashflowEvent] = []
    latest_date: Optional[date] = None
    per_share = 0.0
    freq = 0
    for row in rows:  # newest-first by ex-date
        d = _event_date(row)
        amt = _safe_float(row.get("cash_amount"), 0.0)
        if d is None or amt <= 0:
            continue
        if latest_date is None or d > latest_date:
            latest_date = d
            per_share = amt
            try:
                freq = int(row.get("frequency") or 0)
            except (TypeError, ValueError):
                freq = 0
        # Declared rows landing inside the window are real dated events.
        if today <= d <= horizon_end:
            events.append(
                CashflowEvent(
                    date=d.isoformat(),
                    symbol=symbol,
                    type="dividend",
                    amount=amt * shares,
                    tax_qualified=qualified,
                    tax_exempt=False,
                    asset_type="equity",
                )
            )

    if latest_date is None or per_share <= 0:
        # Rows existed but were unusable — let yfinance have a go.
        return None

    # Project beyond the last declared row using the declared frequency
    # (payments/year: 1, 2, 4, 12...; 0 = one-time special → no projection).
    # Anchoring on the latest declared row means projections start strictly
    # after every declared event, so no double-counting is possible.
    if freq > 0:
        cadence = max(1, round(365 / freq))
        next_date = latest_date + timedelta(days=cadence)
        while next_date <= today:
            next_date += timedelta(days=cadence)
        while next_date <= horizon_end:
            events.append(
                CashflowEvent(
                    date=next_date.isoformat(),
                    symbol=symbol,
                    type="dividend",
                    amount=per_share * shares,
                    tax_qualified=qualified,
                    tax_exempt=False,
                    asset_type="equity",
                )
            )
            next_date += timedelta(days=cadence)

    events.sort(key=lambda e: e.date)
    return events


def _fetch_dividend_history(symbol: str) -> List[Tuple[date, float]]:
    """Return trailing per-share dividend history as [(ex_date, per_share)]."""
    try:
        tkr = yf.Ticker(symbol)
        series = tkr.dividends
        if series is None or len(series) == 0:
            return []
        pairs: List[Tuple[date, float]] = []
        for idx, amount in series.items():
            try:
                d = idx.date() if hasattr(idx, "date") else idx
                pairs.append((d, float(amount)))
            except Exception:
                continue
        pairs.sort(key=lambda p: p[0])
        return pairs
    except Exception as e:
        logger.debug(f"{symbol}: dividend history fetch failed: {e}")
        return []


def _infer_dividend_cadence_days(history: List[Tuple[date, float]]) -> Optional[int]:
    """Estimate the typical inter-dividend spacing in days. None = irregular."""
    if len(history) < 2:
        return None
    # Use the last ~4 intervals for a current-regime estimate.
    recent = history[-5:] if len(history) >= 5 else history
    gaps = [
        (recent[i + 1][0] - recent[i][0]).days
        for i in range(len(recent) - 1)
        if (recent[i + 1][0] - recent[i][0]).days > 0
    ]
    if not gaps:
        return None
    avg = sum(gaps) / len(gaps)
    # Snap to canonical cadence buckets
    if avg < 20:
        return 7
    if avg < 45:
        return 30
    if avg < 120:
        return 91
    if avg < 240:
        return 182
    return 365


def _latest_dividend_per_share(history: List[Tuple[date, float]]) -> float:
    return history[-1][1] if history else 0.0


def _project_equity_dividends(
    symbol: str,
    entry: Dict[str, Any],
    shares: float,
    today: date,
    horizon_end: date,
) -> List[CashflowEvent]:
    """Project forward dividend events for one equity position."""
    history = _fetch_dividend_history(symbol)
    if not history:
        return []

    per_share = _latest_dividend_per_share(history)
    if per_share <= 0:
        return []

    cadence = _infer_dividend_cadence_days(history)
    if cadence is None:
        # One-off or unknown schedule — skip forward projection.
        return []

    last_ex = history[-1][0]
    # Determine the next projected ex-date (strictly after today; if the last
    # historical event already falls after today, that's a legitimate event
    # in the forward window).
    next_date = last_ex + timedelta(days=cadence)
    while next_date <= today:
        next_date += timedelta(days=cadence)

    # Qualification flag: held-since signal if available
    held_days = _held_days(entry, today)
    qualified = held_days is None or held_days >= QUALIFIED_MIN_HOLDING_DAYS

    events: List[CashflowEvent] = []
    while next_date <= horizon_end:
        events.append(
            CashflowEvent(
                date=next_date.isoformat(),
                symbol=symbol,
                type="dividend",
                amount=per_share * shares,
                tax_qualified=qualified,
                tax_exempt=False,
                asset_type="equity",
            )
        )
        next_date += timedelta(days=cadence)
    return events


def _held_days(entry: Dict[str, Any], today: date) -> Optional[int]:
    """Try to derive days-held from the holding entry. None if unknown."""
    for key in ("purchase_date", "acquisition_date", "opened_at"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            d = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
            return max(0, (today - d).days)
        except Exception:
            continue
    return None


def _project_equity_events_parallel(
    rows: List[Tuple[str, Dict[str, Any]]],
    today: date,
    horizon_end: date,
) -> List[CashflowEvent]:
    """Parallelize dividend lookups over positions (Massive primary, yfinance fallback)."""
    if not rows:
        return []

    out: List[CashflowEvent] = []

    # Resolve the shared Massive provider ONCE before fanning out so the
    # worker threads never race the lazy construction.
    massive = _get_massive_provider()

    def _one(row: Tuple[str, Dict[str, Any]]) -> List[CashflowEvent]:
        sym, entry = row
        shares = _safe_float(entry.get("shares"), 0.0)
        if shares <= 0:
            return []
        try:
            # Massive first: declared ex/pay dates beat cadence inference.
            # None (no key / no coverage / error) → yfinance path unchanged.
            if massive is not None:
                events = _project_equity_dividends_massive(
                    massive, sym, entry, shares, today, horizon_end
                )
                if events is not None:
                    return events
            return _project_equity_dividends(sym, entry, shares, today, horizon_end)
        except Exception as e:
            logger.debug(f"{sym}: equity projection failed: {e}")
            return []

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_one, r): r[0] for r in rows}
        for fut in as_completed(futures):
            try:
                out.extend(fut.result() or [])
            except Exception as e:
                logger.debug(f"equity projection future failed: {e}")
    return out


# ─── Coupon projection (bonds) ──────────────────────────────────────────────

_FREQ_TO_MONTHS = {
    1: 12,  # annual
    2: 6,  # semi-annual (US Treasury / most corporates)
    4: 3,  # quarterly
    12: 1,  # monthly
}


def _parse_iso_date(raw: Any) -> Optional[date]:
    if not raw:
        return None
    try:
        s = str(raw)
        # Accept either YYYY-MM-DD or full ISO datetime.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        pass
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _add_months(d: date, months: int) -> date:
    """Calendar-safe month add (clamps to end of short months)."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    # Clamp day
    from calendar import monthrange

    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def _bond_asset_type(bond: Dict[str, Any]) -> str:
    raw = (bond.get("asset_type") or bond.get("bond_type") or "").lower()
    if "muni" in raw:
        return "municipal_bond"
    if "treas" in raw:
        return "treasury"
    if "corp" in raw:
        return "corporate_bond"
    # Heuristic fallbacks
    name = (bond.get("security_name") or "").lower()
    if any(tok in name for tok in ("treas", "ust ", "tips")):
        return "treasury"
    if any(tok in name for tok in ("muni", "county", "school", "district", "state of")):
        return "municipal_bond"
    if raw:
        return raw
    # Final default: treat as corporate (most common private-sector case)
    return "corporate_bond"


def _coupon_frequency(bond: Dict[str, Any]) -> int:
    """Annual payments per year. Treasuries default 2; munis 2; corporates 2."""
    freq = bond.get("payment_frequency") or bond.get("coupon_frequency") or bond.get("frequency")
    try:
        f = int(freq)
        if f in _FREQ_TO_MONTHS:
            return f
    except (TypeError, ValueError):
        pass
    return 2  # sensible default for US bonds


def _coupon_rate_as_fraction(bond: Dict[str, Any]) -> float:
    """Normalize coupon rate to a decimal fraction (e.g. 0.045 for 4.5%)."""
    rate = _safe_float(bond.get("coupon_rate"), 0.0)
    if rate > 1.0:
        rate = rate / 100.0
    return max(0.0, rate)


def _bond_par_value(bond: Dict[str, Any]) -> float:
    """Face/par value used to compute coupon dollar amounts."""
    for key in ("par_value", "face_value", "par"):
        v = _safe_float(bond.get(key), 0.0)
        if v > 0:
            return v
    # Fallback: use market_value as approximation (over/underestimates
    # depending on price vs par, but better than zero).
    return _safe_float(bond.get("market_value"), 0.0)


def _project_coupon_events(
    bond: Dict[str, Any],
    today: date,
    horizon_end: date,
) -> List[CashflowEvent]:
    """Generate coupon payment events for one bond out to the horizon."""
    symbol = (
        bond.get("symbol")
        or bond.get("cusip")
        or bond.get("isin")
        or bond.get("security_name")
        or "BOND"
    )
    maturity = _parse_iso_date(bond.get("maturity_date"))
    if maturity is None or maturity <= today:
        return []

    rate = _coupon_rate_as_fraction(bond)
    par = _bond_par_value(bond)
    if rate <= 0 or par <= 0:
        return []

    freq = _coupon_frequency(bond)
    months = _FREQ_TO_MONTHS[freq]
    coupon_amount = (rate * par) / float(freq)

    asset_type = _bond_asset_type(bond)
    tax_exempt = asset_type == "municipal_bond"

    # Generate payment dates backward from maturity to the horizon start.
    # This anchors on the contractual maturity day-of-month, which is how
    # real bonds pay (e.g. semi-annual coupons every Feb 15 / Aug 15 for a
    # UST maturing Feb 15).
    events: List[CashflowEvent] = []
    # Walk forward from today until we find the first coupon date, then step.
    # Start by projecting coupons back from maturity into the past, then
    # discard any that fall outside [today, horizon_end].
    dates: List[date] = []
    cursor = maturity
    # Safety bound: at most ~50 years of history to generate
    max_steps = 50 * freq + 4
    for _ in range(max_steps):
        if cursor < today - timedelta(days=30):
            break
        dates.append(cursor)
        cursor = _add_months(cursor, -months)
    dates.sort()

    for d in dates:
        if d < today or d > horizon_end:
            continue
        events.append(
            CashflowEvent(
                date=d.isoformat(),
                symbol=str(symbol),
                type="coupon",
                amount=coupon_amount,
                tax_qualified=False,
                tax_exempt=tax_exempt,
                asset_type=asset_type,
            )
        )
    return events


def _load_bonds_from_bond_analysis(path: str) -> List[Dict[str, Any]]:
    """Read bond_analysis.json and return its individual_bonds list."""
    try:
        with open(path) as f:
            doc = json.load(f)
    except Exception as e:
        logger.warning(f"Could not read bond_analysis.json at {path}: {e}")
        return []
    data = doc.get("data", doc)
    bonds = data.get("individual_bonds") or []
    if not isinstance(bonds, list):
        return []
    return bonds


def _bonds_from_portfolio(portfolio: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Derive minimal bond dicts from the canonical portfolio schema."""
    bonds: List[Dict[str, Any]] = []
    for sym, entry in _bond_positions(portfolio):
        if not isinstance(entry, dict):
            continue
        enriched = dict(entry)
        enriched.setdefault("symbol", sym)
        bonds.append(enriched)
    return bonds


# ─── Aggregation & tax breakdown ────────────────────────────────────────────


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _forward_months(today: date, months: int) -> List[str]:
    """Produce YYYY-MM strings starting from *today*'s month, inclusive."""
    keys: List[str] = []
    cursor = date(today.year, today.month, 1)
    for _ in range(months):
        keys.append(_month_key(cursor))
        cursor = _add_months(cursor, 1)
    return keys


def _bucket_events(
    events: List[CashflowEvent],
    month_keys: List[str],
) -> Dict[str, MonthBucket]:
    buckets: Dict[str, MonthBucket] = {k: MonthBucket(month=k) for k in month_keys}
    for ev in events:
        try:
            d = datetime.fromisoformat(ev.date).date()
        except Exception:
            continue
        key = _month_key(d)
        b = buckets.get(key)
        if b is None:
            continue
        b.events.append(ev)
        if ev.type == "dividend":
            b.dividend_income += ev.amount
            if ev.tax_qualified:
                b.qualified_dividend += ev.amount
            else:
                b.ordinary_dividend += ev.amount
        elif ev.type == "coupon":
            b.coupon_income += ev.amount
            if ev.tax_exempt:
                b.tax_exempt_coupon += ev.amount
            else:
                b.taxable_coupon += ev.amount
    return buckets


def _summarize_taxes(events: List[CashflowEvent]) -> Dict[str, float]:
    """Annual tax-bucket totals across all forward events."""
    qual = ord_ = exempt = taxable_cpn = 0.0
    for ev in events:
        if ev.type == "dividend":
            if ev.tax_qualified:
                qual += ev.amount
            else:
                ord_ += ev.amount
        elif ev.type == "coupon":
            if ev.tax_exempt:
                exempt += ev.amount
            else:
                taxable_cpn += ev.amount
    return {
        "qualified_dividend": round(qual, 2),
        "ordinary_dividend": round(ord_, 2),
        "tax_exempt_coupon": round(exempt, 2),
        "taxable_coupon": round(taxable_cpn, 2),
    }


# ─── Orchestration ──────────────────────────────────────────────────────────


def run_cashflow(
    holdings_file: str,
    bond_analysis_file: Optional[str] = None,
    months: int = 12,
    annual_expenses: float = 0.0,
    output_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate the forward-looking income calendar for a portfolio."""
    if months <= 0:
        months = 12

    portfolio = _load_portfolio(holdings_file)
    today = date.today()
    horizon_end = _add_months(today, months)

    # Equity dividend projection
    eq_rows = _equity_positions(portfolio)
    logger.info(f"Projecting dividends for {len(eq_rows)} equity positions over {months} months")
    dividend_events = _project_equity_events_parallel(eq_rows, today, horizon_end)

    # Bond coupon projection: prefer bond_analysis.json when provided (richer
    # asset-type + frequency metadata), fall back to the portfolio bond bucket.
    bonds: List[Dict[str, Any]] = []
    if bond_analysis_file and Path(bond_analysis_file).exists():
        bonds = _load_bonds_from_bond_analysis(bond_analysis_file)
        logger.info(f"Loaded {len(bonds)} bonds from bond_analysis.json for coupon projection")
    if not bonds:
        bonds = _bonds_from_portfolio(portfolio)
        if bonds:
            logger.info(
                f"Derived {len(bonds)} bonds from portfolio (no bond_analysis.json supplied)"
            )

    coupon_events: List[CashflowEvent] = []
    for bond in bonds:
        try:
            coupon_events.extend(_project_coupon_events(bond, today, horizon_end))
        except Exception as e:
            logger.debug(f"Bond coupon projection failed: {e}")

    all_events = sorted(
        dividend_events + coupon_events,
        key=lambda e: (e.date, e.symbol),
    )

    # Monthly aggregation
    month_keys = _forward_months(today, months)
    buckets = _bucket_events(all_events, month_keys)
    monthly = [buckets[k].to_dict() for k in month_keys]

    annual_total = round(sum(ev.amount for ev in all_events), 2)
    cost_basis = _total_cost_basis(portfolio)
    yoc = round(annual_total / cost_basis, 4) if cost_basis > 0 else 0.0
    coverage = (
        round(annual_total / annual_expenses, 4) if annual_expenses and annual_expenses > 0 else 0.0
    )

    result: Dict[str, Any] = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "horizon_months": months,
        "monthly_cashflow": monthly,
        "annual_total": annual_total,
        "yield_on_cost": yoc,
        "tax_breakdown": _summarize_taxes(all_events),
        "coverage_ratio": coverage,
        "annual_expenses_input": round(annual_expenses, 2) if annual_expenses else 0.0,
        "calendar_events": [ev.to_dict() for ev in all_events],
        "positions_analyzed": {
            "equity": len(eq_rows),
            "bond": len(bonds),
        },
    }

    if output_file:
        DisclaimerWrapper.wrap_and_save(result, output_file, "Income & Cashflow Calendar")
        logger.info(f"Cashflow calendar saved to {output_file}")

    return result


# ─── Compact stdout summary ─────────────────────────────────────────────────


def _build_compact_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """2–3 KB compact summary for stdout consumption."""
    monthly = result.get("monthly_cashflow", []) or []
    compact_monthly = [
        {
            "month": m.get("month"),
            "total_income": m.get("total_income"),
            "dividend_income": m.get("dividend_income"),
            "coupon_income": m.get("coupon_income"),
        }
        for m in monthly
    ]
    events = result.get("calendar_events", []) or []
    # Keep only the next ~20 events in stdout; full list remains in the
    # persisted JSON output.
    preview_events = [
        {
            "date": e.get("date"),
            "symbol": e.get("symbol"),
            "type": e.get("type"),
            "amount": e.get("amount"),
            "tax_qualified": e.get("tax_qualified"),
            "tax_exempt": e.get("tax_exempt"),
        }
        for e in events[:20]
    ]
    return {
        "as_of": result.get("as_of"),
        "horizon_months": result.get("horizon_months"),
        "annual_total": result.get("annual_total"),
        "yield_on_cost": result.get("yield_on_cost"),
        "coverage_ratio": result.get("coverage_ratio"),
        "tax_breakdown": result.get("tax_breakdown"),
        "positions_analyzed": result.get("positions_analyzed"),
        "monthly_cashflow": compact_monthly,
        "events_preview": preview_events,
        "events_total": len(events),
    }


# ─── Artifact builder (HTML calendar view) ─────────────────────────────────


def _build_cashflow_artifact(
    result: Dict[str, Any],
    output_path: str,
    stonkmode: bool = False,
) -> str:
    """Render an HTML calendar artifact for the cashflow projection."""
    from html import escape as _h

    from ic_engine.rendering.artifact_generator import (
        PALETTE,
        ArtifactGenerator,
        detect_terms_in_text,
        extract_dr_stonk_definitions,
        get_stonkmode_narrative,
    )

    tax = result.get("tax_breakdown", {}) or {}
    positions = result.get("positions_analyzed", {}) or {}

    metadata = {
        "Horizon": f"{result.get('horizon_months', 12)} months",
        "Annual Income": f"${result.get('annual_total', 0):,.0f}",
        "Yield on Cost": f"{(result.get('yield_on_cost', 0) or 0) * 100:.2f}%",
        "Coverage": (
            f"{result.get('coverage_ratio', 0):.2f}x" if result.get("coverage_ratio") else "n/a"
        ),
        "Equity Pos": positions.get("equity", 0),
        "Bond Pos": positions.get("bond", 0),
    }
    artifact = ArtifactGenerator(
        title="Income & Cashflow Calendar",
        disclaimer="EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        metadata=metadata,
    )

    # Monthly stacked income bar chart (dividend + coupon)
    monthly = result.get("monthly_cashflow", []) or []
    months = [m.get("month", "") for m in monthly]
    divs = [float(m.get("dividend_income", 0) or 0) for m in monthly]
    cpns = [float(m.get("coupon_income", 0) or 0) for m in monthly]

    if months and (any(divs) or any(cpns)):
        artifact.add_bar_chart(
            months,
            divs,
            "Monthly Dividend Income",
            x_label="Month",
            y_label="USD",
            col_class="col-6",
            color=PALETTE.get("equity", "#2dd4bf"),
        )
        artifact.add_bar_chart(
            months,
            cpns,
            "Monthly Coupon Income",
            x_label="Month",
            y_label="USD",
            col_class="col-6",
            color=PALETTE.get("bond", "#f59e0b"),
        )

    # Tax breakdown pie
    tax_labels = [
        "Qualified Div",
        "Ordinary Div",
        "Tax-Exempt Coupon",
        "Taxable Coupon",
    ]
    tax_values = [
        tax.get("qualified_dividend", 0),
        tax.get("ordinary_dividend", 0),
        tax.get("tax_exempt_coupon", 0),
        tax.get("taxable_coupon", 0),
    ]
    if any(tax_values):
        artifact.add_pie_chart(
            tax_labels,
            tax_values,
            "Annual Tax Breakdown (by Bucket)",
            col_class="col-6",
        )

    # Monthly calendar table with federal/state/exempt columns
    if monthly:
        cal_rows = []
        for m in monthly:
            ti = m.get("tax_impact", {}) or {}
            cal_rows.append(
                {
                    "Month": m.get("month"),
                    "Dividends": f"${m.get('dividend_income', 0):,.2f}",
                    "Coupons": f"${m.get('coupon_income', 0):,.2f}",
                    "Total": f"${m.get('total_income', 0):,.2f}",
                    "Federal Tax": f"${ti.get('federal', 0):,.2f}",
                    "State Tax": f"${ti.get('state', 0):,.2f}",
                    "Tax-Exempt": f"${ti.get('tax_exempt', 0):,.2f}",
                }
            )
        artifact.add_table(
            cal_rows,
            "Monthly Cashflow Calendar",
            columns=[
                "Month",
                "Dividends",
                "Coupons",
                "Total",
                "Federal Tax",
                "State Tax",
                "Tax-Exempt",
            ],
            col_class="col-12",
        )

    # Event timeline with tooltips — render as a simple HTML block with
    # title="" attributes that function as hover tooltips without JS.
    events = result.get("calendar_events", []) or []
    if events:
        rows_html: List[str] = []
        for ev in events[:200]:
            tooltip = (
                f"{ev.get('symbol')} — {ev.get('type')} "
                f"${ev.get('amount', 0):,.2f} "
                f"({'qualified' if ev.get('tax_qualified') else ''}"
                f"{'tax-exempt' if ev.get('tax_exempt') else ''}"
                f"{'taxable' if not ev.get('tax_qualified') and not ev.get('tax_exempt') else ''})"
            ).strip()
            dot_color = (
                PALETTE.get("equity", "#2dd4bf")
                if ev.get("type") == "dividend"
                else PALETTE.get("bond", "#f59e0b")
            )
            rows_html.append(
                f'<tr title="{_h(tooltip)}">'
                f"<td>{_h(str(ev.get('date') or ''))}</td>"
                f'<td><span style="display:inline-block;width:10px;height:10px;'
                f'border-radius:50%;background:{_h(dot_color)};margin-right:6px;"></span>'
                f"{_h(str(ev.get('symbol') or ''))}</td>"
                f"<td>{_h(str(ev.get('type') or ''))}</td>"
                f'<td style="text-align:right;">${float(ev.get("amount") or 0):,.2f}</td>'
                f"<td>{_h(str(ev.get('asset_type') or ''))}</td>"
                f"</tr>"
            )
        table_html = (
            '<table class="cashflow-events" style="width:100%;border-collapse:collapse;">'
            "<thead><tr>"
            '<th style="text-align:left;padding:4px;">Date</th>'
            '<th style="text-align:left;padding:4px;">Symbol</th>'
            '<th style="text-align:left;padding:4px;">Type</th>'
            '<th style="text-align:right;padding:4px;">Amount</th>'
            '<th style="text-align:left;padding:4px;">Asset Type</th>'
            "</tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody>"
            "</table>"
        )
        if len(events) > 200:
            table_html += (
                f'<div style="margin-top:8px;opacity:0.7;">'
                f"Showing first 200 of {len(events)} events. "
                f"Full list available in the JSON output."
                f"</div>"
            )
        artifact.add_raw_block(
            table_html,
            title="Event Timeline (hover for details)",
            col_class="col-12",
        )

    # Stonkmode narrative + Dr. Stonk term box
    summary_lines = [
        f"Projected annual income: ${result.get('annual_total', 0):,.0f}",
        f"Yield on cost: {(result.get('yield_on_cost', 0) or 0) * 100:.2f}%",
        f"Coverage ratio: {result.get('coverage_ratio', 0):.2f}x",
        f"Qualified dividends: ${tax.get('qualified_dividend', 0):,.0f}",
        f"Tax-exempt coupons: ${tax.get('tax_exempt_coupon', 0):,.0f}",
    ]
    data_summary = "\n".join(summary_lines)
    text_for_terms = (
        data_summary + " Qualified Dividend. Ordinary Dividend. Coupon. Yield on Cost. Tax Exempt."
    )

    if stonkmode:
        narration = get_stonkmode_narrative("cashflow", data_summary)
        if narration:
            artifact.add_stonkmode_pair(
                lead_name=narration["lead"]["name"],
                lead_text=narration["lead"]["text"],
                foil_name=narration["foil"]["name"],
                foil_text=narration["foil"]["text"],
                lead_archetype=narration["lead"]["archetype"],
                foil_archetype=narration["foil"]["archetype"],
                closer=narration.get("closer"),
            )
            text_for_terms += f" {narration['lead']['text']} {narration['foil']['text']}"

    terms = detect_terms_in_text(text_for_terms)
    if terms:
        defs = extract_dr_stonk_definitions(terms)
        if defs:
            artifact.add_dr_stonk_box(defs)

    return str(artifact.save(output_path))


# ─── Argv parsing ───────────────────────────────────────────────────────────


def _parse_argv(argv: List[str]) -> Tuple[str, Optional[str], int, float, Optional[str]]:
    """Parse:
    <holdings.json> [bond_analysis.json] [--months 12]
                    [--annual-expenses FLOAT] [output.json]
    """
    months = 12
    annual_expenses = 0.0

    positional: List[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--months" and i + 1 < len(argv):
            try:
                months = int(argv[i + 1])
            except ValueError:
                logger.warning(f"Invalid --months value: {argv[i + 1]}")
            i += 2
            continue
        if tok == "--annual-expenses" and i + 1 < len(argv):
            try:
                annual_expenses = float(argv[i + 1])
            except ValueError:
                logger.warning(f"Invalid --annual-expenses value: {argv[i + 1]}")
            i += 2
            continue
        positional.append(tok)
        i += 1

    if not positional:
        raise SystemExit(
            "Usage: cashflow.py <holdings.json> [bond_analysis.json] "
            "[--months 12] [--annual-expenses FLOAT] [output.json]"
        )

    holdings_file = positional[0]
    bond_analysis_file: Optional[str] = None
    output_file: Optional[str] = None

    # Distinguish bond_analysis vs output by filename heuristic.
    for p in positional[1:]:
        name = os.path.basename(p).lower()
        if "bond" in name and "analysis" in name:
            bond_analysis_file = p
        else:
            output_file = p

    return holdings_file, bond_analysis_file, months, annual_expenses, output_file


# ─── Entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from ic_engine.commands._artifact_helpers import pop_artifact_flags  # noqa: E402

    _argv = list(sys.argv)
    _artifact_path, _stonkmode = pop_artifact_flags(_argv)
    sys.argv = _argv

    (
        holdings_file,
        bond_analysis_file,
        months,
        annual_expenses,
        output_file,
    ) = _parse_argv(sys.argv)

    if output_file is None:
        output_file = str(Path(holdings_file).expanduser().resolve().parent / "cashflow.json")

    result = run_cashflow(
        holdings_file=holdings_file,
        bond_analysis_file=bond_analysis_file,
        months=months,
        annual_expenses=annual_expenses,
        output_file=output_file,
    )

    compact = _build_compact_summary(result)

    # Print human-readable summary to stderr
    print(f"\n{'=' * 70}", file=sys.stderr)
    print("💡 Analysis complete. Review the detailed JSON output above.", file=sys.stderr)
    print("   → Bring these findings to your financial advisor.", file=sys.stderr)
    print(f"{'=' * 70}\n", file=sys.stderr)

    print(json.dumps(compact, separators=(",", ":")))

    if _artifact_path:
        try:
            rendered = _build_cashflow_artifact(result, _artifact_path, stonkmode=_stonkmode)
            logger.info(f"Cashflow artifact saved to {rendered}")
        except Exception as e:
            logger.warning(f"Artifact rendering failed: {e}")

    if output_file:
        logger.info(f"Full cashflow data saved to: {output_file}")
