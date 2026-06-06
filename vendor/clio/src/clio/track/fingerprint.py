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

"""clio.track.fingerprint — content-addressable extraction fingerprints.

Every extraction event in clio gets a Fingerprint. The fingerprint:

  * is content-addressable — the same source URI + extraction date +
    payload always produces the same fingerprint_id (SHA256-based);
  * carries provenance — source URI, extractor module + version, payload
    hash, confidence info, optional schema metadata for tabular sources;
  * is composable — optional parent_fingerprint_id chains lineage across
    multi-step extractions (PDF -> vision -> schema_map -> enrichment).

Fingerprints are written to clio.track.store (parquet-backed, append-only,
year/month-partitioned). Adapters reference them via clio.track.audit.
AuditEnvelope, which composes with adapter result envelopes (e.g.
ic-engine ic_result) to provide end-to-end audit chains from agent output
back to the original source.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass(frozen=True)
class Fingerprint:
    """Provenance record for a single clio extraction event.

    The id field is deterministic — recomputing from the same inputs always
    yields the same id. This makes fingerprints content-addressable, which
    in turn lets downstream code dedupe (same source extracted twice yields
    one logical row).

    Attributes:
        fingerprint_id: SHA256 hex of source_uri + extraction_date + payload_hash.
        source_uri: URL, file path, S3 key, or any locator identifying the source.
        source_type: "csv" | "pdf" | "api_json" | "html" | "shapefile" | "geojson"
            | "xlsx" | "parquet" | other extractor-specific tag.
        extraction_date: When the extraction ran (UTC).
        extractor_module: Fully-qualified module name that produced this row,
            e.g. "clio.extract.vision" or "clio.extract.schema_map".
        extractor_version: clio package version at extraction time.
        payload_hash: SHA256 of canonical-JSON of the extracted payload. Used
            both as input to fingerprint_id and as a payload-equality check.
        confidence_method: "structural" | "cosine" | "ensemble" | "none".
        confidence_value: Score in [0, 1]; None if no confidence was scored.
        confidence_threshold: Threshold against which confidence_value was checked.
        confidence_passed: True iff confidence_value >= confidence_threshold.
        column_fingerprint: For tabular sources, SHA256 of "||"-joined sorted
            column-name list. For non-tabular sources, None.
        dtype_map: For tabular sources, dict[column_name, dtype_string]. Used by
            drift detection to flag dtype changes.
        row_count: For tabular sources, number of rows extracted. Used by drift
            detection to flag row-count anomalies.
        parent_fingerprint_id: For chained extractions, the fingerprint id of
            the parent extraction that produced this one's input.
        metadata: Open-ended dict for extractor-specific details. Audit logs
            preserve verbatim; downstream code reads only by-key, not by-shape.
    """

    fingerprint_id: str
    source_uri: str
    source_type: str
    extraction_date: datetime
    extractor_module: str
    extractor_version: str
    payload_hash: str
    confidence_method: str = "none"
    confidence_value: Optional[float] = None
    confidence_threshold: Optional[float] = None
    confidence_passed: Optional[bool] = None
    column_fingerprint: Optional[str] = None
    dtype_map: Optional[dict[str, str]] = None
    row_count: Optional[int] = None
    parent_fingerprint_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def compute(
        cls,
        *,
        source_uri: str,
        source_type: str,
        extractor_module: str,
        extractor_version: str,
        payload: Any,
        extraction_date: Optional[datetime] = None,
        confidence_method: str = "none",
        confidence_value: Optional[float] = None,
        confidence_threshold: Optional[float] = None,
        confidence_passed: Optional[bool] = None,
        column_fingerprint: Optional[str] = None,
        dtype_map: Optional[dict[str, str]] = None,
        row_count: Optional[int] = None,
        parent_fingerprint_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "Fingerprint":
        """Build a Fingerprint deterministically from inputs and a payload.

        The fingerprint_id is SHA256 of (source_uri || extraction_date_iso ||
        payload_hash). Recomputing with the same inputs always yields the
        same id. The payload is canonical-JSON-serialized (sort_keys=True,
        default=str for non-JSON types) before hashing, so dict-key
        ordering doesn't affect the hash.

        Args:
            source_uri: Source locator.
            source_type: Source category tag.
            extractor_module: Fully-qualified clio module that ran the extraction.
            extractor_version: clio package version (caller should pass `clio.__version__`).
            payload: Extracted payload. Will be JSON-canonicalized for hashing.
            extraction_date: Defaults to datetime.now(timezone.utc).
            confidence_*, column_fingerprint, dtype_map, row_count,
                parent_fingerprint_id, metadata: optional context fields,
                see Fingerprint class docstring.

        Returns:
            Fingerprint with deterministic fingerprint_id.
        """
        date = extraction_date if extraction_date is not None else datetime.now(timezone.utc)
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        fp_id = hashlib.sha256(
            f"{source_uri}||{date.isoformat()}||{payload_hash}".encode("utf-8")
        ).hexdigest()
        return cls(
            fingerprint_id=fp_id,
            source_uri=source_uri,
            source_type=source_type,
            extraction_date=date,
            extractor_module=extractor_module,
            extractor_version=extractor_version,
            payload_hash=payload_hash,
            confidence_method=confidence_method,
            confidence_value=confidence_value,
            confidence_threshold=confidence_threshold,
            confidence_passed=confidence_passed,
            column_fingerprint=column_fingerprint,
            dtype_map=dtype_map,
            row_count=row_count,
            parent_fingerprint_id=parent_fingerprint_id,
            metadata=metadata or {},
        )


def column_fingerprint_of(columns: list[str]) -> str:
    """Compute the column-fingerprint hash for a tabular source.

    SHA256 of "||"-joined sorted column-name list. Sort makes the result
    invariant to column ordering — same columns in different order produce
    the same fingerprint. The "||" separator avoids collisions between
    e.g. ["foo", "bar"] and ["foob", "ar"].

    Args:
        columns: Column-name list.

    Returns:
        SHA256 hex digest.
    """
    canonical = "||".join(sorted(str(c) for c in columns))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def payload_hash_of(payload: Any) -> str:
    """Compute the SHA256 payload-hash for arbitrary JSON-serializable data.

    Useful when callers want to compute the hash separately from building
    a Fingerprint — e.g. for content-addressable cache keys outside clio.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
