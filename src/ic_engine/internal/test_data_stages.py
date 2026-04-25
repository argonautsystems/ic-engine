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
Unit tests for Phase 2 data stages.

Scope:
* DataDownloader: cache-key determinism, cache-hit reuse, rate limiting,
  retry behaviour, 404-skip behaviour, stats accounting.
* DataExtractor:  parquet + JSON parsing, integrity checks (empty, NaN),
  missing-symbol detection.
* DataTransformer: field mapping, type coercion, derived fields,
  drop/add columns, missing-rule pass-through.

All tests use provider adapters mocked via DataDownloader._invoke_adapter,
so no real network calls are made.

Run with::

    python -m unittest internal.test_data_stages
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import polars as pl

from ic_engine.internal.data_downloader import (
    DEFAULT_PROVIDER_CONFIGS,
    DataDownloader,
    DataProviderConfig,
    _ProviderError,
)
from ic_engine.internal.data_extractor import (
    DEFAULT_SCHEMAS,
    DataExtractor,
)
from ic_engine.internal.data_transformer import DataTransformer, TransformConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmp_downloader(tmp: Path, **override) -> DataDownloader:
    """DataDownloader pointed at a tmp cache dir, with shorter retry delays."""
    cfg = DataProviderConfig(
        provider_name="yfinance",
        rate_limit_delay=0.0,
        retry_delay=0.0,
        retry_count=2,
        cache_max_age_hours=24.0,
        **override,
    )
    return DataDownloader(
        cache_dir=tmp,
        provider_configs={"yfinance": cfg},
    )


def _fake_pandas_df(rows: int = 3):
    import pandas as pd

    return pd.DataFrame(
        {
            "Open": [1.0 + i for i in range(rows)],
            "High": [2.0 + i for i in range(rows)],
            "Low": [0.5 + i for i in range(rows)],
            "Close": [1.5 + i for i in range(rows)],
            "Volume": [100 + i for i in range(rows)],
        }
    )


# ---------------------------------------------------------------------------
# DataDownloader
# ---------------------------------------------------------------------------


