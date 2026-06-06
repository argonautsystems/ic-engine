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

"""clio.track.store — append-only parquet-backed Fingerprint store.

Storage layout (Hive-partitioned):

    <track_dir>/
      year=YYYY/
        month=MM/
          fingerprints-<DATE>-<UUID>.parquet

Each parquet file is a single batch — small, immutable, append-only by
naming convention (unique filenames per write). Reads use Polars'
glob-pattern lazy scan, which transparently unions all partition files.

Default track_dir is `data/clio/track/`. Override per-process via
`CLIO_TRACK_DIR` environment variable, or per-call by passing `track_dir=`
to read/write/scan/iterate functions.

Concurrency: writes are safe across processes because each writer chooses
a unique filename (UUID4 suffix). No explicit locking. Readers ignore any
half-written file (Polars validates parquet footers on read).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import polars as pl

from clio.track.fingerprint import Fingerprint

logger = logging.getLogger(__name__)


DEFAULT_TRACK_DIR = Path("data/clio/track")


def _resolve_track_dir(track_dir: Optional[Path] = None) -> Path:
    """Pick the active track directory: explicit arg > env var > default."""
    if track_dir is not None:
        return Path(track_dir)
    env = os.environ.get("CLIO_TRACK_DIR")
    if env:
        return Path(env)
    return DEFAULT_TRACK_DIR


def _partition_dir(root: Path, when: datetime) -> Path:
    """Return the year=YYYY/month=MM partition directory for a given timestamp."""
    return root / f"year={when.year:04d}" / f"month={when.month:02d}"


def _fingerprint_to_row(fp: Fingerprint) -> dict:
    """Serialize a Fingerprint to a flat row for parquet writes."""
    return {
        "fingerprint_id": fp.fingerprint_id,
        "source_uri": fp.source_uri,
        "source_type": fp.source_type,
        "extraction_date": fp.extraction_date,
        "extractor_module": fp.extractor_module,
        "extractor_version": fp.extractor_version,
        "payload_hash": fp.payload_hash,
        "confidence_method": fp.confidence_method,
        "confidence_value": fp.confidence_value,
        "confidence_threshold": fp.confidence_threshold,
        "confidence_passed": fp.confidence_passed,
        "column_fingerprint": fp.column_fingerprint,
        "dtype_map_json": json.dumps(fp.dtype_map, sort_keys=True) if fp.dtype_map else None,
        "row_count": fp.row_count,
        "parent_fingerprint_id": fp.parent_fingerprint_id,
        "metadata_json": json.dumps(fp.metadata, sort_keys=True, default=str)
        if fp.metadata
        else None,
    }


def _row_to_fingerprint(row: dict) -> Fingerprint:
    """Deserialize a parquet row back into a Fingerprint."""
    dtype_map = json.loads(row["dtype_map_json"]) if row.get("dtype_map_json") else None
    metadata = json.loads(row["metadata_json"]) if row.get("metadata_json") else {}
    return Fingerprint(
        fingerprint_id=row["fingerprint_id"],
        source_uri=row["source_uri"],
        source_type=row["source_type"],
        extraction_date=row["extraction_date"],
        extractor_module=row["extractor_module"],
        extractor_version=row["extractor_version"],
        payload_hash=row["payload_hash"],
        confidence_method=row.get("confidence_method") or "none",
        confidence_value=row.get("confidence_value"),
        confidence_threshold=row.get("confidence_threshold"),
        confidence_passed=row.get("confidence_passed"),
        column_fingerprint=row.get("column_fingerprint"),
        dtype_map=dtype_map,
        row_count=row.get("row_count"),
        parent_fingerprint_id=row.get("parent_fingerprint_id"),
        metadata=metadata,
    )


def write(
    fingerprints: list[Fingerprint] | Fingerprint,
    *,
    track_dir: Optional[Path] = None,
) -> Path:
    """Append one or more fingerprints to the store.

    Writes a single parquet file containing the batch. The filename includes
    a UUID4 suffix to make concurrent writers safe. All fingerprints in the
    batch are placed in the partition for the EARLIEST extraction_date —
    callers writing across multiple months should split into per-month
    batches if they care about strict month partitioning.

    Args:
        fingerprints: Single Fingerprint or list of Fingerprints to write.
        track_dir: Override the store root.

    Returns:
        Path to the written parquet file.
    """
    if isinstance(fingerprints, Fingerprint):
        fingerprints = [fingerprints]
    if not fingerprints:
        raise ValueError("write() called with no fingerprints")

    root = _resolve_track_dir(track_dir)
    earliest = min(fp.extraction_date for fp in fingerprints)
    partition = _partition_dir(root, earliest)
    partition.mkdir(parents=True, exist_ok=True)

    filename = f"fingerprints-{earliest.strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:12]}.parquet"
    file_path = partition / filename

    rows = [_fingerprint_to_row(fp) for fp in fingerprints]
    df = pl.from_dicts(rows)
    df.write_parquet(file_path, compression="snappy")

    logger.info("wrote %d fingerprint(s) to %s", len(fingerprints), file_path)
    return file_path


def scan(*, track_dir: Optional[Path] = None) -> pl.LazyFrame:
    """Return a Polars LazyFrame over all fingerprints in the store.

    Lazy — no I/O until the caller .collect()s. Use for ad-hoc queries
    via Polars expressions rather than building Python iterators.

    If the store is empty (no parquet files written yet), returns an
    empty LazyFrame with the canonical schema.

    Args:
        track_dir: Override the store root.

    Returns:
        Polars LazyFrame over all fingerprint rows.
    """
    root = _resolve_track_dir(track_dir)
    pattern = str(root / "**" / "*.parquet")
    glob_results = list(root.glob("**/*.parquet"))
    if not glob_results:
        # Empty store — return an empty LazyFrame with the right schema.
        empty_schema = {
            "fingerprint_id": pl.Utf8,
            "source_uri": pl.Utf8,
            "source_type": pl.Utf8,
            "extraction_date": pl.Datetime,
            "extractor_module": pl.Utf8,
            "extractor_version": pl.Utf8,
            "payload_hash": pl.Utf8,
            "confidence_method": pl.Utf8,
            "confidence_value": pl.Float64,
            "confidence_threshold": pl.Float64,
            "confidence_passed": pl.Boolean,
            "column_fingerprint": pl.Utf8,
            "dtype_map_json": pl.Utf8,
            "row_count": pl.Int64,
            "parent_fingerprint_id": pl.Utf8,
            "metadata_json": pl.Utf8,
        }
        return pl.LazyFrame(schema=empty_schema)
    return pl.scan_parquet(pattern)


def read(fingerprint_id: str, *, track_dir: Optional[Path] = None) -> Optional[Fingerprint]:
    """Look up a single Fingerprint by its content-hash id.

    Returns None if no matching row exists. If multiple rows share the same
    fingerprint_id (which only happens if a caller deduplicates poorly),
    the first match is returned and a warning is logged.

    Args:
        fingerprint_id: SHA256 hex from Fingerprint.compute().
        track_dir: Override the store root.

    Returns:
        Fingerprint or None.
    """
    df = scan(track_dir=track_dir).filter(pl.col("fingerprint_id") == fingerprint_id).collect()
    if df.height == 0:
        return None
    if df.height > 1:
        logger.warning("multiple rows with fingerprint_id=%s; returning first", fingerprint_id)
    return _row_to_fingerprint(df.row(0, named=True))


def iterate(
    *,
    source_uri: Optional[str] = None,
    source_type: Optional[str] = None,
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
    extractor_module: Optional[str] = None,
    track_dir: Optional[Path] = None,
) -> Iterator[Fingerprint]:
    """Iterate Fingerprints matching one or more filters.

    All filters are AND-combined. None means "don't filter on this field".
    Results are NOT sorted by default; callers wanting time-ordered iteration
    should sort the resulting list, or use scan() with a Polars sort
    expression.

    Args:
        source_uri: Exact source-URI match.
        source_type: Exact source-type match.
        after: Only fingerprints with extraction_date >= after.
        before: Only fingerprints with extraction_date <= before.
        extractor_module: Exact extractor-module match.
        track_dir: Override the store root.

    Yields:
        Matching Fingerprint records.
    """
    lf = scan(track_dir=track_dir)
    if source_uri is not None:
        lf = lf.filter(pl.col("source_uri") == source_uri)
    if source_type is not None:
        lf = lf.filter(pl.col("source_type") == source_type)
    if after is not None:
        lf = lf.filter(pl.col("extraction_date") >= after)
    if before is not None:
        lf = lf.filter(pl.col("extraction_date") <= before)
    if extractor_module is not None:
        lf = lf.filter(pl.col("extractor_module") == extractor_module)

    df = lf.collect()
    for row in df.iter_rows(named=True):
        yield _row_to_fingerprint(row)
