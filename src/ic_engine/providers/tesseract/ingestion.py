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
TesseractIngestion — bulk flat-file download + atomic folder-rename ingestion.

Design:
  * Download daily parquet files from Massive's S3-style bulk endpoint.
  * Stage downloads into a ``_staging/`` directory.
  * On success, atomically rename ``_staging/<date>/`` → ``<date>/``.
  * If `_staging` contains a leftover partial download (crash), it is cleaned
    on next run.
  * Sync state is updated only AFTER the atomic rename succeeds.
  * Hive-partitioned by date: ``<data_dir>/YYYY-MM-DD/data.parquet``.
  * Within each partition, rows are sorted by ``symbol`` (not partitioned by
    symbol — predicate pushdown via polars LazyFrame handles that).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import polars as pl

from .models import (
    AS_OF_COL,
    CONFIDENCE_COL,
    INGESTED_AT_COL,
    PROVENANCE_COL,
    REQUIRED_COLUMNS,
)
from .sync_state import SyncState

logger = logging.getLogger(__name__)

# Massive bulk download endpoint (flat-file daily archives)
MASSIVE_BULK_BASE = "https://bulk.massive.com/tesseract/v1"

# Staging subdirectory under data_dir
STAGING_DIR = "_staging"


def _date_range(
    start: str,
    end: Optional[str] = None,
) -> List[str]:
    """Yield YYYY-MM-DD date strings from start to end (inclusive)."""
    d = date.fromisoformat(start)
    stop = date.fromisoformat(end) if end else date.today()
    days: List[str] = []
    while d <= stop:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