class TestDataDownloader(unittest.TestCase):
    def test_cache_key_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _tmp_downloader(Path(tmp))
            k1 = d._cache_key("yfinance", "AAPL", ("2024-01-01", "2024-12-31"))
            k2 = d._cache_key("yfinance", "AAPL", ("2024-01-01", "2024-12-31"))
            k3 = d._cache_key("yfinance", "MSFT", ("2024-01-01", "2024-12-31"))
            self.assertEqual(k1, k2)
            self.assertNotEqual(k1, k3)

    def test_cache_hit_skips_adapter(self):
        """Second call for the same (symbol, range) must not re-invoke adapter."""
        with tempfile.TemporaryDirectory() as tmp:
            d = _tmp_downloader(Path(tmp))

            call_count = {"n": 0}

            def fake_invoke(cfg, symbol, date_range):
                call_count["n"] += 1
                return _fake_pandas_df()

            with patch.object(d, "_invoke_adapter", side_effect=fake_invoke):
                r1 = d.download("yfinance", ["AAPL"], ("2024-01-01", "2024-12-31"))
                r2 = d.download("yfinance", ["AAPL"], ("2024-01-01", "2024-12-31"))

            self.assertTrue(r1["AAPL"].success)
            self.assertFalse(r1["AAPL"].cache_hit)
            self.assertTrue(r2["AAPL"].cache_hit)
            self.assertEqual(
                call_count["n"], 1, "Adapter must be called only once (cache hit on 2nd)"
            )
            self.assertEqual(d.stats.cache_hits, 1)
            self.assertEqual(d.stats.cache_misses, 1)

    def test_retry_on_transient_failure(self):
        """Adapter failing twice, then succeeding — should retry and win."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = DataProviderConfig(
                provider_name="yfinance",
                rate_limit_delay=0.0,
                retry_delay=0.0,
                retry_count=3,
                cache_max_age_hours=24.0,
            )
            d = DataDownloader(cache_dir=Path(tmp), provider_configs={"yfinance": cfg})

            attempts = {"n": 0}

            def flaky(cfg_, symbol, date_range):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise _ProviderError("transient 503", 503)
                return _fake_pandas_df()

            with patch.object(d, "_invoke_adapter", side_effect=flaky):
                results = d.download("yfinance", ["AAPL"], ("2024-01-01", "2024-12-31"))
            self.assertTrue(results["AAPL"].success)
            self.assertEqual(results["AAPL"].attempts, 3)
            self.assertEqual(d.stats.retries, 2)

    def test_skip_on_404(self):
        """404 must be recorded as skipped (not a hard failure)."""
        with tempfile.TemporaryDirectory() as tmp:
            d = _tmp_downloader(Path(tmp))

            def not_found(cfg, symbol, date_range):
                raise _ProviderError("404 not found", 404)

            with patch.object(d, "_invoke_adapter", side_effect=not_found):
                results = d.download("yfinance", ["ZZZZ"], ("2024-01-01", "2024-12-31"))
            self.assertTrue(results["ZZZZ"].skipped)
            self.assertFalse(results["ZZZZ"].success)
            self.assertEqual(d.stats.skipped_404, 1)
            self.assertEqual(d.stats.failures, 0)

    def test_failure_after_retries_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _tmp_downloader(Path(tmp))

            def always_fail(cfg, symbol, date_range):
                raise _ProviderError("503 still down", 503)

            with patch.object(d, "_invoke_adapter", side_effect=always_fail):
                results = d.download("yfinance", ["AAPL"], ("2024-01-01", "2024-12-31"))
            self.assertFalse(results["AAPL"].success)
            self.assertIsNotNone(results["AAPL"].error)
            self.assertEqual(d.stats.failures, 1)

    def test_rate_limit_applies_between_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = DataProviderConfig(
                provider_name="yfinance",
                rate_limit_delay=0.15,  # small but measurable
                retry_delay=0.0,
                retry_count=1,
                cache_max_age_hours=0.0,  # disable cache so both requests run
            )
            d = DataDownloader(cache_dir=Path(tmp), provider_configs={"yfinance": cfg})
            with patch.object(d, "_invoke_adapter", side_effect=lambda *a, **k: _fake_pandas_df()):
                start = time.monotonic()
                d.download("yfinance", ["AAPL", "MSFT"], ("2024-01-01", "2024-12-31"))
                elapsed = time.monotonic() - start
            # At least one rate-limit gap of ~0.15s should have been enforced.
            self.assertGreaterEqual(elapsed, 0.14)

    def test_clear_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _tmp_downloader(Path(tmp))
            with patch.object(d, "_invoke_adapter", side_effect=lambda *a, **k: _fake_pandas_df()):
                d.download("yfinance", ["AAPL", "MSFT"], ("2024-01-01", "2024-12-31"))
            # Two .bin + two .parquet files = 4
            self.assertGreaterEqual(len(list(Path(tmp).glob("*"))), 2)
            removed = d.clear_cache(provider="yfinance")
            self.assertGreaterEqual(removed, 2)

    def test_default_provider_configs_present(self):
        """Regression: make sure we ship sensible defaults for all providers."""
        for p in ("yfinance", "finnhub", "polygon", "alphavantage", "newsapi"):
            self.assertIn(p, DEFAULT_PROVIDER_CONFIGS)
            cfg = DEFAULT_PROVIDER_CONFIGS[p]
            self.assertGreater(cfg.retry_count, 0)
            self.assertGreaterEqual(cfg.rate_limit_delay, 0.0)


# ---------------------------------------------------------------------------
# DataExtractor
# ---------------------------------------------------------------------------


class TestDataExtractor(unittest.TestCase):
    def _write_pandas_parquet(self, path: Path, df) -> None:
        df.to_parquet(path.with_suffix(".parquet"), index=True)
        path.write_bytes(path.with_suffix(".parquet").read_bytes())

    def test_extract_parquet_ohlcv_passes_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path_aapl = tmp / "yf_AAPL.bin"
            self._write_pandas_parquet(path_aapl, _fake_pandas_df(5))

            extractor = DataExtractor()
            result = extractor.extract(
                {"AAPL": path_aapl},
                schema=DEFAULT_SCHEMAS["yfinance_ohlcv"],
            )
            self.assertIn("AAPL", result.data)
            self.assertTrue(result.integrity_valid)
            self.assertEqual(result.statistics["AAPL"], 5)
            self.assertIn("_symbol", result.data["AAPL"].columns)

    def test_extract_flags_empty_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            import pandas as pd

            path = tmp / "empty.bin"
            self._write_pandas_parquet(path, pd.DataFrame(columns=list(_fake_pandas_df(1).columns)))
            extractor = DataExtractor()
            result = extractor.extract(
                {"EMPTY": path},
                schema=DEFAULT_SCHEMAS["yfinance_ohlcv"],
            )
            self.assertIn("EMPTY", result.missing_symbols)
            self.assertFalse(result.integrity_valid)

    def test_extract_missing_path(self):
        extractor = DataExtractor()
        result = extractor.extract(
            {"GHOST": Path("/nonexistent/path.bin")},
            schema=DEFAULT_SCHEMAS["yfinance_ohlcv"],
        )
        self.assertIn("GHOST", result.missing_symbols)

    def test_extract_json_newsapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news.bin"
            payload = {
                "status": "ok",
                "articles": [
                    {
                        "title": "AAPL beats earnings",
                        "description": "Details...",
                        "publishedAt": "2024-01-15T10:00:00Z",
                        "url": "https://example.com/1",
                        "source": {"name": "Reuters"},
                    },
                    {
                        "title": "AAPL product launch",
                        "description": "More details",
                        "publishedAt": "2024-02-01T12:00:00Z",
                        "url": "https://example.com/2",
                        "source": {"name": "Bloomberg"},
                    },
                ],
            }
            path.write_text(json.dumps(payload))
            extractor = DataExtractor()
            result = extractor.extract(
                {"AAPL": path},
                schema=DEFAULT_SCHEMAS["newsapi_everything"],
            )
            self.assertIn("AAPL", result.data)
            self.assertEqual(result.statistics["AAPL"], 2)

    def test_extract_json_finnhub_parallel_arrays(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "finnhub.bin"
            payload = {
                "s": "ok",
                "c": [150.0, 151.0, 152.0],
                "h": [151.5, 152.5, 153.5],
                "l": [149.0, 150.0, 151.0],
                "o": [150.5, 151.0, 152.0],
                "t": [1704067200, 1704153600, 1704240000],
                "v": [1_000_000, 1_100_000, 1_200_000],
            }
            path.write_text(json.dumps(payload))
            extractor = DataExtractor()
            result = extractor.extract(
                {"AAPL": path},
                schema=DEFAULT_SCHEMAS["finnhub_candles"],
            )
            self.assertIn("AAPL", result.data)
            self.assertEqual(result.statistics["AAPL"], 3)
            self.assertTrue(result.integrity_valid)


# ---------------------------------------------------------------------------
# DataTransformer
# ---------------------------------------------------------------------------


class TestDataTransformer(unittest.TestCase):
    @staticmethod
    def _yf_like_df():
        return pl.DataFrame(
            {
                "Open": [100.0, 101.0, 102.0],
                "High": [101.0, 102.0, 103.0],
                "Low": [99.0, 100.0, 101.0],
                "Close": [100.5, 101.5, 102.5],
                "Adj Close": [100.5, 101.5, 102.5],
                "Volume": [1_000_000, 1_100_000, 1_200_000],
                "Dividends": [0.0, 0.0, 0.0],
                "Stock Splits": [0.0, 0.0, 0.0],
                "_symbol": ["AAPL", "AAPL", "AAPL"],
            }
        )

    def test_transform_yfinance_canonical_schema(self):
        """yfinance raw → canonical schema via the shipped config."""
        rules_path = Path(__file__).resolve().parent.parent / "config" / "data_transform_rules.json"
        self.assertTrue(rules_path.exists(), f"Expected rules file at {rules_path}")
        transformer = DataTransformer.from_rules_file(rules_path)
        out = transformer.transform(
            {"AAPL": self._yf_like_df()},
            source="yfinance_ohlcv",
        )
        df = out["AAPL"]
        for expected in ("open", "high", "low", "close", "adj_close", "volume", "symbol", "source"):
            self.assertIn(expected, df.columns, f"Missing column {expected}")
        # Dropped columns
        self.assertNotIn("Dividends", df.columns)
        self.assertNotIn("Stock Splits", df.columns)
        # Literal added column
        self.assertEqual(df["source"].to_list()[0], "yfinance")
        # Type coerced
        self.assertEqual(df["close"].dtype, pl.Float64)
        self.assertEqual(df["volume"].dtype, pl.Int64)

    def test_derived_from_epoch(self):
        """from_epoch:_timestamp must convert Unix seconds to Date."""
        df = pl.DataFrame(
            {
                "o": [150.0],
                "h": [151.0],
                "l": [149.0],
                "c": [150.5],
                "v": [1_000_000],
                "t": [1704067200],
                "_symbol": ["AAPL"],
                "s": ["ok"],
            }
        )
        rules_path = Path(__file__).resolve().parent.parent / "config" / "data_transform_rules.json"
        transformer = DataTransformer.from_rules_file(rules_path)
        out = transformer.transform({"AAPL": df}, source="finnhub_candles")
        result = out["AAPL"]
        self.assertIn("date", result.columns)
        self.assertEqual(result["date"].dtype, pl.Date)
        self.assertIn("adj_close", result.columns)
        self.assertEqual(result["adj_close"].to_list(), result["close"].to_list())

    def test_missing_rule_passes_through(self):
        """Unknown source should pass through with a warning, not crash."""
        transformer = DataTransformer(rules={})
        df = pl.DataFrame({"x": [1, 2, 3]})
        out = transformer.transform({"X": df}, source="nonexistent_source")
        # Pandas equality check via shape + column
        self.assertIn("X", out)
        self.assertEqual(out["X"].shape, (3, 1))

    def test_inline_transform_config(self):
        """TransformConfig constructed directly (no JSON file)."""
        rule = TransformConfig(
            source="custom",
            field_mappings={"A": "alpha", "B": "beta"},
            type_coercions={"alpha": "float64", "beta": "int64"},
            add_columns={"source": "custom"},
        )
        transformer = DataTransformer(rules={"custom": rule})
        df = pl.DataFrame({"A": ["1.5", "2.5"], "B": ["10", "20"]})
        out = transformer.transform({"S": df}, source="custom")
        result = out["S"]
        self.assertIn("alpha", result.columns)
        self.assertIn("beta", result.columns)
        self.assertEqual(result["alpha"].dtype, pl.Float64)
        self.assertEqual(result["beta"].dtype, pl.Int64)
        self.assertEqual(result["source"].to_list(), ["custom", "custom"])


# ---------------------------------------------------------------------------
# End-to-end wiring test
# ---------------------------------------------------------------------------


class TestEndToEnd(unittest.TestCase):
    """Smoke test: downloader → extractor → transformer, fully mocked."""

    def test_three_stage_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            downloader = _tmp_downloader(tmp)

            with patch.object(
                downloader, "_invoke_adapter", side_effect=lambda *a, **k: _fake_pandas_df(4)
            ):
                dl_results = downloader.download(
                    "yfinance",
                    ["AAPL", "MSFT"],
                    ("2024-01-01", "2024-12-31"),
                )

            # Build symbol → cache path map (skip failures)
            raw = {sym: r.cache_path for sym, r in dl_results.items() if r.success}
            self.assertEqual(len(raw), 2)

            extractor = DataExtractor()
            extracted = extractor.extract(raw, schema=DEFAULT_SCHEMAS["yfinance_ohlcv"])
            self.assertTrue(extracted.integrity_valid)
            self.assertEqual(len(extracted.data), 2)

            rules_path = (
                Path(__file__).resolve().parent.parent / "config" / "data_transform_rules.json"
            )
            transformer = DataTransformer.from_rules_file(rules_path)
            standardized = transformer.transform(extracted.data, source="yfinance_ohlcv")

            for sym, df in standardized.items():
                for col in ("open", "high", "low", "close", "volume", "source"):
                    self.assertIn(col, df.columns)
                self.assertEqual(df["source"].to_list()[0], "yfinance")


if __name__ == "__main__":
    unittest.main(verbosity=2)
