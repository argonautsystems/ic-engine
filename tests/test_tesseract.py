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
Tests for Tesseract — Massive ML prediction features via flat-file parquet.

Covers:
  * SyncState CRUD + atomic writes
  * Model dataclasses (TesseractFeature, TesseractPrediction)
  * Staleness tier classification
  * Ingestion: parquet normalisation, column mapping, provenance injection
  * ParquetTransport: scan_parquet, predicate pushdown, degraded-mode fallback
  * TesseractFacade: REST/Parquet split, provenance enforcement

All parquet I/O uses temporary directories; no live network calls.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import polars as pl
import pytest

from ic_engine.providers.tesseract.ingestion import TesseractIngestion
from ic_engine.providers.tesseract.models import (
    STALENESS_THRESHOLD_DAYS,
    TesseractFeature,
    TesseractPrediction,
    staleness_tier,
)
from ic_engine.providers.tesseract.parquet_transport import ParquetTransport
from ic_engine.providers.tesseract.sync_state import STATE_FILENAME, SyncState


# ── Test data helpers ────────────────────────────────────────────────────────


def _sample_df() -> pl.DataFrame:
    """A small well-formed Tesseract parquet with three symbols, two dates."""
    return pl.DataFrame(
        {
            "symbol": ["AAPL", "MSFT", "GOOGL", "AAPL", "MSFT", "GOOGL"],
            "date": [
                "2026-05-01",
                "2026-05-01",
                "2026-05-01",
                "2026-05-02",
                "2026-05-02",
                "2026-05-02",
            ],
            "close": [190.0, 425.0, 175.0, 191.5, 427.0, 176.2],
            "volume": [50_000_000, 22_000_000, 18_000_000, 48_000_000, 21_000_000, 17_500_000],
            "prediction_1d": [191.2, 428.0, 176.5, 192.1, 429.5, 177.0],
            "prediction_5d": [194.0, 435.0, 180.0, 195.0, 436.5, 181.0],
            "prediction_21d": [200.0, 450.0, 185.0, 201.0, 452.0, 186.5],
            "prediction_63d": [210.0, 475.0, 195.0, 211.5, 478.0, 196.0],
            "sentiment_score": [0.65, 0.72, 0.45, 0.68, 0.74, 0.41],
            "momentum_score": [0.55, 0.60, 0.30, 0.58, 0.62, 0.28],
            "volatility_est": [0.18, 0.22, 0.25, 0.17, 0.21, 0.26],
            "regime": ["bull", "bull", "neutral", "bull", "bull", "neutral"],
            "provenance": ["massive/tesseract/v1"] * 6,
            "as_of": [
                "2026-05-01T00:00:00Z",
                "2026-05-01T00:00:00Z",
                "2026-05-01T00:00:00Z",
                "2026-05-02T00:00:00Z",
                "2026-05-02T00:00:00Z",
                "2026-05-02T00:00:00Z",
            ],
            "confidence": [0.95, 0.93, 0.88, 0.94, 0.92, 0.87],
            "ingested_at": [
                "2026-06-05T03:00:00+00:00",
                "2026-06-05T03:00:00+00:00",
                "2026-06-05T03:00:00+00:00",
                "2026-06-05T03:00:00+00:00",
                "2026-06-05T03:00:00+00:00",
                "2026-06-05T03:00:00+00:00",
            ],
        }
    )


def _write_partition(data_dir: Path, date_str: str, df: pl.DataFrame) -> Path:
    """Helper: write a hive-partitioned parquet file (as ingestion would)."""
    part_dir = data_dir / date_str
    part_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = part_dir / "data.parquet"
    df.write_parquet(str(parquet_path), compression="zstd")
    return parquet_path


