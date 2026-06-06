# Copyright 2026 clio Contributors
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

"""clio.drift.detect — compare fingerprints to identify drift events.

When a new extraction's fingerprint differs from the historical pattern
for the same source URI, we want to know what changed and how serious it
is. This module compares two fingerprints (or compares one against
historical scan results) and emits a list of typed DriftEvent records.

Event types (severity defaults in parentheses):

    column_added            (warn)   New column in the schema.
    column_removed          (error)  Existing column gone — possible data loss.
    column_renamed          (warn)   Same column count, names differ.
                                     Auto-resolvable via clio.extract.schema_map.
    dtype_changed           (error)  Column kept its name but its dtype shifted.
    row_count_anomaly       (warn)   Row count outside historical [P10, P90].
    confidence_dropped      (warn)   Confidence below historical mean - 2*sigma.
    extractor_version_change (info)  clio.extract.* module version changed.

Severity is a default; callers can override per-event before persistence.

Persistence and auto-remap are downstream concerns handled by
clio.drift.remap (auto-resolve column_renamed via schema_map) and
clio.drift.alarm (surface for human review). This module is the
diagnosis layer.
"""

from __future__ import annotations

import logging
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from clio.track.fingerprint import Fingerprint
from clio.track.store import iterate

logger = logging.getLogger(__name__)


# Default thresholds. Callers can override via DriftDetector(...).
DEFAULT_ROW_COUNT_TOLERANCE_PCT = 25.0  # ±25% from historical median
DEFAULT_CONFIDENCE_SIGMA = 2.0  # drop more than 2σ below historical mean


# ============================================================================
# DriftEvent — typed record per detected change
# ============================================================================


@dataclass(frozen=True)
class DriftEvent:
    """A single detected drift event between two fingerprints.

    Drift events are detected lazily and persisted via clio.drift.store. The
    drift_id is generated per event (UUID4) since events aren't content-
    addressable the way fingerprints are — a re-comparison of the same
    pair could re-detect the same change, but each detection is a distinct
    audit record.
    """

    drift_id: str
    prior_fingerprint_id: str
    current_fingerprint_id: str
    event_type: str
    severity: str
    detection_date: datetime
    auto_resolved: bool = False
    resolution_method: Optional[str] = None
    resolution_fingerprint_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


# Event-type -> default severity mapping. Adjust here if the project-wide
# severity convention shifts.
_DEFAULT_SEVERITY: dict[str, str] = {
    "column_added": "warn",
    "column_removed": "error",
    "column_renamed": "warn",
    "dtype_changed": "error",
    "row_count_anomaly": "warn",
    "confidence_dropped": "warn",
    "extractor_version_change": "info",
}


def _new_drift_event(
    *,
    prior_id: str,
    current_id: str,
    event_type: str,
    metadata: dict,
    severity: Optional[str] = None,
) -> DriftEvent:
    return DriftEvent(
        drift_id=uuid.uuid4().hex,
        prior_fingerprint_id=prior_id,
        current_fingerprint_id=current_id,
        event_type=event_type,
        severity=severity or _DEFAULT_SEVERITY.get(event_type, "warn"),
        detection_date=datetime.now(timezone.utc),
        metadata=metadata,
    )


# ============================================================================
# Pairwise comparison
# ============================================================================


def compare(prior: Fingerprint, current: Fingerprint) -> list[DriftEvent]:
    """Compare two fingerprints; emit zero or more drift events.

    Both fingerprints are typically for the same logical source URI, but
    this isn't enforced — callers can compare across sources if they want.
    Comparison is structural, based on the metadata fields stored in the
    Fingerprint records. Value-level distribution drift (mean shifts,
    percentile movement) is not done here — that requires reading the
    actual extracted data and is reserved for a v0.2+ extension.
    """
    events: list[DriftEvent] = []

    # 1. Column-set drift (only meaningful when both fingerprints have
    #    column_fingerprint metadata, i.e. tabular sources).
    if prior.dtype_map and current.dtype_map:
        prior_cols = set(prior.dtype_map.keys())
        current_cols = set(current.dtype_map.keys())

        added = current_cols - prior_cols
        removed = prior_cols - current_cols

        # If counts match but names differ, classify as renames not add+remove.
        # The renaming candidate set is (added, removed) when |added|==|removed|.
        if added and removed and len(added) == len(removed):
            events.append(
                _new_drift_event(
                    prior_id=prior.fingerprint_id,
                    current_id=current.fingerprint_id,
                    event_type="column_renamed",
                    metadata={
                        "candidate_old_names": sorted(removed),
                        "candidate_new_names": sorted(added),
                    },
                )
            )
        else:
            if added:
                events.append(
                    _new_drift_event(
                        prior_id=prior.fingerprint_id,
                        current_id=current.fingerprint_id,
                        event_type="column_added",
                        metadata={"new_columns": sorted(added)},
                    )
                )
            if removed:
                events.append(
                    _new_drift_event(
                        prior_id=prior.fingerprint_id,
                        current_id=current.fingerprint_id,
                        event_type="column_removed",
                        metadata={"removed_columns": sorted(removed)},
                    )
                )

        # 2. Dtype drift on columns present in both schemas.
        shared = prior_cols & current_cols
        dtype_changes = {
            col: {"prior_dtype": prior.dtype_map[col], "current_dtype": current.dtype_map[col]}
            for col in shared
            if prior.dtype_map[col] != current.dtype_map[col]
        }
        if dtype_changes:
            events.append(
                _new_drift_event(
                    prior_id=prior.fingerprint_id,
                    current_id=current.fingerprint_id,
                    event_type="dtype_changed",
                    metadata={"changes": dtype_changes},
                )
            )

    # 3. Extractor-version change. Info-level; included for audit clarity.
    if prior.extractor_version != current.extractor_version:
        events.append(
            _new_drift_event(
                prior_id=prior.fingerprint_id,
                current_id=current.fingerprint_id,
                event_type="extractor_version_change",
                metadata={
                    "prior_version": prior.extractor_version,
                    "current_version": current.extractor_version,
                },
            )
        )

    return events


