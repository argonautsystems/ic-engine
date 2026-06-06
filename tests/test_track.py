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

"""Tests for clio.track — fingerprint, store, lineage, audit."""

from __future__ import annotations

from datetime import datetime, timezone

from clio.track import (
    AuditEnvelope,
    Fingerprint,
    column_fingerprint_of,
    descendants,
    iterate,
    payload_hash_of,
    read,
    scan,
    trace,
    write,
)

# ============================================================================
# Fingerprint determinism
# ============================================================================


def test_fingerprint_compute_is_deterministic():
    """Same inputs always produce the same fingerprint_id."""
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp1 = Fingerprint.compute(
        source_uri="s3://bucket/file.csv",
        source_type="csv",
        extractor_module="clio.extract.schema_map",
        extractor_version="0.1.0",
        payload={"row": 1, "col": "a"},
        extraction_date=when,
    )
    fp2 = Fingerprint.compute(
        source_uri="s3://bucket/file.csv",
        source_type="csv",
        extractor_module="clio.extract.schema_map",
        extractor_version="0.1.0",
        payload={"col": "a", "row": 1},  # same payload, different key order
        extraction_date=when,
    )
    assert fp1.fingerprint_id == fp2.fingerprint_id


def test_fingerprint_different_payloads_different_ids():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp1 = Fingerprint.compute(
        source_uri="x",
        source_type="csv",
        extractor_module="clio.extract.schema_map",
        extractor_version="0.1.0",
        payload={"a": 1},
        extraction_date=when,
    )
    fp2 = Fingerprint.compute(
        source_uri="x",
        source_type="csv",
        extractor_module="clio.extract.schema_map",
        extractor_version="0.1.0",
        payload={"a": 2},
        extraction_date=when,
    )
    assert fp1.fingerprint_id != fp2.fingerprint_id


def test_fingerprint_different_source_different_ids():
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"row": 1}
    fp1 = Fingerprint.compute(
        source_uri="A",
        source_type="csv",
        extractor_module="clio.extract.schema_map",
        extractor_version="0.1.0",
        payload=payload,
        extraction_date=when,
    )
    fp2 = Fingerprint.compute(
        source_uri="B",
        source_type="csv",
        extractor_module="clio.extract.schema_map",
        extractor_version="0.1.0",
        payload=payload,
        extraction_date=when,
    )
    assert fp1.fingerprint_id != fp2.fingerprint_id


def test_column_fingerprint_invariant_to_order():
    a = column_fingerprint_of(["foo", "bar", "baz"])
    b = column_fingerprint_of(["bar", "baz", "foo"])
    assert a == b


def test_payload_hash_consistent():
    h1 = payload_hash_of({"x": 1, "y": 2})
    h2 = payload_hash_of({"y": 2, "x": 1})
    assert h1 == h2


# ============================================================================
# Store roundtrip
# ============================================================================


def _make_fp(suffix: str, when: datetime, parent: str | None = None) -> Fingerprint:
    return Fingerprint.compute(
        source_uri=f"s3://bucket/file-{suffix}.csv",
        source_type="csv",
        extractor_module="clio.extract.schema_map",
        extractor_version="0.1.0",
        payload={"key": suffix},
        extraction_date=when,
        parent_fingerprint_id=parent,
    )


def test_store_write_and_read_roundtrip(tmp_path):
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp = _make_fp("alpha", when)
    write(fp, track_dir=tmp_path)

    loaded = read(fp.fingerprint_id, track_dir=tmp_path)
    assert loaded is not None
    assert loaded.fingerprint_id == fp.fingerprint_id
    assert loaded.source_uri == fp.source_uri
    assert loaded.payload_hash == fp.payload_hash


def test_store_read_missing_returns_none(tmp_path):
    assert read("nonexistent_id", track_dir=tmp_path) is None


def test_store_partition_layout(tmp_path):
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp = _make_fp("alpha", when)
    file_path = write(fp, track_dir=tmp_path)
    # File should land at year=2026/month=04/
    assert "year=2026" in str(file_path)
    assert "month=04" in str(file_path)


def test_store_iterate_filters(tmp_path):
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    fp_a = _make_fp("alpha", when)
    fp_b = _make_fp("beta", when)
    write([fp_a, fp_b], track_dir=tmp_path)

    matched = list(iterate(source_uri=fp_a.source_uri, track_dir=tmp_path))
    assert len(matched) == 1
    assert matched[0].fingerprint_id == fp_a.fingerprint_id


def test_store_scan_empty(tmp_path):
    """Empty store returns an empty LazyFrame with the canonical schema."""
    df = scan(track_dir=tmp_path).collect()
    assert df.height == 0
    # Must have the schema; downstream filters should work even on empty.
    assert "fingerprint_id" in df.columns


# ============================================================================
# Lineage chain walking
# ============================================================================


def test_lineage_trace_single_step(tmp_path):
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    root = _make_fp("root", when)
    write(root, track_dir=tmp_path)

    chain = trace(root.fingerprint_id, track_dir=tmp_path)
    assert len(chain) == 1
    assert chain[0].fingerprint_id == root.fingerprint_id


def test_lineage_trace_chain(tmp_path):
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    root = _make_fp("root", when)
    middle = _make_fp("middle", when, parent=root.fingerprint_id)
    leaf = _make_fp("leaf", when, parent=middle.fingerprint_id)
    write([root, middle, leaf], track_dir=tmp_path)

    chain = trace(leaf.fingerprint_id, track_dir=tmp_path)
    assert len(chain) == 3
    # Root-to-leaf order
    assert chain[0].fingerprint_id == root.fingerprint_id
    assert chain[1].fingerprint_id == middle.fingerprint_id
    assert chain[2].fingerprint_id == leaf.fingerprint_id


def test_lineage_descendants_finds_subtree(tmp_path):
    when = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    root = _make_fp("root", when)
    middle = _make_fp("middle", when, parent=root.fingerprint_id)
    leaf = _make_fp("leaf", when, parent=middle.fingerprint_id)
    sibling = _make_fp("sibling", when)  # not in the lineage
    write([root, middle, leaf, sibling], track_dir=tmp_path)

    found = descendants(root.fingerprint_id, track_dir=tmp_path)
    found_ids = {fp.fingerprint_id for fp in found}
    assert root.fingerprint_id in found_ids
    assert middle.fingerprint_id in found_ids
    assert leaf.fingerprint_id in found_ids
    assert sibling.fingerprint_id not in found_ids


def test_lineage_trace_missing_returns_empty(tmp_path):
    chain = trace("nonexistent_id", track_dir=tmp_path)
    assert chain == []


# ============================================================================
# AuditEnvelope
# ============================================================================


def test_audit_envelope_minimal():
    env = AuditEnvelope(clio_fingerprint_id="abc123", clio_version="0.1.0")
    assert env.clio_fingerprint_id == "abc123"
    assert env.clio_version == "0.1.0"


def test_audit_envelope_is_frozen():
    env = AuditEnvelope(clio_fingerprint_id="abc", clio_version="0.1.0")
    # Frozen dataclass — assignment should raise
    import pytest

    with pytest.raises(Exception):
        env.clio_fingerprint_id = "xyz"  # type: ignore
