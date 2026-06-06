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

"""Tests for clio.drift — detect, remap, alarm."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from clio.drift import (
    DriftEvent,
    auto_remap,
    compare,
    detect_against_history,
    severity_of,
    surface,
)
from clio.track import Fingerprint, write


def _fp(
    *,
    suffix: str,
    when: datetime,
    cols: dict | None = None,
    extractor_version: str = "0.1.0",
    confidence_value: float | None = None,
    row_count: int | None = None,
    source_uri: str | None = None,
) -> Fingerprint:
    return Fingerprint.compute(
        source_uri=source_uri or "s3://bucket/file.csv",
        source_type="csv",
        extractor_module="clio.extract.schema_map",
        extractor_version=extractor_version,
        payload={"key": suffix},
        extraction_date=when,
        dtype_map=cols,
        confidence_value=confidence_value,
        row_count=row_count,
    )


# ============================================================================
# Pairwise compare — column drift events
# ============================================================================


def test_compare_no_drift_returns_empty():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    schema = {"a": "Utf8", "b": "Int64"}
    fp1 = _fp(suffix="alpha", when=when, cols=schema)
    fp2 = _fp(suffix="beta", when=when + timedelta(hours=1), cols=schema)
    assert compare(fp1, fp2) == []


def test_compare_column_added():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp1 = _fp(suffix="alpha", when=when, cols={"a": "Utf8", "b": "Int64"})
    fp2 = _fp(
        suffix="beta",
        when=when + timedelta(hours=1),
        cols={"a": "Utf8", "b": "Int64", "c": "Float64"},
    )
    events = compare(fp1, fp2)
    types = [e.event_type for e in events]
    assert "column_added" in types
    added_event = next(e for e in events if e.event_type == "column_added")
    assert added_event.metadata["new_columns"] == ["c"]
    assert added_event.severity == "warn"


def test_compare_column_removed():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp1 = _fp(suffix="alpha", when=when, cols={"a": "Utf8", "b": "Int64", "c": "Float64"})
    fp2 = _fp(suffix="beta", when=when + timedelta(hours=1), cols={"a": "Utf8", "b": "Int64"})
    events = compare(fp1, fp2)
    types = [e.event_type for e in events]
    assert "column_removed" in types
    removed_event = next(e for e in events if e.event_type == "column_removed")
    assert removed_event.metadata["removed_columns"] == ["c"]
    assert removed_event.severity == "error"


def test_compare_column_renamed_when_counts_match():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp1 = _fp(suffix="alpha", when=when, cols={"a": "Utf8", "old_name": "Int64"})
    fp2 = _fp(
        suffix="beta", when=when + timedelta(hours=1), cols={"a": "Utf8", "new_name": "Int64"}
    )
    events = compare(fp1, fp2)
    types = [e.event_type for e in events]
    assert "column_renamed" in types
    # Should NOT also emit column_added + column_removed
    assert "column_added" not in types
    assert "column_removed" not in types


def test_compare_dtype_changed():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp1 = _fp(suffix="alpha", when=when, cols={"a": "Utf8", "b": "Int64"})
    fp2 = _fp(suffix="beta", when=when + timedelta(hours=1), cols={"a": "Utf8", "b": "Float64"})
    events = compare(fp1, fp2)
    types = [e.event_type for e in events]
    assert "dtype_changed" in types
    dtype_event = next(e for e in events if e.event_type == "dtype_changed")
    assert "b" in dtype_event.metadata["changes"]
    assert dtype_event.metadata["changes"]["b"]["prior_dtype"] == "Int64"
    assert dtype_event.metadata["changes"]["b"]["current_dtype"] == "Float64"


def test_compare_extractor_version_change_is_info_severity():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp1 = _fp(suffix="alpha", when=when, extractor_version="0.1.0")
    fp2 = _fp(suffix="beta", when=when + timedelta(hours=1), extractor_version="0.2.0")
    events = compare(fp1, fp2)
    version_events = [e for e in events if e.event_type == "extractor_version_change"]
    assert len(version_events) == 1
    assert version_events[0].severity == "info"


# ============================================================================
# Historical detection
# ============================================================================


def test_detect_against_history_no_history_returns_empty(tmp_path):
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp = _fp(suffix="alpha", when=when, cols={"a": "Utf8"}, source_uri="s3://only-once.csv")
    write(fp, track_dir=tmp_path)
    events = detect_against_history(fp, track_dir=tmp_path)
    assert events == []


def test_detect_against_history_row_count_anomaly(tmp_path):
    base_time = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    history = []
    for day in range(5):
        history.append(
            _fp(
                suffix=f"day{day}",
                when=base_time + timedelta(days=day),
                cols={"a": "Utf8"},
                row_count=1000,
                source_uri="s3://daily.csv",
            )
        )
    write(history, track_dir=tmp_path)

    # Current is dramatically different — 100 rows vs historical 1000
    current = _fp(
        suffix="today",
        when=base_time + timedelta(days=10),
        cols={"a": "Utf8"},
        row_count=100,
        source_uri="s3://daily.csv",
    )
    write(current, track_dir=tmp_path)
    events = detect_against_history(current, track_dir=tmp_path)
    types = [e.event_type for e in events]
    assert "row_count_anomaly" in types


# ============================================================================
# severity_of
# ============================================================================


def test_severity_of_empty_is_info():
    assert severity_of([]) == "info"


def test_severity_of_picks_max():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    base_event = lambda et, sev: DriftEvent(  # noqa: E731
        drift_id="x",
        prior_fingerprint_id="p",
        current_fingerprint_id="c",
        event_type=et,
        severity=sev,
        detection_date=when,
    )
    events = [
        base_event("info_event", "info"),
        base_event("warn_event", "warn"),
        base_event("error_event", "error"),
    ]
    assert severity_of(events) == "error"


def test_severity_of_critical_dominates():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        DriftEvent(
            drift_id="x",
            prior_fingerprint_id="p",
            current_fingerprint_id="c",
            event_type="info",
            severity="info",
            detection_date=when,
        ),
        DriftEvent(
            drift_id="y",
            prior_fingerprint_id="p",
            current_fingerprint_id="c",
            event_type="critical",
            severity="critical",
            detection_date=when,
        ),
    ]
    assert severity_of(events) == "critical"


# ============================================================================
# surface — file target writes JSON-Lines
# ============================================================================


def test_surface_to_file(tmp_path):
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        DriftEvent(
            drift_id="d1",
            prior_fingerprint_id="p",
            current_fingerprint_id="c",
            event_type="column_added",
            severity="warn",
            detection_date=when,
            metadata={"new_columns": ["foo"]},
        ),
    ]
    log_path = tmp_path / "drift.jsonl"
    surface(events, target="file", file_path=log_path)
    assert log_path.exists()
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 1

    import json

    parsed = json.loads(lines[0])
    assert parsed["drift_id"] == "d1"
    assert parsed["event_type"] == "column_added"
    assert parsed["metadata"]["new_columns"] == ["foo"]


def test_surface_to_log_does_not_raise():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        DriftEvent(
            drift_id="d1",
            prior_fingerprint_id="p",
            current_fingerprint_id="c",
            event_type="column_added",
            severity="warn",
            detection_date=when,
        ),
    ]
    surface(events, target="log")  # should not raise


def test_surface_unknown_target_raises():
    import pytest

    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        DriftEvent(
            drift_id="d1",
            prior_fingerprint_id="p",
            current_fingerprint_id="c",
            event_type="column_added",
            severity="warn",
            detection_date=when,
        ),
    ]
    with pytest.raises(ValueError, match="unsupported drift-alarm target"):
        surface(events, target="webhook")