def _sample_feature(**overrides) -> TesseractFeature:
    defaults = {
        "symbol": "AAPL",
        "date": "2026-05-01",
        "close": 190.0,
        "volume": 50_000_000,
        "prediction_1d": 191.2,
        "prediction_5d": 194.0,
        "prediction_21d": 200.0,
        "prediction_63d": 210.0,
        "sentiment_score": 0.65,
        "momentum_score": 0.55,
        "volatility_est": 0.18,
        "regime": "bull",
        "provenance": "massive/tesseract/v1",
        "as_of": "2026-05-01T00:00:00Z",
        "confidence": 0.95,
        "ingested_at": "2026-06-05T03:00:00+00:00",
    }
    defaults.update(overrides)
    return TesseractFeature(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# SyncState
# ═══════════════════════════════════════════════════════════════════════════════


class TestSyncState:
    def test_empty_state(self, tmp_path):
        ss = SyncState(tmp_path)
        assert ss.partitions() == []
        assert ss.latest_partition() is None
        assert ss.total_rows() == 0

    def test_record_and_read(self, tmp_path):
        ss = SyncState(tmp_path)
        ss.record_ingestion("2026-05-01", rows=100, files=2, source_url="https://bulk.example.com")
        assert ss.partitions() == ["2026-05-01"]
        assert ss.latest_partition() == "2026-05-01"
        assert ss.total_rows() == 100
        assert ss.total_files() == 2
        assert ss.source_url() == "https://bulk.example.com"

    def test_multiple_partitions_sorted(self, tmp_path):
        ss = SyncState(tmp_path)
        ss.record_ingestion("2026-05-03", rows=50, files=1)
        ss.record_ingestion("2026-05-01", rows=30, files=1)
        ss.record_ingestion("2026-05-02", rows=40, files=1)
        assert ss.partitions() == ["2026-05-01", "2026-05-02", "2026-05-03"]
        assert ss.earliest_partition() == "2026-05-01"
        assert ss.latest_partition() == "2026-05-03"
        assert ss.total_rows() == 120

    def test_duplicate_partition_no_double_count(self, tmp_path):
        ss = SyncState(tmp_path)
        ss.record_ingestion("2026-05-01", rows=10, files=1)
        ss.record_ingestion("2026-05-01", rows=10, files=1)
        assert ss.partitions() == ["2026-05-01"]
        assert ss.total_rows() == 20  # rows accumulate because caller already added
        # But partitions only listed once

    def test_last_sync_at_set(self, tmp_path):
        ss = SyncState(tmp_path)
        assert ss.last_sync_at() is None
        ss.record_ingestion("2026-05-01", rows=1, files=1)
        assert ss.last_sync_at() is not None
        dt = datetime.fromisoformat(ss.last_sync_at())
        assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < 10

    def test_atomic_save_no_tmp_leftover(self, tmp_path):
        ss = SyncState(tmp_path)
        ss.record_ingestion("2026-05-01", rows=1, files=1)
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_persistence_across_instances(self, tmp_path):
        ss1 = SyncState(tmp_path)
        ss1.record_ingestion("2026-05-01", rows=42, files=1)

        ss2 = SyncState(tmp_path)
        assert ss2.partitions() == ["2026-05-01"]
        assert ss2.total_rows() == 42

    def test_clear(self, tmp_path):
        ss = SyncState(tmp_path)
        ss.record_ingestion("2026-05-01", rows=1, files=1)
        ss.clear()
        assert ss.partitions() == []
        assert ss.total_rows() == 0

    def test_corrupt_state_file_resets(self, tmp_path):
        state_path = tmp_path / STATE_FILENAME
        state_path.write_text("not valid json{{{", encoding="utf-8")
        ss = SyncState(tmp_path)
        assert ss.partitions() == []


# ═══════════════════════════════════════════════════════════════════════════════
# Models — staleness
# ═══════════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_feature_staleness_days_fresh(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        f = _sample_feature(as_of=today)
        assert f.staleness_days == 0
        assert not f.is_stale

    def test_feature_staleness_days_old(self):
        old = "2020-01-01T00:00:00Z"
        f = _sample_feature(as_of=old)
        assert f.staleness_days is not None
        assert f.staleness_days > STALENESS_THRESHOLD_DAYS
        assert f.is_stale

    def test_feature_staleness_none_when_no_as_of(self):
        f = _sample_feature(as_of="")
        assert f.staleness_days is None
        assert not f.is_stale

    def test_prediction_staleness(self):
        p = TesseractPrediction(
            symbol="AAPL",
            date="2026-05-01",
            horizon="1d",
            predicted_price=191.2,
            provenance="massive/tesseract/v1",
            as_of="2020-01-01T00:00:00Z",
            confidence=0.95,
        )
        assert p.is_stale

    def test_staleness_tier_fresh(self):
        assert staleness_tier(1) == "fresh"
        assert staleness_tier(0) == "fresh"

    def test_staleness_tier_recent(self):
        assert staleness_tier(2) == "recent"
        assert staleness_tier(3) == "recent"

    def test_staleness_tier_aging(self):
        assert staleness_tier(4) == "aging"
        assert staleness_tier(5) == "aging"

    def test_staleness_tier_stale(self):
        assert staleness_tier(6) == "stale"
        assert staleness_tier(100) == "stale"

    def test_staleness_tier_unknown(self):
        assert staleness_tier(None) == "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Ingestion
# ═══════════════════════════════════════════════════════════════════════════════


class TestIngestion:
    def test_normalize_dataframe_adds_provenance(self):
        df = pl.DataFrame(
            {
                "symbol": ["AAPL"],
                "date": ["2026-05-01"],
                "close": [190.0],
                "volume": [50_000_000],
                "prediction_1d": [191.2],
                "prediction_5d": [194.0],
                "prediction_21d": [200.0],
                "prediction_63d": [210.0],
                "sentiment_score": [0.65],
                "momentum_score": [0.55],
                "volatility_est": [0.18],
                "regime": ["bull"],
            }
        )
        result = TesseractIngestion._normalize_dataframe(df, "2026-05-01")
        assert "provenance" in result.columns
        assert result["provenance"][0] == "massive/tesseract/v1"
        assert "as_of" in result.columns
        assert "confidence" in result.columns
        assert "ingested_at" in result.columns

    def test_normalize_dataframe_renames_alternates(self):
        df = pl.DataFrame(
            {
                "ticker": ["AAPL"],
                "trade_date": ["2026-05-01"],
                "close": [190.0],
                "volume": [50_000_000],
                "pred_1d": [191.2],
                "pred_5d": [194.0],
                "pred_21d": [200.0],
                "pred_63d": [210.0],
                "sentiment": [0.65],
                "momentum": [0.55],
                "vol_30d": [0.18],
                "regime": ["bull"],
            }
        )
        result = TesseractIngestion._normalize_dataframe(df, "2026-05-01")
        assert "symbol" in result.columns
        assert result["symbol"][0] == "AAPL"
        assert "date" in result.columns
        assert "prediction_1d" in result.columns
        assert "sentiment_score" in result.columns
        assert "momentum_score" in result.columns
        assert "volatility_est" in result.columns

    def test_normalize_dataframe_does_not_overwrite_existing(self):
        df = pl.DataFrame(
            {
                "symbol": ["MSFT"],
                "ticker": ["SHOULD_NOT_WIN"],
                "date": ["2026-05-01"],
                "close": [425.0],
                "volume": [22_000_000],
                "prediction_1d": [428.0],
                "prediction_5d": [435.0],
                "prediction_21d": [450.0],
                "prediction_63d": [475.0],
                "sentiment_score": [0.72],
                "momentum_score": [0.60],
                "volatility_est": [0.22],
                "regime": ["bull"],
            }
        )
        result = TesseractIngestion._normalize_dataframe(df, "2026-05-01")
        assert result["symbol"][0] == "MSFT"  # existing wins over ticker rename

    def test_ingest_writes_hive_partition_and_updates_sync(self, tmp_path):
        data_dir = tmp_path / "tesseract_data"
        ingestion = TesseractIngestion(data_dir=data_dir, api_key="test-key")

        # Create a mock downloaded parquet
        staging = data_dir / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        raw_path = staging / "2026-05-01.parquet"
        _sample_df().write_parquet(str(raw_path))

        result = ingestion._ingest_date("2026-05-01", raw_path)
        assert result is not None
        assert result.exists()
        assert (result / "data.parquet").exists()

        # Sync state updated
        assert "2026-05-01" in ingestion.sync_state.partitions()
        assert ingestion.sync_state.total_rows() == 6

        # Raw file cleaned
        assert not raw_path.exists()

        # Verify data is sorted by symbol within partition
        df = pl.read_parquet(str(result / "data.parquet"))
        symbols = df["symbol"].to_list()
        assert symbols == sorted(symbols)

    def test_ingest_with_symbol_filter(self, tmp_path):
        data_dir = tmp_path / "tesseract_data"
        ingestion = TesseractIngestion(
            data_dir=data_dir, api_key="test-key", symbol_filter={"AAPL"}
        )

        staging = data_dir / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        raw_path = staging / "2026-05-01.parquet"
        _sample_df().write_parquet(str(raw_path))

        result = ingestion._ingest_date("2026-05-01", raw_path)
        df = pl.read_parquet(str(result / "data.parquet"))
        assert df["symbol"].unique().to_list() == ["AAPL"]
        assert len(df) == 2  # AAPL appears on both dates

    def test_clean_staging(self, tmp_path):
        data_dir = tmp_path / "tesseract_data"
        ingestion = TesseractIngestion(data_dir=data_dir, api_key="test-key")
        # Create some staging cruft
        staging = data_dir / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "orphan.parquet").write_text("junk")
        (staging / "subdir").mkdir()
        (staging / "subdir" / "file.txt").write_text("junk")

        ingestion._clean_staging()
        assert not staging.exists() or not list(staging.iterdir())


