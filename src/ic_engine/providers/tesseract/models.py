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
Tesseract data models — provenance-annotated ML feature and prediction records.

Every record carries:
  * ``provenance`` — source identifier (e.g. "massive/tesseract/v1")
  * ``as_of``     — UTC timestamp when the source generated this value
  * ``confidence``— source-reported confidence (0.0–1.0, or None if absent)
  * ``ingested_at``— UTC timestamp when this partition was ingested locally

The schema is enforced so callers cannot silently drop provenance columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ── Column names reserved for provenance — must appear in every parquet file ──

PROVENANCE_COL = "provenance"
AS_OF_COL = "as_of"
CONFIDENCE_COL = "confidence"
INGESTED_AT_COL = "ingested_at"

PROVENANCE_COLUMNS = frozenset(
    {PROVENANCE_COL, AS_OF_COL, CONFIDENCE_COL, INGESTED_AT_COL}
)

# Core feature columns produced by Massive Tesseract bulk feed
CORE_FEATURE_COLUMNS = frozenset(
    {
        "symbol",          # ticker
        "date",            # trade / prediction date
        "close",           # adjusted close
        "volume",          # volume
        "prediction_1d",   # 1-day forward price prediction
        "prediction_5d",   # 5-day forward
        "prediction_21d",  # 21-day forward
        "prediction_63d",  # 63-day (quarterly) forward
        "sentiment_score", # ML sentiment [-1, 1]
        "momentum_score",  # ML momentum signal
        "volatility_est",  # estimated 30d forward vol
        "regime",          # market regime label
    }
)

REQUIRED_COLUMNS = CORE_FEATURE_COLUMNS | PROVENANCE_COLUMNS


@dataclass(frozen=True)
class TesseractFeature:
    """One row from the Tesseract bulk parquet feed.

    All fields are verbatim from source — no imputation or derived values.
    """

    symbol: str
    date: str  # YYYY-MM-DD
    close: Optional[float] = None
    volume: Optional[int] = None
    prediction_1d: Optional[float] = None
    prediction_5d: Optional[float] = None
    prediction_21d: Optional[float] = None
    prediction_63d: Optional[float] = None
    sentiment_score: Optional[float] = None
    momentum_score: Optional[float] = None
    volatility_est: Optional[float] = None
    regime: Optional[str] = None

    # Provenance (verbatim from source)
    provenance: str = ""
    as_of: str = ""  # ISO-8601 UTC
    confidence: Optional[float] = None
    ingested_at: str = ""  # ISO-8601 UTC

    @property
    def staleness_days(self) -> Optional[int]:
        """Days since ``as_of`` relative to now. None when as_of is unset."""
        if not self.as_of:
            return None
        try:
            ts = datetime.fromisoformat(self.as_of.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - ts
            return delta.days
        except (ValueError, TypeError):
            return None

    @property
    def is_stale(self) -> bool:
        """True when the feature record is older than the staleness threshold."""
        days = self.staleness_days
        return days is not None and days > STALENESS_THRESHOLD_DAYS


@dataclass(frozen=True)
class TesseractPrediction:
    """A single prediction distilled from Tesseract features.

    Carries the same provenance envelope so callers can attribute results.
    """

    symbol: str
    date: str
    horizon: str  # "1d", "5d", "21d", "63d"
    predicted_price: Optional[float] = None
    confidence: Optional[float] = None
    provenance: str = ""
    as_of: str = ""
    ingested_at: str = ""

    @property
    def staleness_days(self) -> Optional[int]:
        if not self.as_of:
            return None
        try:
            ts = datetime.fromisoformat(self.as_of.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - ts
            return delta.days
        except (ValueError, TypeError):
            return None

    @property
    def is_stale(self) -> bool:
        days = self.staleness_days
        return days is not None and days > STALENESS_THRESHOLD_DAYS


# ── Staleness ────────────────────────────────────────────────────────────────

STALENESS_THRESHOLD_DAYS: int = 5
"""Days after which a Tesseract row is considered stale."""

STALENESS_TIERS = {
    "fresh": 1,     # ≤1 day
    "recent": 3,    # ≤3 days
    "aging": 5,     # ≤5 days
    "stale": None,  # >5 days — degraded mode
}


def staleness_tier(days: Optional[int]) -> str:
    """Map a day count to a staleness tier label."""
    if days is None:
        return "unknown"
    if days <= 1:
        return "fresh"
    if days <= 3:
        return "recent"
    if days <= 5:
        return "aging"
    return "stale"
