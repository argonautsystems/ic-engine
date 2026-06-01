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
DataExtractor — Phase 2 Stage P1b
=================================

Financial-data extraction/parsing stage for InvestorClaw.

Adapts the RiskyEats SunBiz parser pattern (cache-first, empty-check,
statistics collection) to the output of :class:`DataDownloader`. Where
the downloader produces raw provider payloads (pandas DataFrames for
yfinance, JSON dicts for REST APIs), the extractor:

* Detects payload format from the downloader's cache file.
* Extracts the ticker-relevant fields (OHLCV, news articles, etc.).
* Runs integrity checks (empty frames, all-NaN price columns).
* Converts everything into a Polars DataFrame for downstream transform.
* Collects statistics (record counts, date ranges, missing symbols).

Cache-first philosophy
----------------------
If the downloader's cache file exists, the extractor reads directly from
it — no re-fetch. This is the RiskyEats "cache parquet → integrity-check
→ return" pattern, lifted intact.

The extractor *does not* call out to any network. It consumes what the
downloader already produced.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Extraction result types
# ---------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    """Outcome of a single extraction operation over a bundle of symbols.

    Attributes:
        data:            Symbol → Polars DataFrame (standardized per source
                         schema family but not yet transformed to CDM).
        statistics:      Per-symbol record counts + aggregate counts.
        cache_hit:       True if *every* input path was satisfied from cache.
        integrity_valid: False if any DataFrame failed integrity checks
                         (empty, all-NaN prices, missing required columns).
        missing_symbols: Symbols that were requested but produced no data.
        errors:          Per-symbol extraction errors (if any).
    """

    data: Dict[str, Any] = field(default_factory=dict)
    statistics: Dict[str, int] = field(default_factory=dict)
    cache_hit: bool = False
    integrity_valid: bool = True
    missing_symbols: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)

    def symbols(self) -> List[str]:
        return list(self.data.keys())

    def summary(self) -> Dict[str, Any]:
        return {
            "symbols_ok": len(self.data),
            "symbols_missing": len(self.missing_symbols),
            "symbols_errored": len(self.errors),
            "total_records": sum(self.statistics.values()),
            "cache_hit_all": self.cache_hit,
            "integrity_valid": self.integrity_valid,
        }


# ---------------------------------------------------------------------------
# Schema specifications
# ---------------------------------------------------------------------------
# Each supported source has a minimal "expected columns / integrity rules"
# spec. These are *pre-transform* schemas — the transformer will rename and
# re-type them into the canonical CDM shape.


@dataclass
class ExtractionSchema:
    """Minimal extraction spec for a source format."""

    source: str  # logical name, e.g. "yfinance_ohlcv"
    format: str  # "parquet" | "json" | "csv"
    required_columns: List[str] = field(default_factory=list)
    nan_sensitive_columns: List[str] = field(default_factory=list)
    json_records_path: Optional[List[str]] = None  # e.g. ["results"]


DEFAULT_SCHEMAS: Dict[str, ExtractionSchema] = {
    "yfinance_ohlcv": ExtractionSchema(
        source="yfinance_ohlcv",
        format="parquet",
        required_columns=["Open", "High", "Low", "Close", "Volume"],
        nan_sensitive_columns=["Close"],
    ),
    "finnhub_candles": ExtractionSchema(
        source="finnhub_candles",
        format="json",
        required_columns=["c", "h", "l", "o", "t", "v"],  # close/high/low/open/time/volume
        nan_sensitive_columns=["c"],
    ),
    "massive_aggs": ExtractionSchema(
        source="massive_aggs",
        format="json",
        required_columns=["c", "h", "l", "o", "t", "v"],
        nan_sensitive_columns=["c"],
        json_records_path=["results"],
    ),
    "alphavantage_daily": ExtractionSchema(
        source="alphavantage_daily",
        format="json",
        required_columns=["1. open", "2. high", "3. low", "4. close", "5. volume"],
        nan_sensitive_columns=["4. close"],
    ),
    "newsapi_everything": ExtractionSchema(
        source="newsapi_everything",
        format="json",
        required_columns=["title", "publishedAt", "source"],
        json_records_path=["articles"],
    ),
}


# ---------------------------------------------------------------------------
# DataExtractor
# ---------------------------------------------------------------------------


class DataExtractor:
    """Parse downloader cache files into Polars DataFrames with integrity checks.

    Usage::

        extractor = DataExtractor()
        result = extractor.extract(
            {"AAPL": Path(".../cache/yfinance__AAPL__abc.bin"),
             "MSFT": Path(".../cache/yfinance__MSFT__def.bin")},
            schema=DEFAULT_SCHEMAS["yfinance_ohlcv"],
        )
        if not result.integrity_valid:
            ...
    """

    def __init__(self, schemas: Optional[Dict[str, ExtractionSchema]] = None):
        self.schemas: Dict[str, ExtractionSchema] = {**DEFAULT_SCHEMAS}
        if schemas:
            self.schemas.update(schemas)

    # ----- Public API -------------------------------------------------------

    def extract(
        self,
        raw_data: Dict[str, Path],
        schema: ExtractionSchema,
    ) -> ExtractionResult:
        """Extract parsed data for every (symbol → cache path) entry.

        Args:
            raw_data: Mapping of symbol → Path produced by DataDownloader.
                      A value of None indicates the downloader failed for
                      that symbol; it's recorded as missing and skipped.
            schema:   Extraction schema describing the expected format.

        Returns:
            An ExtractionResult capturing parsed data, statistics, and
            integrity flags.
        """
        try:
            import polars as pl
        except ImportError as e:
            raise ImportError(
                "DataExtractor requires polars. Install with: pip install polars"
            ) from e

        result = ExtractionResult()
        all_cache = True

        for symbol, path in raw_data.items():
            if path is None or not Path(path).exists():
                result.missing_symbols.append(symbol)
                all_cache = False
                continue

            try:
                df = self._parse_one(Path(path), schema)
            except Exception as e:
                logger.error("[DataExtractor] %s: parse failed: %s", symbol, e)
                result.errors[symbol] = f"{type(e).__name__}: {e}"
                result.integrity_valid = False
                continue

            # Integrity checks
            if df is None or df.is_empty():
                logger.warning("[DataExtractor] %s: empty DataFrame — missing", symbol)
                result.missing_symbols.append(symbol)
                result.integrity_valid = False
                continue

            missing_cols = [c for c in schema.required_columns if c not in df.columns]
            if missing_cols:
                logger.warning(
                    "[DataExtractor] %s: missing required columns %s",
                    symbol,
                    missing_cols,
                )
                result.integrity_valid = False
                # Still record the partial data for inspection

            for col in schema.nan_sensitive_columns:
                if col in df.columns:
                    # Polars null count
                    nulls = int(df[col].null_count())
                    if nulls == df.height:
                        logger.warning(
                            "[DataExtractor] %s: column %r is all-NaN",
                            symbol,
                            col,
                        )
                        result.integrity_valid = False

            # Attach symbol as a derived column so downstream transform has it.
            df = df.with_columns(pl.lit(symbol).alias("_symbol"))

            result.data[symbol] = df
            result.statistics[symbol] = int(df.height)

        result.cache_hit = all_cache and not result.errors
        # Aggregate stat
        result.statistics["_total_records"] = sum(
            v for k, v in result.statistics.items() if k != "_total_records"
        )

        logger.info(
            "[DataExtractor] schema=%s: %d ok, %d missing, %d errored, %d total records",
            schema.source,
            len(result.data),
            len(result.missing_symbols),
            len(result.errors),
            result.statistics["_total_records"],
        )
        return result

    # ----- Internal ---------------------------------------------------------

    def _parse_one(self, path: Path, schema: ExtractionSchema):
        """Dispatch on schema.format to load a single cache file."""
        import polars as pl

        fmt = schema.format.lower()

        if fmt == "parquet":
            # DataDownloader writes pandas.to_parquet via _write_cache, and also
            # mirrors to .bin. Prefer the companion .parquet if present.
            parquet_path = path.with_suffix(".parquet")
            if parquet_path.exists():
                return pl.read_parquet(parquet_path)
            # Fallback: read the .bin as parquet (bytes are identical).
            return pl.read_parquet(path)

        if fmt == "csv":
            return pl.read_csv(path)

        if fmt == "json":
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            records = self._navigate_json(payload, schema.json_records_path)
            if records is None:
                # Alpha Vantage–style: records are values of a dict keyed by date.
                if isinstance(payload, dict):
                    ts_key = next(
                        (k for k in payload if "time series" in k.lower()),
                        None,
                    )
                    if ts_key:
                        ts = payload.get(ts_key) or {}
                        records = [
                            {"_date": date, **vals}
                            for date, vals in ts.items()
                            if isinstance(vals, dict)
                        ]
                # Finnhub shape: parallel arrays under top-level keys.
                if (
                    records is None
                    and isinstance(payload, dict)
                    and all(k in payload for k in ("c", "h", "l", "o", "t", "v"))
                ):
                    length = len(payload.get("t") or [])
                    records = [
                        {k: payload[k][i] for k in ("c", "h", "l", "o", "t", "v")}
                        for i in range(length)
                    ]
            if not records:
                return pl.DataFrame()
            return pl.DataFrame(records)

        raise ValueError(f"Unsupported extraction format: {schema.format!r}")

    @staticmethod
    def _navigate_json(payload: Any, path: Optional[List[str]]) -> Optional[list]:
        """Walk nested dict keys; return a list or None."""
        if path is None:
            if isinstance(payload, list):
                return payload
            return None
        cur: Any = payload
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        if isinstance(cur, list):
            return cur
        return None


__all__ = [
    "DataExtractor",
    "ExtractionResult",
    "ExtractionSchema",
    "DEFAULT_SCHEMAS",
]
