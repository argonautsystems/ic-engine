"""Massive extended-surface mixin for MassiveProvider.

Wraps the Massive REST surfaces beyond stocks/futures/forex quotes that the
polygon-api-client SDK does not cover (or covers awkwardly):

  - Benzinga partner feed: analyst ratings, consensus ratings, earnings,
    analyst/firm directories
  - Fundamentals: income statements, financial ratios, short interest
  - Corporate actions: dividends, splits
  - Filings: SEC Form 4 (insider transactions)
  - Forex: currency conversion (server-side)
  - Economy: treasury yields, inflation
  - Technical indicators: SMA / EMA / RSI / MACD
  - Snapshots: top market movers, single-index, single-crypto
  - Reference: ticker overview, related companies

Entitlement awareness: some surfaces are plan-gated (e.g. Benzinga guidance,
bulls/bears, ETF Global returned 403 on the 2026-06-06 probe). A 403 marks
the endpoint family as not-entitled for the process lifetime and the method
returns None thereafter without re-hitting the API.

All methods return plain dicts/lists normalized for ic-engine consumption,
or None/[] on any failure — callers must treat Massive as best-effort and
keep their existing fallbacks.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class MassiveSurfaceMixin:
    """Extended Massive REST surface. Mixed into MassiveProvider.

    Expects the host class to provide ``self.api_key`` and ``API_BASE``.
    """

    SURFACE_TIMEOUT = 15

    # Endpoint families confirmed 403 (not entitled) get parked here at
    # runtime so we never hammer a gated surface. MassiveProvider.__init__
    # initializes this eagerly (thread-safe); the hasattr guard in
    # _surface_get is belt-and-suspenders for any other host class.
    _not_entitled: set

    def _surface_get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET an api.massive.com path; None on any failure.

        403 → family marked not-entitled for the process lifetime.
        """
        if not hasattr(self, "_not_entitled"):
            self._not_entitled = set()
        family = "/".join(path.strip("/").split("/")[:2])
        if family in self._not_entitled:
            return None
        url = f"{self.API_BASE}{path}"
        q = dict(params or {})
        q["apiKey"] = self.api_key
        try:
            resp = requests.get(url, params=q, timeout=self.SURFACE_TIMEOUT)
            if resp.status_code == 403:
                logger.info("Massive surface %s not entitled on this plan; disabling", family)
                self._not_entitled.add(family)
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("Massive surface GET %s: %s", path, e)
            return None

    @staticmethod
    def _results(data: Optional[dict]) -> List[dict]:
        """Extract the results list from a standard Massive envelope."""
        if not isinstance(data, dict):
            return []
        r = data.get("results")
        if isinstance(r, list):
            return r
        if isinstance(r, dict):
            return [r]
        return []

    # ── Benzinga partner feed ────────────────────────────────────────────

    def get_benzinga_ratings(self, symbol: str, limit: int = 20) -> List[Dict]:
        """Recent per-analyst rating actions for a ticker."""
        data = self._surface_get(
            "/benzinga/v1/ratings", {"ticker": symbol.upper(), "limit": limit, "sort": "date.desc"}
        )
        out = []
        for r in self._results(data):
            out.append(
                {
                    "symbol": symbol.upper(),
                    "date": r.get("date") or r.get("last_updated"),
                    "firm": r.get("firm") or r.get("firm_name"),
                    "analyst": r.get("analyst") or r.get("analyst_name"),
                    "action": r.get("rating_action") or r.get("action_company"),
                    "rating": r.get("rating_current"),
                    "rating_prior": r.get("rating_prior"),
                    "price_target": r.get("price_target_current") or r.get("pt_current"),
                    "price_target_prior": r.get("price_target_prior") or r.get("pt_prior"),
                }
            )
        return out

    def get_consensus_ratings(self, symbol: str, window_days: int = 365) -> Optional[Dict]:
        """Benzinga consensus rating for a ticker over a recency window.

        IMPORTANT: without a date filter the endpoint aggregates ALL rating
        history (decades — AAPL came back with a $3.57 low target), so we
        always constrain to the trailing ``window_days``.
        """
        from datetime import date, timedelta

        since = (date.today() - timedelta(days=window_days)).isoformat()
        data = self._surface_get(
            f"/benzinga/v1/consensus-ratings/{symbol.upper()}", {"date.gte": since}
        )
        rows = self._results(data)
        if not rows:
            return None
        r = rows[0]
        strong_buy = r.get("strong_buy_ratings") or 0
        strong_sell = r.get("strong_sell_ratings") or 0
        return {
            "symbol": symbol.upper(),
            "window_days": window_days,
            "consensus_rating": r.get("consensus_rating"),
            "consensus_rating_value": r.get("consensus_rating_value"),
            "consensus_price_target": r.get("consensus_price_target"),
            "high_price_target": r.get("high_price_target"),
            "low_price_target": r.get("low_price_target"),
            "strong_buy": strong_buy,
            "buy": r.get("buy_ratings"),
            "hold": r.get("hold_ratings"),
            "sell": r.get("sell_ratings"),
            "strong_sell": strong_sell,
            "total_analysts": r.get("ratings_contributors"),
        }

    def get_benzinga_earnings(self, symbol: str, limit: int = 8) -> List[Dict]:
        """Recent + upcoming earnings (EPS/revenue actual vs estimate)."""
        data = self._surface_get(
            "/benzinga/v1/earnings", {"ticker": symbol.upper(), "limit": limit, "sort": "date.desc"}
        )
        out = []
        for r in self._results(data):
            out.append(
                {
                    "symbol": symbol.upper(),
                    "date": r.get("date"),
                    "fiscal_period": r.get("fiscal_period") or r.get("period"),
                    "eps_actual": r.get("eps_actual") or r.get("eps"),
                    "eps_estimate": r.get("eps_estimate") or r.get("eps_est"),
                    "eps_surprise_pct": r.get("eps_surprise_percent"),
                    "revenue_actual": r.get("revenue_actual") or r.get("revenue"),
                    "revenue_estimate": r.get("revenue_estimate") or r.get("revenue_est"),
                }
            )
        return out

    # ── Fundamentals ─────────────────────────────────────────────────────

    def get_income_statements(self, symbol: str, limit: int = 4) -> List[Dict]:
        """Recent income statements (annual/quarterly mix as served)."""
        data = self._surface_get(
            "/stocks/financials/v1/income-statements",
            {"tickers": symbol.upper(), "limit": limit, "sort": "period_end_date.desc"},
        )
        out = []
        for r in self._results(data):
            out.append(
                {
                    "symbol": symbol.upper(),
                    "period_end": r.get("period_end_date") or r.get("end_date"),
                    "timeframe": r.get("timeframe") or r.get("fiscal_period"),
                    "revenue": r.get("revenues") or r.get("revenue"),
                    "gross_profit": r.get("gross_profit"),
                    "operating_income": r.get("operating_income_loss") or r.get("operating_income"),
                    "net_income": r.get("net_income_loss") or r.get("net_income"),
                    "eps_diluted": r.get("diluted_earnings_per_share") or r.get("eps_diluted"),
                }
            )
        return out

    def get_financial_ratios(self, symbol: str) -> Optional[Dict]:
        """Latest financial ratios snapshot (valuation/leverage/profitability)."""
        data = self._surface_get(
            "/stocks/financials/v1/ratios", {"ticker": symbol.upper(), "limit": 1}
        )
        rows = self._results(data)
        if not rows:
            return None
        r = rows[0]
        keep = (
            "price_to_earnings", "price_to_book", "price_to_sales",
            "debt_to_equity", "current", "quick", "return_on_equity",
            "return_on_assets", "gross_margin", "operating_margin", "net_margin",
            "dividend_yield", "market_cap", "enterprise_value",
        )
        out = {"symbol": symbol.upper(), "date": r.get("date") or r.get("period_end_date")}
        for k in keep:
            if r.get(k) is not None:
                out[k] = r[k]
        return out

    def get_short_interest(self, symbol: str) -> Optional[Dict]:
        """Latest short interest for a ticker."""
        data = self._surface_get(
            "/stocks/v1/short-interest",
            {"ticker": symbol.upper(), "limit": 1, "sort": "settlement_date.desc"},
        )
        rows = self._results(data)
        if not rows:
            return None
        r = rows[0]
        return {
            "symbol": symbol.upper(),
            "settlement_date": r.get("settlement_date"),
            "short_interest": r.get("short_interest"),
            "avg_daily_volume": r.get("avg_daily_volume"),
            "days_to_cover": r.get("days_to_cover"),
        }

    # ── Corporate actions ────────────────────────────────────────────────

    def get_dividends(self, symbol: str, limit: int = 12) -> List[Dict]:
        """Dividend history + declared upcoming (ex-date ordered, newest first)."""
        data = self._surface_get(
            "/v3/reference/dividends",
            {"ticker": symbol.upper(), "limit": limit, "sort": "ex_dividend_date", "order": "desc"},
        )
        out = []
        for r in self._results(data):
            out.append(
                {
                    "symbol": symbol.upper(),
                    "ex_date": r.get("ex_dividend_date"),
                    "pay_date": r.get("pay_date"),
                    "record_date": r.get("record_date"),
                    "declaration_date": r.get("declaration_date"),
                    "cash_amount": r.get("cash_amount"),
                    "frequency": r.get("frequency"),
                    "dividend_type": r.get("dividend_type"),
                }
            )
        return out

    def get_splits(self, symbol: str, limit: int = 10) -> List[Dict]:
        """Stock split history (newest first)."""
        data = self._surface_get(
            "/v3/reference/splits",
            {"ticker": symbol.upper(), "limit": limit, "sort": "execution_date", "order": "desc"},
        )
        out = []
        for r in self._results(data):
            out.append(
                {
                    "symbol": symbol.upper(),
                    "execution_date": r.get("execution_date"),
                    "split_from": r.get("split_from"),
                    "split_to": r.get("split_to"),
                }
            )
        return out

    # ── Filings (insider activity) ───────────────────────────────────────

    def get_form4_filings(self, symbol: str, limit: int = 15) -> List[Dict]:
        """Recent SEC Form 4 insider transactions for a ticker."""
        data = self._surface_get(
            "/stocks/filings/vX/form-4",
            {"tickers": symbol.upper(), "limit": limit, "sort": "filing_date.desc"},
        )
        out = []
        for r in self._results(data):
            out.append(
                {
                    "symbol": symbol.upper(),
                    "filing_date": r.get("filing_date"),
                    "owner": r.get("owner_name") or r.get("reporting_owner"),
                    "officer_title": r.get("officer_title"),
                    "transaction_code": r.get("transaction_code"),
                    "shares": r.get("transaction_shares") or r.get("shares"),
                    "price": r.get("transaction_price") or r.get("price_per_share"),
                    "shares_owned_after": r.get("shares_owned_following_transaction"),
                }
            )
        return out

    # ── Forex: server-side currency conversion ───────────────────────────

    def convert_currency(
        self, amount: float, from_ccy: str, to_ccy: str = "USD"
    ) -> Optional[Dict]:
        """Convert an amount between currencies at the live Massive rate.

        Returns {converted, rate, from, to} or None.
        """
        from_ccy = from_ccy.upper()
        to_ccy = to_ccy.upper()
        if from_ccy == to_ccy:
            return {"converted": amount, "rate": 1.0, "from": from_ccy, "to": to_ccy}
        data = self._surface_get(
            f"/v1/conversion/{from_ccy}/{to_ccy}", {"amount": amount, "precision": 4}
        )
        if not isinstance(data, dict) or data.get("converted") is None:
            return None
        rate = None
        last = data.get("last")
        if isinstance(last, dict):
            rate = last.get("ask") or last.get("bid")
        return {
            "converted": data.get("converted"),
            "rate": rate,
            "from": from_ccy,
            "to": to_ccy,
        }

    # ── Economy ──────────────────────────────────────────────────────────

    def get_treasury_yields(self, limit: int = 1) -> List[Dict]:
        """Latest treasury yield curve rows (1m..30y per row)."""
        data = self._surface_get("/fed/v1/treasury-yields", {"limit": limit, "sort": "date.desc"})
        return self._results(data)

    def get_inflation(self, limit: int = 1) -> List[Dict]:
        """Latest CPI/inflation rows."""
        data = self._surface_get("/fed/v1/inflation", {"limit": limit, "sort": "date.desc"})
        return self._results(data)

    # ── Technical indicators ─────────────────────────────────────────────

    _INDICATORS = ("sma", "ema", "rsi", "macd")

    def get_indicator(
        self,
        symbol: str,
        indicator: str = "rsi",
        window: int = 14,
        timespan: str = "day",
        limit: int = 1,
    ) -> List[Dict]:
        """Server-side technical indicator values (newest first).

        Returns [{timestamp, value, ...}]; MACD rows also carry
        signal/histogram.
        """
        ind = indicator.lower()
        if ind not in self._INDICATORS:
            raise ValueError(f"indicator must be one of {self._INDICATORS}")
        params = {"timespan": timespan, "order": "desc", "limit": limit}
        if ind != "macd":
            params["window"] = window
        data = self._surface_get(f"/v1/indicators/{ind}/{symbol.upper()}", params)
        if not isinstance(data, dict):
            return []
        values = (data.get("results") or {}).get("values") or []
        return values if isinstance(values, list) else []

    # ── Snapshots / reference ────────────────────────────────────────────

    def get_market_movers(self, direction: str = "gainers", top: int = 10) -> List[Dict]:
        """Top US-equity gainers or losers right now."""
        if direction not in ("gainers", "losers"):
            raise ValueError("direction must be 'gainers' or 'losers'")
        data = self._surface_get(f"/v2/snapshot/locale/us/markets/stocks/{direction}")
        out = []
        for t in (data or {}).get("tickers", [])[:top]:
            day = t.get("day") or {}
            out.append(
                {
                    "symbol": t.get("ticker"),
                    "price": day.get("c") or t.get("lastTrade", {}).get("p"),
                    "change_pct": t.get("todaysChangePerc"),
                    "volume": day.get("v"),
                }
            )
        return out

    def get_ticker_overview(self, symbol: str) -> Optional[Dict]:
        """Reference profile: name, market cap, sector (SIC), employees, etc."""
        data = self._surface_get(f"/v3/reference/tickers/{symbol.upper()}")
        rows = self._results(data)
        if not rows:
            return None
        r = rows[0]
        return {
            "symbol": r.get("ticker"),
            "name": r.get("name"),
            "market_cap": r.get("market_cap"),
            "sic_description": r.get("sic_description"),
            "total_employees": r.get("total_employees"),
            "list_date": r.get("list_date"),
            "homepage_url": r.get("homepage_url"),
            "description": (r.get("description") or "")[:500] or None,
        }

    def get_related_tickers(self, symbol: str) -> List[str]:
        """Tickers Massive considers related (peers/comparables)."""
        data = self._surface_get(f"/v1/related-companies/{symbol.upper()}")
        return [r.get("ticker") for r in self._results(data) if r.get("ticker")]

    def get_options_chain_snapshot(
        self, underlying: str, contract_type: Optional[str] = None, limit: int = 50
    ) -> List[Dict]:
        """Option chain snapshot rows for an underlying."""
        params: dict = {"limit": limit}
        if contract_type:
            params["contract_type"] = contract_type
        data = self._surface_get(f"/v3/snapshot/options/{underlying.upper()}", params)
        out = []
        for r in self._results(data):
            details = r.get("details") or {}
            day = r.get("day") or {}
            out.append(
                {
                    "occ_ticker": details.get("ticker"),
                    "underlying": underlying.upper(),
                    "contract_type": details.get("contract_type"),
                    "strike": details.get("strike_price"),
                    "expiration": details.get("expiration_date"),
                    "price": day.get("close") or (r.get("last_trade") or {}).get("price"),
                    "open_interest": r.get("open_interest"),
                    "implied_volatility": r.get("implied_volatility"),
                    "delta": (r.get("greeks") or {}).get("delta"),
                }
            )
        return out

    def get_option_contract_snapshot(self, underlying: str, occ_ticker: str) -> Optional[Dict]:
        """Snapshot for one OCC option contract (O:AAPL251219C00300000)."""
        data = self._surface_get(
            f"/v3/snapshot/options/{underlying.upper()}/{occ_ticker}"
        )
        rows = self._results(data)
        if not rows:
            return None
        r = rows[0]
        details = r.get("details") or {}
        day = r.get("day") or {}
        return {
            "occ_ticker": details.get("ticker") or occ_ticker,
            "underlying": underlying.upper(),
            "contract_type": details.get("contract_type"),
            "strike": details.get("strike_price"),
            "expiration": details.get("expiration_date"),
            "price": day.get("close") or (r.get("last_trade") or {}).get("price"),
            "prev_close": day.get("previous_close"),
            "open_interest": r.get("open_interest"),
            "implied_volatility": r.get("implied_volatility"),
            "delta": (r.get("greeks") or {}).get("delta"),
            "break_even": r.get("break_even_price"),
        }

    def get_index_snapshot(self, index_ticker: str) -> Optional[Dict]:
        """Snapshot for an index (I:SPX, I:NDX, I:DJI...)."""
        tick = index_ticker.upper()
        if not tick.startswith("I:"):
            tick = f"I:{tick}"
        data = self._surface_get("/v3/snapshot/indices", {"ticker.any_of": tick})
        rows = self._results(data)
        if not rows:
            return None
        r = rows[0]
        session = r.get("session") or {}
        return {
            "symbol": r.get("ticker"),
            "name": r.get("name"),
            "value": r.get("value"),
            "change": session.get("change"),
            "change_pct": session.get("change_percent"),
        }

    def get_crypto_snapshot(self, symbol: str) -> Optional[Dict]:
        """Snapshot for a crypto pair. Accepts BTC, BTC-USD, or X:BTCUSD."""
        s = symbol.upper().replace("-USD", "USD")
        if not s.startswith("X:"):
            if not s.endswith("USD"):
                s = f"{s}USD"
            s = f"X:{s}"
        data = self._surface_get(
            f"/v2/snapshot/locale/global/markets/crypto/tickers/{s}"
        )
        t = (data or {}).get("ticker")
        if not isinstance(t, dict):
            return None
        day = t.get("day") or {}
        return {
            "symbol": t.get("ticker"),
            "price": day.get("c") or (t.get("lastTrade") or {}).get("p"),
            "open": day.get("o"),
            "high": day.get("h"),
            "low": day.get("l"),
            "volume": day.get("v"),
            "change_pct": t.get("todaysChangePerc"),
        }
