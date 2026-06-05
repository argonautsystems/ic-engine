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
SyncState — lightweight JSON manifest tracking Tesseract ingestion progress.

Stored alongside the parquet partitions so restarts / crash-recovery can
resume where the last atomic rename left off. The state file lives at the
root of the data directory as ``.tesseract_sync_state.json``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = ".tesseract_sync_state.json"


class SyncState:
    """CRUD wrapper around the JSON sync-state file.

    The file is read on construction and written back on mutation. It is
    intentionally single-instance per data directory — callers holding a
    ``SyncState`` handle after an ingestion should discard it and open a
    fresh one to see the updated partitions.
    """

    def __init__(self, data_dir: Path):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / STATE_FILENAME
        self._data = self._load()

    # ── read helpers ─────────────────────────────────────────────────────────

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def partitions(self) -> List[str]:
        """Sorted list of ingested partition dates (YYYY-MM-DD)."""
        return sorted(self._data.get("partitions", []))

    def latest_partition(self) -> Optional[str]:
        """Most recent ingested partition date, or None."""
        parts = self.partitions()
        return parts[-1] if parts else None

    def earliest_partition(self) -> Optional[str]:
        """Earliest ingested partition date, or None."""
        parts = self.partitions()
        return parts[0] if parts else None

    def last_sync_at(self) -> Optional[str]:
        """ISO-8601 UTC timestamp of last successful ingestion."""
        return self._data.get("last_sync_at")

    def total_rows(self) -> int:
        """Cumulative row count across all ingested partitions."""
        return self._data.get("total_rows", 0)

    def total_files(self) -> int:
        """Count of parquet files across all ingested partitions."""
        return self._data.get("total_files", 0)

    def source_url(self) -> Optional[str]:
        """Base URL of the Massive bulk download source."""
        return self._data.get("source_url")

    def provider(self) -> str:
        return self._data.get("provider", "massive")

    # ── write helpers ────────────────────────────────────────────────────────

    def record_ingestion(
        self,
        partition_date: str,
        rows: int,
        files: int,
        source_url: Optional[str] = None,
    ) -> None:
        """Mark a partition as ingested.

        ``partition_date`` is YYYY-MM-DD. Duplicate dates are silently
        ignored — the partition set is a unique list.
        """
        data = dict(self._data)
        parts: List[str] = data.get("partitions", [])
        if partition_date not in parts:
            parts.append(partition_date)
            parts.sort()
        data["partitions"] = parts
        data["last_sync_at"] = datetime.now(timezone.utc).isoformat()
        data["total_rows"] = data.get("total_rows", 0) + max(rows, 0)
        data["total_files"] = data.get("total_files", 0) + max(files, 0)
        data["provider"] = "massive"
        if source_url:
            data["source_url"] = source_url
        self._data = data
        self._save()

    def set_source_url(self, url: str) -> None:
        self._data["source_url"] = url
        self._save()

    def clear(self) -> None:
        """Reset state (for testing / re-ingestion)."""
        self._data = {}
        self._save()

    # ── internal ─────────────────────────────────────────────────────────────

    def _load(self) -> Dict:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("SyncState %s unreadable (%s); resetting", self._path, e)
            return {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)  # atomic on POSIX
        except OSError as e:
            logger.error("SyncState save failed: %s", e)
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