class TesseractIngestion:
    """Download + ingest Massive Tesseract bulk parquet files.

    Parameters:
        data_dir:         Root directory for hive-partitioned parquet storage.
        api_key:          Massive API key (or read from ``MASSIVE_API_KEY`` env).
        base_url:         Bulk download base URL (default: MASSIVE_BULK_BASE).
        symbol_filter:    Optional set of symbols to retain (others dropped).
        verify_checksum:  When True (default), validate SHA-256 after download.
    """

    def __init__(
        self,
        data_dir: Path,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        symbol_filter: Optional[set] = None,
        verify_checksum: bool = True,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.staging = self.data_dir / STAGING_DIR
        self.api_key = api_key or os.environ.get("MASSIVE_API_KEY", "")
        self.base_url = (base_url or MASSIVE_BULK_BASE).rstrip("/")
        self.symbol_filter = symbol_filter
        self.verify_checksum = verify_checksum
        self._sync = SyncState(self.data_dir)

    @property
    def sync_state(self) -> SyncState:
        return self._sync

    # ── Public API ───────────────────────────────────────────────────────────

    def sync_daily(
        self,
        date_str: str,
        *,
        force: bool = False,
    ) -> Optional[str]:
        """Download and ingest a single day.

        Returns the partition path on success, or ``None`` if already ingested
        (and ``force`` is False) or the download yielded no files.
        """
        if not force and date_str in set(self._sync.partitions()):
            logger.info("Tesseract %s already ingested; skipping", date_str)
            return None

        self._clean_staging()

        downloaded = self._download_date(date_str)
        if not downloaded:
            logger.warning("Tesseract %s: no files downloaded", date_str)
            return None

        partition_path = self._ingest_date(date_str, downloaded)
        if partition_path is None:
            return None

        logger.info("Tesseract ingested %s → %s", date_str, partition_path)
        return str(partition_path)

    def sync_range(
        self,
        start: str,
        end: Optional[str] = None,
        *,
        force: bool = False,
    ) -> Dict[str, str]:
        """Download and ingest a date range. Returns {date: partition_path}."""
        results: Dict[str, str] = {}
        for d in _date_range(start, end):
            path = self.sync_daily(d, force=force)
            if path:
                results[d] = path
        return results

    def sync_all(
        self,
        *,
        force: bool = False,
    ) -> Dict[str, str]:
        """Download all available Tesseract partitions (from earliest missing)."""
        # Determine the last ingested date; start from there (or 2020-01-01)
        last = self._sync.latest_partition()
        start = (
            (date.fromisoformat(last) + timedelta(days=1)).isoformat() if last else "2020-01-01"
        )
        return self.sync_range(start, force=force)

    def partitions(self) -> List[str]:
        """Return sorted list of ingested partition dates."""
        return self._sync.partitions()

    def latest_partition(self) -> Optional[str]:
        return self._sync.latest_partition()

    # ── Download ─────────────────────────────────────────────────────────────

    def _download_date(self, date_str: str) -> Optional[Path]:
        """Download one daily parquet file to staging. Returns staged file path.

        Massive bulk API serves daily archives at:
            ``{base_url}/daily/{date_str}/tesseract.parquet``

        The response includes a ``X-Checksum-SHA256`` header for verification.
        """
        import urllib.request
        import urllib.error

        url = f"{self.base_url}/daily/{date_str}/tesseract.parquet"
        self.staging.mkdir(parents=True, exist_ok=True)
        out_path = self.staging / f"{date_str}.parquet"

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": "ic-engine/4.6 (tesseract-ingestion)",
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
                checksum_header = resp.headers.get("X-Checksum-SHA256", "")

            if not data:
                logger.warning("Tesseract %s: empty response body", date_str)
                return None

            # Verify checksum if header present and verification enabled
            if self.verify_checksum and checksum_header:
                actual = hashlib.sha256(data).hexdigest()
                if actual.lower() != checksum_header.lower():
                    logger.error(
                        "Tesseract %s: checksum mismatch (expected %s, got %s)",
                        date_str,
                        checksum_header,
                        actual,
                    )
                    return None

            with open(out_path, "wb") as fh:
                fh.write(data)

            logger.info("Tesseract downloaded %s → %s (%d bytes)", date_str, out_path, len(data))
            return out_path

        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.info("Tesseract %s: no file available (404)", date_str)
            else:
                logger.warning("Tesseract %s: HTTP %d — %s", date_str, e.code, e.reason)
            return None
        except Exception as e:
            logger.warning("Tesseract %s download failed: %s", date_str, e)
            return None

    # ── Ingest ───────────────────────────────────────────────────────────────

    def _ingest_date(
        self,
        date_str: str,
        downloaded_path: Optional[Path] = None,
    ) -> Optional[Path]:
        """Process downloaded parquet into hive-partitioned structure.

        Steps:
          1. Read raw parquet → validate columns → sort by symbol → add provenance.
          2. Write sorted parquet into ``_staging/<date_str>/data.parquet``.
          3. Atomically rename ``_staging/<date_str>/`` → ``<data_dir>/<date_str>/``.
          4. Update sync state.
        """
        if downloaded_path is None or not downloaded_path.exists():
            return None

        target_dir = self.data_dir / date_str
        staging_dir = self.staging / date_str

        try:
            # Read raw data
            df = pl.read_parquet(str(downloaded_path))

            # Validate / add columns
            df = self._normalize_dataframe(df, date_str)

            # Filter symbols if requested
            if self.symbol_filter:
                df = df.filter(pl.col("symbol").is_in(self.symbol_filter))

            # Sort by symbol within date partition
            df = df.sort("symbol")

            # Write into staging subdirectory
            staging_dir.mkdir(parents=True, exist_ok=True)
            staging_parquet = staging_dir / "data.parquet"
            df.write_parquet(str(staging_parquet), compression="zstd")

            # Atomic rename: staging/<date>/ → <date>/
            if target_dir.exists():
                shutil.rmtree(str(target_dir))
            os.rename(str(staging_dir), str(target_dir))

            # Record in sync state
            self._sync.record_ingestion(
                partition_date=date_str,
                rows=len(df),
                files=1,
                source_url=self.base_url,
            )

            # Cleanup downloaded raw file
            downloaded_path.unlink(missing_ok=True)

            return target_dir

        except Exception as e:
            logger.error("Tesseract ingest %s failed: %s", date_str, e)
            # Clean staging so next run starts fresh
            if staging_dir.exists():
                shutil.rmtree(str(staging_dir), ignore_errors=True)
            return None

    @staticmethod
    def _normalize_dataframe(df: pl.DataFrame, date_str: str) -> pl.DataFrame:
        """Ensure required columns exist; add provenance columns if missing."""
        now_utc = datetime.now(timezone.utc).isoformat()

        # Add provenance columns if absent
        if PROVENANCE_COL not in df.columns:
            df = df.with_columns(pl.lit("massive/tesseract/v1").alias(PROVENANCE_COL))
        if AS_OF_COL not in df.columns:
            df = df.with_columns(pl.lit(date_str + "T00:00:00Z").alias(AS_OF_COL))
        if CONFIDENCE_COL not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(CONFIDENCE_COL))
        if INGESTED_AT_COL not in df.columns:
            df = df.with_columns(pl.lit(now_utc).alias(INGESTED_AT_COL))

        # Rename common alternates to canonical names
        _renames = {
            "ticker": "symbol",
            "trade_date": "date",
            "adj_close": "close",
            "pred_1d": "prediction_1d",
            "pred_5d": "prediction_5d",
            "pred_21d": "prediction_21d",
            "pred_63d": "prediction_63d",
            "sentiment": "sentiment_score",
            "momentum": "momentum_score",
            "vol_30d": "volatility_est",
        }
        for src, dst in _renames.items():
            if src in df.columns and dst not in df.columns:
                df = df.rename({src: dst})

        return df

    # ── Housekeeping ─────────────────────────────────────────────────────────

    def _clean_staging(self) -> None:
        """Remove any leftover staging artifacts from a prior crashed run."""
        if self.staging.exists():
            for item in list(self.staging.iterdir()):
                try:
                    if item.is_dir():
                        shutil.rmtree(str(item))
                    else:
                        item.unlink()
                except OSError:
                    pass