# ═══════════════════════════════════════════════════════════════════════════════
# ParquetTransport
# ═══════════════════════════════════════════════════════════════════════════════


class TestParquetTransport:
    @pytest.fixture
    def populated_dir(self, tmp_path):
        """A data dir with two ingested partitions."""
        data_dir = tmp_path / "tesseract_data"
        # Write partitions
        _write_partition(data_dir, "2026-05-01", _sample_df().filter(pl.col("date") == "2026-05-01"))
        _write_partition(data_dir, "2026-05-02", _sample_df().filter(pl.col("date") == "2026-05-02"))
        # Create sync state
        ss = SyncState(data_dir)
        ss.record_ingestion("2026-05-01", rows=3, files=1, source_url="https://bulk.test")
        ss.record_ingestion("2026-05-02", rows=3, files=1, source_url="https://bulk.test")
        return data_dir

    def test_scan_date_returns_lazyframe(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        lf = pt.scan_date("2026-05-01")
        assert lf is not None
        df = lf.collect()
        assert len(df) == 3
        assert set(df["symbol"].to_list()) == {"AAPL", "MSFT", "GOOGL"}

    def test_scan_date_missing_without_degraded(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        lf = pt.scan_date("2026-05-03", degraded=False)
        df = lf.collect()
        assert df.is_empty()

    def test_scan_date_missing_with_degraded(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        lf = pt.scan_date("2026-05-03", degraded=True)
        df = lf.collect()
        assert not df.is_empty()
        assert len(df) == 3  # falls back to 2026-05-02

    def test_predicate_pushdown_symbol_filter(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        lf = pt.scan_date("2026-05-01", symbols={"AAPL"})
        df = lf.collect()
        assert len(df) == 1
        assert df["symbol"][0] == "AAPL"

    def test_scan_range_concat(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        lf = pt.scan_range("2026-05-01", "2026-05-02")
        df = lf.collect()
        assert len(df) == 6

    def test_features_for_symbol(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        features = pt.features_for_symbol("MSFT", lookback_days=63)
        assert len(features) == 2
        assert all(f.symbol == "MSFT" for f in features)
        assert all(isinstance(f, TesseractFeature) for f in features)

    def test_features_for_symbol_with_provenance(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        features = pt.features_for_symbol("AAPL", lookback_days=5)
        for f in features:
            assert f.provenance == "massive/tesseract/v1"
            assert f.as_of != ""
            assert f.confidence is not None

    def test_latest_prediction(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        pred = pt.latest_prediction("AAPL", horizon="1d")
        assert pred is not None
        assert pred["symbol"] == "AAPL"
        assert pred["date"] == "2026-05-02"  # most recent
        assert pred["prediction"] == 192.1
        assert pred["provenance"] == "massive/tesseract/v1"
        assert "staleness_tier" in pred

    def test_latest_prediction_different_horizon(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        pred = pt.latest_prediction("GOOGL", horizon="5d")
        assert pred is not None
        assert pred["prediction"] == 181.0
        assert pred["horizon"] == "5d"

    def test_latest_predictions_batch(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        results = pt.latest_predictions(["AAPL", "MSFT", "GOOGL"], horizon="21d")
        assert len(results) == 3
        assert results["AAPL"]["prediction"] == 201.0
        assert results["MSFT"]["prediction"] == 452.0

    def test_symbol_coverage(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        symbols = pt.symbol_coverage()
        assert symbols == {"AAPL", "MSFT", "GOOGL"}

    def test_health(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        h = pt.health()
        assert h["partitions"] == 2
        assert h["earliest"] == "2026-05-01"
        assert h["latest"] == "2026-05-02"
        assert h["total_rows"] == 6

    def test_empty_data_dir(self, tmp_path):
        pt = ParquetTransport(tmp_path / "empty")
        assert pt.symbol_coverage() == set()
        assert pt.latest_prediction("AAPL") is None
        lf = pt.scan_date("2026-05-01", degraded=False)
        assert lf.collect().is_empty()

    def test_degraded_mode_fallback_to_earliest(self, tmp_path):
        """When requesting a date older than any partition, fall back to earliest."""
        data_dir = tmp_path / "data"
        # Create a valid partition with date 2026-06-01
        df = _sample_df().filter(pl.col("date") == "2026-05-01").with_columns(
            pl.lit("2026-06-01").alias("date"),
            pl.lit("2026-06-01T00:00:00Z").alias("as_of"),
        )
        _write_partition(data_dir, "2026-06-01", df)
        ss = SyncState(data_dir)
        ss.record_ingestion("2026-06-01", rows=len(df), files=1)

        pt = ParquetTransport(data_dir, sync_state=ss)
        lf = pt.scan_date("2025-01-01", degraded=True)
        result = lf.collect()
        assert not result.is_empty()
        assert result["date"][0] == "2026-06-01"

    def test_columns_projection_includes_provenance(self, populated_dir):
        pt = ParquetTransport(populated_dir)
        lf = pt.scan_date("2026-05-01", columns=["symbol", "close"])
        df = lf.collect()
        # Provenance columns always included
        assert "provenance" in df.columns
        assert "as_of" in df.columns
        assert "confidence" in df.columns
        assert "ingested_at" in df.columns
        assert "symbol" in df.columns
        assert "close" in df.columns


# ═══════════════════════════════════════════════════════════════════════════════
# Degraded-mode & edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestDegradedMode:
    def test_no_partitions_at_all(self, tmp_path):
        pt = ParquetTransport(tmp_path / "nonexistent")
        lf = pt.scan_date("2026-05-01", degraded=True)
        df = lf.collect()
        assert df.is_empty()
        assert pt.latest_prediction("AAPL") is None

    def test_fallback_when_only_older_data_exists(self, tmp_path):
        data_dir = tmp_path / "data"
        df = _sample_df().filter(pl.col("date") == "2026-05-01")
        _write_partition(data_dir, "2026-05-01", df)
        ss = SyncState(data_dir)
        ss.record_ingestion("2026-05-01", rows=3, files=1)

        pt = ParquetTransport(data_dir, sync_state=ss)
        # Request a date AFTER the only partition
        lf = pt.scan_date("2026-05-10", degraded=True)
        result = lf.collect()
        assert not result.is_empty()

    def test_staleness_tier_on_prediction(self, tmp_path):
        """Verify staleness_tier appears on prediction results."""
        data_dir = tmp_path / "data"
        _write_partition(data_dir, "2026-05-01", _sample_df().filter(pl.col("date") == "2026-05-01"))
        ss = SyncState(data_dir)
        ss.record_ingestion("2026-05-01", rows=3, files=1)
        pt = ParquetTransport(data_dir, sync_state=ss)
        pred = pt.latest_prediction("AAPL", horizon="1d")
        assert pred is not None
        assert "staleness_tier" in pred
        # as_of is the partition date, which is in the past
        assert pred["staleness_tier"] in ("fresh", "recent", "aging", "stale")


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: Ingestion → ParquetTransport round-trip
# ═══════════════════════════════════════════════════════════════════════════════


class TestIntegration:
    def test_ingest_then_query(self, tmp_path):
        data_dir = tmp_path / "tesseract_data"
        ingestion = TesseractIngestion(data_dir=data_dir, api_key="test-key")

        # Simulate what _ingest_date does by writing directly and
        # recording sync state manually (so we don't need network).
        df = _sample_df()
        for d in ("2026-05-01", "2026-05-02"):
            _write_partition(data_dir, d, df.filter(pl.col("date") == d))
            ingestion.sync_state.record_ingestion(d, rows=3, files=1)

        pt = ParquetTransport(data_dir, sync_state=ingestion.sync_state)
        features = pt.features_for_symbol("GOOGL", lookback_days=63)
        assert len(features) == 2
        assert features[0].sentiment_score is not None
        assert features[1].sentiment_score is not None
        # sentiment values from sample: 0.45, 0.41
        scores = [f.sentiment_score for f in features]
        assert 0.45 in scores
        assert 0.41 in scores

    def test_provenance_columns_survive_roundtrip(self, tmp_path):
        data_dir = tmp_path / "tesseract_data"
        df = _sample_df().filter(pl.col("date") == "2026-05-01")
        _write_partition(data_dir, "2026-05-01", df)
        ss = SyncState(data_dir)
        ss.record_ingestion("2026-05-01", rows=3, files=1)

        pt = ParquetTransport(data_dir, sync_state=ss)
        lf = pt.scan_date("2026-05-01")
        result = lf.collect()

        # Every row must have all provenance columns non-null (except confidence may be null)
        for col_name in ("provenance", "as_of", "ingested_at"):
            assert col_name in result.columns
            assert result[col_name].null_count() == 0

    def test_sorted_by_symbol_within_partition(self, tmp_path):
        """Verify partition data is sorted by symbol (not date)."""
        data_dir = tmp_path / "tesseract_data"
        # Create unsorted data
        df = pl.DataFrame(
            {
                "symbol": ["MSFT", "AAPL", "GOOGL"],
                "date": ["2026-05-01", "2026-05-01", "2026-05-01"],
                "close": [425.0, 190.0, 175.0],
                "volume": [22_000_000, 50_000_000, 18_000_000],
                "prediction_1d": [428.0, 191.2, 176.5],
                "prediction_5d": [435.0, 194.0, 180.0],
                "prediction_21d": [450.0, 200.0, 185.0],
                "prediction_63d": [475.0, 210.0, 195.0],
                "sentiment_score": [0.72, 0.65, 0.45],
                "momentum_score": [0.60, 0.55, 0.30],
                "volatility_est": [0.22, 0.18, 0.25],
                "regime": ["bull", "bull", "neutral"],
                "provenance": ["massive/tesseract/v1"] * 3,
                "as_of": ["2026-05-01T00:00:00Z"] * 3,
                "confidence": [0.93, 0.95, 0.88],
                "ingested_at": ["2026-06-05T03:00:00+00:00"] * 3,
            }
        )

        staging = data_dir / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        raw_path = staging / "2026-05-01.parquet"
        df.write_parquet(str(raw_path))

        ingestion = TesseractIngestion(data_dir=data_dir, api_key="test-key")
        result = ingestion._ingest_date("2026-05-01", raw_path)

        result_df = pl.read_parquet(str(result / "data.parquet"))
        assert result_df["symbol"].to_list() == ["AAPL", "GOOGL", "MSFT"]


# ═══════════════════════════════════════════════════════════════════════════════
# TesseractFacade — Strategy-pattern provider split
# ═══════════════════════════════════════════════════════════════════════════════


class TestTesseractFacade:
    """Tests for the TesseractFacade strategy-pattern split (REST ↔ Parquet)."""

    @pytest.fixture
    def facade_with_data(self, tmp_path):
        """Facade with local parquet data (no live REST)."""
        from ic_engine.providers.tesseract.facade import TesseractFacade

        data_dir = tmp_path / "tesseract_data"
        # Write two partitions
        _write_partition(
            data_dir, "2026-05-01",
            _sample_df().filter(pl.col("date") == "2026-05-01"),
        )
        _write_partition(
            data_dir, "2026-05-02",
            _sample_df().filter(pl.col("date") == "2026-05-02"),
        )
        ss = SyncState(data_dir)
        ss.record_ingestion("2026-05-01", rows=3, files=1,
                            source_url="https://bulk.test")
        ss.record_ingestion("2026-05-02", rows=3, files=1,
                            source_url="https://bulk.test")

        facade = TesseractFacade(data_dir=data_dir, api_key="test-key")
        return facade

    def test_get_features_returns_typed_objects(self, facade_with_data):
        features = facade_with_data.get_features("AAPL", lookback_days=63)
        assert len(features) == 2
        for f in features:
            assert isinstance(f, TesseractFeature)
            assert f.symbol == "AAPL"
            assert f.provenance == "massive/tesseract/v1"

    def test_latest_prediction_with_provenance(self, facade_with_data):
        pred = facade_with_data.latest_prediction("MSFT", horizon="1d")
        assert pred is not None
        assert pred["symbol"] == "MSFT"
        assert pred["provenance"] == "massive/tesseract/v1"
        assert "staleness_tier" in pred
        assert "confidence" in pred

    def test_latest_predictions_batch(self, facade_with_data):
        results = facade_with_data.latest_predictions(
            ["AAPL", "MSFT", "GOOGL"], horizon="21d"
        )
        assert len(results) == 3
        assert results["AAPL"]["prediction"] == 201.0
        assert results["MSFT"]["prediction"] == 452.0

    def test_get_sentiment_returns_provenance(self, facade_with_data):
        rows = facade_with_data.get_sentiment("GOOGL", lookback_days=63)
        assert len(rows) == 2
        for r in rows:
            assert r["symbol"] == "GOOGL"
            assert r["provenance"] == "massive/tesseract/v1"
            assert "staleness" in r
            assert r["staleness"] in ("fresh", "recent", "aging", "stale", "unknown")

    def test_get_regime_returns_latest(self, facade_with_data):
        regime = facade_with_data.get_regime("AAPL", lookback_days=63)
        assert regime is not None
        assert regime["symbol"] == "AAPL"
        assert regime["regime"] == "bull"
        assert regime["provenance"] == "massive/tesseract/v1"

    def test_get_volatility_returns_estimate(self, facade_with_data):
        vol = facade_with_data.get_volatility("MSFT", lookback_days=63)
        assert vol is not None
        assert vol["symbol"] == "MSFT"
        assert vol["volatility_30d_est"] == 0.21  # most recent
        assert vol["provenance"] == "massive/tesseract/v1"

    def test_get_regime_none_for_missing_symbol(self, facade_with_data):
        regime = facade_with_data.get_regime("NONEXISTENT", lookback_days=63)
        assert regime is None

    def test_health_reports_all_layers(self, facade_with_data):
        h = facade_with_data.health()
        assert "rest" in h
        assert h["rest"] == "massive"
        assert "parquet" in h
        assert h["parquet"]["partitions"] == 2
        assert "sync" in h
        assert len(h["sync"]["partitions"]) == 2

    def test_symbol_coverage(self, facade_with_data):
        cov = facade_with_data.symbol_coverage()
        assert cov == {"AAPL", "MSFT", "GOOGL"}

    def test_partitions_list(self, facade_with_data):
        parts = facade_with_data.partitions()
        assert parts == ["2026-05-01", "2026-05-02"]

    def test_facade_name_matches_provider(self, facade_with_data):
        assert facade_with_data.NAME == "massive"

    def test_sync_daily_writes_partition(self, tmp_path):
        """Verify sync_daily creates partition via ingestion pipeline."""
        from ic_engine.providers.tesseract.facade import TesseractFacade

        data_dir = tmp_path / "tesseract_data"
        facade = TesseractFacade(data_dir=data_dir, api_key="test-key")

        # Manually stage a raw parquet as if downloaded
        staging = data_dir / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        raw_path = staging / "2026-06-01.parquet"
        _sample_df().filter(pl.col("date") == "2026-05-01").with_columns(
            pl.lit("2026-06-01").alias("date"),
            pl.lit("2026-06-01T00:00:00Z").alias("as_of"),
        ).write_parquet(str(raw_path))

        # Ingest via facade's internal ingestion engine
        result = facade.ingest._ingest_date("2026-06-01", raw_path)
        assert result is not None
        assert (result / "data.parquet").exists()
        assert "2026-06-01" in facade.ingest.sync_state.partitions()


# ═══════════════════════════════════════════════════════════════════════════════
# Forex / Crypto flat-file stubs — prove the architecture extends beyond equities
# ═══════════════════════════════════════════════════════════════════════════════


class TestTesseractForexCrypto:
    """Verify Tesseract ingestion + transport accept forex/crypto data shapes."""

    def _forex_df(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "symbol": ["EUR/USD", "GBP/USD", "USD/JPY"],
                "date": ["2026-06-01", "2026-06-01", "2026-06-01"],
                "close": [1.085, 1.275, 145.30],
                "volume": [0, 0, 0],
                "prediction_1d": [1.086, 1.277, 145.10],
                "prediction_5d": [1.090, 1.282, 144.80],
                "prediction_21d": [1.100, 1.295, 144.00],
                "prediction_63d": [1.120, 1.310, 142.50],
                "sentiment_score": [0.55, 0.48, -0.32],
                "momentum_score": [0.42, 0.35, -0.28],
                "volatility_est": [0.08, 0.09, 0.12],
                "regime": ["bull", "neutral", "bear"],
                "provenance": ["massive/tesseract/v1"] * 3,
                "as_of": ["2026-06-01T00:00:00Z"] * 3,
                "confidence": [0.91, 0.89, 0.93],
                "ingested_at": ["2026-06-05T03:00:00+00:00"] * 3,
            }
        )

    def _crypto_df(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "symbol": ["BTC/USD", "ETH/USD", "SOL/USD"],
                "date": ["2026-06-01", "2026-06-01", "2026-06-01"],
                "close": [87500.0, 4200.0, 185.0],
                "volume": [28_000_000_000, 12_000_000_000, 3_500_000_000],
                "prediction_1d": [88000.0, 4250.0, 188.0],
                "prediction_5d": [89500.0, 4400.0, 195.0],
                "prediction_21d": [92000.0, 4800.0, 210.0],
                "prediction_63d": [98000.0, 5500.0, 240.0],
                "sentiment_score": [0.72, 0.65, 0.58],
                "momentum_score": [0.68, 0.60, 0.52],
                "volatility_est": [0.35, 0.42, 0.55],
                "regime": ["bull", "bull", "bull"],
                "provenance": ["massive/tesseract/v1"] * 3,
                "as_of": ["2026-06-01T00:00:00Z"] * 3,
                "confidence": [0.88, 0.85, 0.82],
                "ingested_at": ["2026-06-05T03:00:00+00:00"] * 3,
            }
        )

    def test_forex_ingestion_normalises(self, tmp_path):
        data_dir = tmp_path / "fx_data"
        ingestion = TesseractIngestion(data_dir=data_dir, api_key="test-key")

        staging = data_dir / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        raw_path = staging / "2026-06-01.parquet"
        self._forex_df().write_parquet(str(raw_path))

        result = ingestion._ingest_date("2026-06-01", raw_path)
        assert result is not None

        df = pl.read_parquet(str(result / "data.parquet"))
        assert "EUR/USD" in df["symbol"].to_list()
        assert "provenance" in df.columns

    def test_crypto_ingestion_normalises(self, tmp_path):
        data_dir = tmp_path / "crypto_data"
        ingestion = TesseractIngestion(data_dir=data_dir, api_key="test-key")

        staging = data_dir / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        raw_path = staging / "2026-06-01.parquet"
        self._crypto_df().write_parquet(str(raw_path))

        result = ingestion._ingest_date("2026-06-01", raw_path)
        assert result is not None

        df = pl.read_parquet(str(result / "data.parquet"))
        assert "BTC/USD" in df["symbol"].to_list()
        assert "provenance" in df.columns

    def test_forex_prediction_query(self, tmp_path):
        data_dir = tmp_path / "fx_data"
        _write_partition(data_dir, "2026-06-01", self._forex_df())
        ss = SyncState(data_dir)
        ss.record_ingestion("2026-06-01", rows=3, files=1)

        pt = ParquetTransport(data_dir, sync_state=ss)
        pred = pt.latest_prediction("EUR/USD", horizon="1d")
        assert pred is not None
        assert pred["symbol"] == "EUR/USD"
        assert pred["prediction"] == 1.086
        assert pred["provenance"] == "massive/tesseract/v1"

    def test_crypto_prediction_query(self, tmp_path):
        data_dir = tmp_path / "crypto_data"
        _write_partition(data_dir, "2026-06-01", self._crypto_df())
        ss = SyncState(data_dir)
        ss.record_ingestion("2026-06-01", rows=3, files=1)

        pt = ParquetTransport(data_dir, sync_state=ss)
        pred = pt.latest_prediction("BTC/USD", horizon="63d")
        assert pred is not None
        assert pred["symbol"] == "BTC/USD"
        assert pred["prediction"] == 98000.0
        assert pred["horizon"] == "63d"
