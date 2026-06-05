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
Tesseract — Massive ML prediction features via flat-file bulk download API.

Design (GRAEAE consultation 1f95eefdfbdf46b797f6e82808b29100, 8-muse, score 1.0):
  * Hive-partitioned Parquet by date (sorted by symbol within file, NOT
    partitioned by symbol).
  * Polars LazyFrame ``scan_parquet`` with predicate pushdown (no DuckDB).
  * Atomic folder-rename ingestion + ``sync_state`` for crash safety.
  * Strategy-pattern provider split:
      - ``RESTTransport``  → live quotes (existing MassiveProvider surface)
      - ``ParquetTransport`` → Tesseract ML features (this module)
  * Strict anti-hallucination: provenance, ``as_of``, and ``confidence``
    columns quoted verbatim from source; no inference / imputation.
  * Staleness tagging + degraded-mode fallback to most-recent partition.

Phases:
  1. Ingestion + sync_state + atomic
  2. ParquetTransport + facade split
  3. EOD/NLQ surfacing w/ provenance enforcement
  4. Tests + degraded-mode
"""

from .facade import TesseractFacade
from .ingestion import TesseractIngestion
from .models import TesseractFeature, TesseractPrediction
from .parquet_transport import ParquetTransport
from .sync_state import SyncState

__all__ = [
    "TesseractFacade",
    "TesseractFeature",
    "TesseractIngestion",
    "TesseractPrediction",
    "ParquetTransport",
    "SyncState",
]
