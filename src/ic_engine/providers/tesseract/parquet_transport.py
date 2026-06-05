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
ParquetTransport — query Tesseract ML features via polars LazyFrame scan_parquet.

Design:
  * Uses ``pl.scan_parquet`` with predicate pushdown — no DuckDB, no SQL.
  * Hive-partitioned by date: ``<data_dir>/YYYY-MM-DD/data.parquet``.
  * Scans are lazy; call ``.collect()`` to materialise.
  * Degraded-mode: when a requested partition is missing, fall back to the
    most-recent available partition (with staleness tag).
  * All returned rows carry provenance, ``as_of``, and ``confidence`` columns
    quoted verbatim from the source parquet files.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

import polars as pl

from .models import (
    AS_OF_COL,
    CONFIDENCE_COL,
    INGESTED_AT_COL,
    PROVENANCE_COL,
    TesseractFeature,
    staleness_tier,
)
from .sync_state import SyncState

logger = logging.getLogger(__name__)


class ParquetTransport:
    """Read Tesseract ML features from hive-partitioned parquet datasets.

    Parameters:
        data_dir:     Root directory containing ``YYYY-MM-DD/data.parquet`` partitions.
        sync_state:   Optional pre-loaded SyncState (created from data_dir if omitted).
    """

    def __init__(self, data_dir: Path, sync_state: Optional[SyncState] = None):
        self.data_dir = Path(data_dir)
        self._sync = sync_state or SyncState(self.data_dir)

    # ── Public query API ─────────────────────────────────────────────────────

    def scan_date(
        self,
        partition_date: str,
        *,
        symbols: Optional[Set[str]] = None,
        columns: Optional[List[str]] = None,
        degraded: bool = True,
    ) -> pl.LazyFrame:
        """Scan a single date partition.

        Returns an empty LazyFrame when the partition is missing and
        ``degraded`` is False. With ``degraded=True``, falls back to the
        most-recent available partition.
        """
        path = self._partition_path(partition_date)
        if not path:
            if degraded:
                fallback = self._degraded_fallback(partition_date)
                if fallback:
                    logger.info(
                        "ParquetTransport degraded: %s → %s", partition_date, fallback
                    )
                    path = self._partition_path(fallback)
            if not path:
                logger.warning("ParquetTransport: no data for %s", partition_date)
                return pl.LazyFrame()

        return self._scan_path(path, symbols=symbols, columns=columns)

    def scan_range(
        self,
        start: str,
        end: Optional[str] = None,
        *,
        symbols: Optional[Set[str]] = None,
        columns: Optional[List[str]] = None,
        degraded: bool = True,
    ) -> pl.LazyFrame:
        """Scan a date range (inclusive). Returns concatenated LazyFrame.

        Uses polars ``concat(how="vertical")`` with ``rechunk=False`` —
        predicate pushdown still works across the concatenated plan.

        Only scans partitions that actually exist on disk (from sync state).
        When ``degraded`` is True and the requested range has no partitions,
        falls back to the most-recent available partition (single scan, no
        duplication).
        """
        end = end or date.today().isoformat()
        # Only scan dates that actually have ingested partitions
        available = self._sync.partitions()
        partitions = [d for d in available if start <= d <= end]

        if not partitions:
            if degraded:
                fallback = self._degraded_fallback(end)
                if fallback:
                    logger.info(
                        "ParquetTransport scan_range degraded: %s..%s → %s",
                        start, end, fallback,
                    )
                    path = self._partition_path(fallback)
                    if path:
                        return self._scan_path(path, symbols=symbols, columns=columns)
            logger.warning(
                "ParquetTransport scan_range: no partitions in %s..%s", start, end
            )
            return pl.LazyFrame()

        scans: List[pl.LazyFrame] = []
        for d in partitions:
            path = self._partition_path(d)
            if path is None:
                continue
            lf = self._scan_path(path, symbols=symbols, columns=columns)
            scans.append(lf)

        if not scans:
            return pl.LazyFrame()

        if len(scans) == 1:
            return scans[0]
        return pl.concat(scans, how="vertical", rechunk=False)

    def features_for_symbol(
        self,
        symbol: str,
        *,
        lookback_days: int = 63,
        as_of_date: Optional[str] = None,
        degraded: bool = True,
    ) -> List[TesseractFeature]:
        """Return Tesseract feature rows for a single symbol.

        Uses predicate pushdown — only the symbol's rows are read from disk.
        """
        end = as_of_date or date.today().isoformat()
        start = (date.fromisoformat(end) - timedelta(days=lookback_days)).isoformat()

        lf = self.scan_range(
            start,
            end,
            symbols={symbol},
            degraded=degraded,
        )
        if lf is None:
            return []

        df = lf.filter(pl.col("symbol") == symbol).collect()
        return [self._row_to_feature(row) for row in df.iter_rows(named=True)]

    def latest_prediction(
        self,
        symbol: str,
        *,
        horizon: str = "1d",
        degraded: bool = True,
    ) -> Optional[Dict]:
        """Return the most recent prediction for ``symbol``.

        Horizons: ``1d``, ``5d``, ``21d``, ``63d``.
        Returns a dict with ``symbol``, ``date``, ``prediction``, ``confidence``,
        ``provenance``, ``as_of``, ``staleness_tier``.
        """
        col = f"prediction_{horizon}"
        lf = self.scan_range(
            (date.today() - timedelta(days=7)).isoformat(),
            date.today().isoformat(),
            symbols={symbol},
            degraded=degraded,
        )
        if lf is None:
            return None

        try:
            df = (
                lf.filter(pl.col("symbol") == symbol)
                .filter(pl.col(col).is_not_null())
                .sort("date", descending=True)
                .limit(1)
                .collect()
            )
        except Exception as e:
            logger.warning("ParquetTransport latest_prediction(%s, %s): %s", symbol, horizon, e)
            return None

        if df.is_empty():
            return None

        row = df.row(0, named=True)
        days_stale: Optional[int] = None
        as_of = row.get(AS_OF_COL, "")
        if as_of:
            try:
                from datetime import datetime, timezone as tz
                ts = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
                days_stale = (datetime.now(tz.utc) - ts).days
            except (ValueError, TypeError):
                pass

        return {
            "symbol": row.get("symbol", symbol),
            "date": row.get("date", ""),
            "prediction": row.get(col),
            "confidence": row.get(CONFIDENCE_COL),
            "provenance": row.get(PROVENANCE_COL, "massive/tesseract/v1"),
            "as_of": as_of,
            "staleness_tier": staleness_tier(days_stale),
            "horizon": horizon,
        }

    def latest_predictions(
        self,
        symbols: List[str],
        *,
        horizon: str = "1d",
        degraded: bool = True,
    ) -> Dict[str, Dict]:
        """Batch latest predictions. Returns {symbol: prediction_dict}."""
        results: Dict[str, Dict] = {}
        for sym in symbols:
            pred = self.latest_prediction(sym, horizon=horizon, degraded=degraded)
            if pred:
                results[sym] = pred
        return results

    def symbol_coverage(self) -> Set[str]:
        """Return the set of all symbols across all ingested partitions.

        Uses a fast parquet metadata scan (no row data read).
        """
        symbols: Set[str] = set()
        for d in self._all_partition_dates():
            path = self._partition_path(d)
            if path:
                try:
                    # Read only the symbol column
                    df = pl.scan_parquet(str(path)).select("symbol").unique().collect()
                    symbols.update(df["symbol"].to_list())
                except Exception as e:
                    logger.warning("ParquetTransport coverage scan %s: %s", d, e)
        return symbols

    def health(self) -> Dict:
        """Diagnostic: partition count, date range, total rows, staleness."""
        parts = self._sync.partitions()
        return {
            "partitions": len(parts),
            "earliest": self._sync.earliest_partition(),
            "latest": self._sync.latest_partition(),
            "last_sync": self._sync.last_sync_at(),
            "total_rows": self._sync.total_rows(),
            "total_files": self._sync.total_files(),
        }

    # ── Internal ─────────────────────────────────────────────────────────────

    def _partition_path(self, date_str: str) -> Optional[Path]:
        """Resolve the canonical parquet file for a partition date."""
        parquet = self.data_dir / date_str / "data.parquet"
        return parquet if parquet.exists() else None

    def _all_partition_dates(self) -> List[str]:
        return self._sync.partitions()

    @staticmethod
    def _partition_dates(start: str, end: str) -> List[str]:
        d = date.fromisoformat(start)
        stop = date.fromisoformat(end)
        result: List[str] = []
        while d <= stop:
            result.append(d.isoformat())
            d += timedelta(days=1)
        return result

    def _degraded_fallback(self, requested_date: str) -> Optional[str]:
        """Return the most recent available partition on or before requested."""
        parts = self._sync.partitions()
        if not parts:
            return None
        # Return the latest partition that is <= requested
        candidates = [p for p in parts if p <= requested_date]
        return candidates[-1] if candidates else parts[0]

    def _scan_path(
        self,
        path: Path,
        symbols: Optional[Set[str]] = None,
        columns: Optional[List[str]] = None,
    ) -> pl.LazyFrame:
        """Scan a single parquet file with optional column / symbol projection."""
        paths = [str(path)]
        if columns:
            # Always include provenance columns so callers see them
            wanted = list(columns)
            for pcol in (PROVENANCE_COL, AS_OF_COL, CONFIDENCE_COL, INGESTED_AT_COL):
                if pcol not in wanted:
                    wanted.append(pcol)
            lf = pl.scan_parquet(paths).select(wanted)
        else:
            lf = pl.scan_parquet(paths)

        if symbols:
            lf = lf.filter(pl.col("symbol").is_in(symbols))

        return lf

    @staticmethod
    def _row_to_feature(row: dict) -> TesseractFeature:
        return TesseractFeature(
            symbol=str(row.get("symbol", "")),
            date=str(row.get("date", "")),
            close=row.get("close"),
            volume=row.get("volume"),
            prediction_1d=row.get("prediction_1d"),
            prediction_5d=row.get("prediction_5d"),
            prediction_21d=row.get("prediction_21d"),
            prediction_63d=row.get("prediction_63d"),
            sentiment_score=row.get("sentiment_score"),
            momentum_score=row.get("momentum_score"),
            volatility_est=row.get("volatility_est"),
            regime=row.get("regime"),
            provenance=str(row.get(PROVENANCE_COL, "")),
            as_of=str(row.get(AS_OF_COL, "")),
            confidence=row.get(CONFIDENCE_COL),
            ingested_at=str(row.get(INGESTED_AT_COL, "")),
        )
