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
TesseractFacade — Strategy-pattern provider split.

Sits between the Massive REST-based ``PriceProvider`` surface (live quotes,
OHLCV history, news) and the Tesseract ``ParquetTransport`` (ML predictions,
sentiment, regime, volatility estimates from bulk flat-file feed).

Routing rules:
  * ``get_quote`` / ``get_quotes``         → REST transport (MassiveProvider)
  * ``get_history``                        → REST transport (existing)
  * ``get_predictions`` / ``latest_prediction`` → ParquetTransport
  * ``get_features``                       → ParquetTransport
  * ``get_sentiment``                      → ParquetTransport
  * ``get_regime``                         → ParquetTransport

The facade enforces:
  * Provenance columns present on every returned row.
  * Staleness tagging (fresh / recent / aging / stale).
  * Degraded-mode fallback: if a requested date has no data, use most-recent
    available partition.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ic_engine.providers.price_provider import MassiveProvider

from .ingestion import TesseractIngestion
from .models import TesseractFeature, TesseractPrediction, staleness_tier
from .parquet_transport import ParquetTransport
from .sync_state import SyncState

logger = logging.getLogger(__name__)


class TesseractFacade:
    """Unified facade split between REST (MassiveProvider) and ParquetTransport.

    Parameters:
        data_dir:      Root parquet storage directory.
        api_key:       Massive API key.
        ingest:        Optional pre-configured ingestion engine.
        transport:     Optional pre-built ParquetTransport.
        rest_provider: Optional pre-built MassiveProvider.
    """

    NAME = "massive"

    def __init__(
        self,
        data_dir: Path,
        api_key: Optional[str] = None,
        *,
        ingest: Optional[TesseractIngestion] = None,
        transport: Optional[ParquetTransport] = None,
        rest_provider: Optional[MassiveProvider] = None,
    ):
        self.data_dir = Path(data_dir)
        self.api_key = api_key

        # REST transport — live quotes, OHLCV, news
        self.rest = rest_provider or MassiveProvider(api_key=api_key)

        # Ingestion engine — bulk flat-file download + atomic partition writing
        self.ingest = ingest or TesseractIngestion(data_dir=data_dir, api_key=api_key)

        # Parquet transport — ML feature/prediction queries with predicate pushdown
        self.parquet = transport or ParquetTransport(
            data_dir=data_dir, sync_state=self.ingest.sync_state
        )

    # ── REST delegation ──────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """Live quote from Massive REST. (Not from parquet.)"""
        return self.rest.get_quote(symbol)

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """Batch live quotes from Massive REST."""
        return self.rest.get_quotes(symbols)

    def get_history(self, symbol: str, days: int = 365) -> List[Dict]:
        """OHLCV history from Massive REST."""
        return self.rest.get_history(symbol, days=days)

    def get_news(self, symbols: List[str], days: int = 7) -> List[Dict]:
        """News from Massive REST."""
        return self.rest.get_news(symbols, days=days)

    def get_futures_contracts(
        self, product_code: Optional[str] = None, active: Optional[bool] = True, limit: int = 100
    ) -> List[Dict]:
        return self.rest.get_futures_contracts(
            product_code=product_code, active=active, limit=limit
        )

    def get_futures_snapshot(self, ticker: str) -> Optional[Dict]:
        return self.rest.get_futures_snapshot(ticker)

    # ── ParquetTransport delegation — ML predictions ─────────────────────────

    def get_features(
        self,
        symbol: str,
        *,
        lookback_days: int = 63,
        as_of_date: Optional[str] = None,
    ) -> List[TesseractFeature]:
        """Tesseract ML feature rows for a symbol (verbatim from parquet)."""
        return self.parquet.features_for_symbol(
            symbol, lookback_days=lookback_days, as_of_date=as_of_date
        )

    def latest_prediction(
        self,
        symbol: str,
        *,
        horizon: str = "1d",
    ) -> Optional[Dict]:
        """Most-recent Tesseract prediction with provenance + staleness."""
        return self.parquet.latest_prediction(symbol, horizon=horizon)

    def latest_predictions(
        self,
        symbols: List[str],
        *,
        horizon: str = "1d",
    ) -> Dict[str, Dict]:
        """Batch latest predictions."""
        return self.parquet.latest_predictions(symbols, horizon=horizon)

    def get_sentiment(
        self,
        symbol: str,
        *,
        lookback_days: int = 21,
    ) -> List[Dict]:
        """Recent sentiment_score rows for a symbol."""
        features = self.parquet.features_for_symbol(
            symbol, lookback_days=lookback_days
        )
        return [
            {
                "symbol": f.symbol,
                "date": f.date,
                "sentiment_score": f.sentiment_score,
                "provenance": f.provenance,
                "as_of": f.as_of,
                "confidence": f.confidence,
                "staleness": staleness_tier(f.staleness_days),
            }
            for f in features
            if f.sentiment_score is not None
        ]

    def get_regime(
        self,
        symbol: str,
        *,
        lookback_days: int = 21,
    ) -> Optional[Dict]:
        """Most-recent market regime label for a symbol."""
        features = self.parquet.features_for_symbol(
            symbol, lookback_days=lookback_days
        )
        for f in reversed(features):
            if f.regime is not None:
                return {
                    "symbol": f.symbol,
                    "date": f.date,
                    "regime": f.regime,
                    "provenance": f.provenance,
                    "as_of": f.as_of,
                    "confidence": f.confidence,
                    "staleness": staleness_tier(f.staleness_days),
                }
        return None

    def get_volatility(
        self,
        symbol: str,
        *,
        lookback_days: int = 21,
    ) -> Optional[Dict]:
        """Most-recent estimated 30d forward volatility."""
        features = self.parquet.features_for_symbol(
            symbol, lookback_days=lookback_days
        )
        for f in reversed(features):
            if f.volatility_est is not None:
                return {
                    "symbol": f.symbol,
                    "date": f.date,
                    "volatility_30d_est": f.volatility_est,
                    "provenance": f.provenance,
                    "as_of": f.as_of,
                    "confidence": f.confidence,
                    "staleness": staleness_tier(f.staleness_days),
                }
        return None

    # ── Ingestion delegation ─────────────────────────────────────────────────

    def sync_daily(self, date_str: str, *, force: bool = False) -> Optional[str]:
        """Download + ingest a single day's Tesseract bulk file."""
        return self.ingest.sync_daily(date_str, force=force)

    def sync_range(
        self, start: str, end: Optional[str] = None, *, force: bool = False
    ) -> Dict[str, str]:
        return self.ingest.sync_range(start, end, force=force)

    def sync_all(self, *, force: bool = False) -> Dict[str, str]:
        return self.ingest.sync_all(force=force)

    def partitions(self) -> List[str]:
        return self.ingest.partitions()

    # ── Diagnostics ──────────────────────────────────────────────────────────

    def health(self) -> Dict:
        return {
            "rest": self.rest.NAME,
            "parquet": self.parquet.health(),
            "sync": {
                "partitions": self.ingest.partitions(),
                "latest": self.ingest.latest_partition(),
            },
        }

    def symbol_coverage(self) -> Set[str]:
        return self.parquet.symbol_coverage()