# ============================================================================
# Historical comparison
# ============================================================================


def detect_against_history(
    current: Fingerprint,
    *,
    lookback_days: int = 90,
    row_count_tolerance_pct: float = DEFAULT_ROW_COUNT_TOLERANCE_PCT,
    confidence_sigma: float = DEFAULT_CONFIDENCE_SIGMA,
    track_dir: Optional[Path] = None,
) -> list[DriftEvent]:
    """Compare a new fingerprint against history for the same source URI.

    Pulls all fingerprints with the same source_uri from the last
    `lookback_days` (excluding the current one), runs the structural
    pairwise compare against the most recent prior, and additionally checks:

      * row_count_anomaly: current row count outside ±row_count_tolerance_pct
        of historical median.
      * confidence_dropped: current confidence below historical mean - sigma * stdev.

    If no historical fingerprints are found, returns an empty list (the
    current fingerprint is treated as the first observation and is its own
    baseline).

    Args:
        current: The new fingerprint just written to the store.
        lookback_days: How far back to scan for historical context.
        row_count_tolerance_pct: ±% window around historical median that
            counts as "no anomaly".
        confidence_sigma: How many standard deviations below historical
            mean confidence triggers a "confidence_dropped" event.
        track_dir: Override the track-store root.

    Returns:
        List of detected drift events.
    """
    cutoff = current.extraction_date - timedelta(days=lookback_days)
    history = [
        fp
        for fp in iterate(
            source_uri=current.source_uri,
            after=cutoff,
            before=current.extraction_date,
            track_dir=track_dir,
        )
        if fp.fingerprint_id != current.fingerprint_id
    ]

    if not history:
        logger.debug(
            "no historical fingerprints for source_uri=%s in last %d days",
            current.source_uri,
            lookback_days,
        )
        return []

    # Compare against the most recent prior, structurally.
    history.sort(key=lambda fp: fp.extraction_date)
    most_recent_prior = history[-1]
    events = list(compare(most_recent_prior, current))

    # Row-count anomaly (uses full history, not just most-recent prior).
    row_counts = [fp.row_count for fp in history if fp.row_count is not None]
    if current.row_count is not None and row_counts:
        median = statistics.median(row_counts)
        if median > 0:
            delta_pct = abs(current.row_count - median) / median * 100.0
            if delta_pct > row_count_tolerance_pct:
                events.append(
                    _new_drift_event(
                        prior_id=most_recent_prior.fingerprint_id,
                        current_id=current.fingerprint_id,
                        event_type="row_count_anomaly",
                        metadata={
                            "current_row_count": current.row_count,
                            "historical_median": median,
                            "historical_n": len(row_counts),
                            "delta_pct": round(delta_pct, 2),
                            "tolerance_pct": row_count_tolerance_pct,
                        },
                    )
                )

    # Confidence drop (uses full history mean+stdev).
    confidence_values = [fp.confidence_value for fp in history if fp.confidence_value is not None]
    if current.confidence_value is not None and len(confidence_values) >= 2:
        mean = statistics.mean(confidence_values)
        stdev = statistics.stdev(confidence_values)
        threshold = mean - confidence_sigma * stdev
        if current.confidence_value < threshold:
            events.append(
                _new_drift_event(
                    prior_id=most_recent_prior.fingerprint_id,
                    current_id=current.fingerprint_id,
                    event_type="confidence_dropped",
                    metadata={
                        "current_confidence": current.confidence_value,
                        "historical_mean": mean,
                        "historical_stdev": stdev,
                        "threshold": threshold,
                        "historical_n": len(confidence_values),
                    },
                )
            )

    return events
