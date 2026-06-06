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

"""clio.track — persistent provenance and lineage.

Subsystems (all landed Phase 1.5b):

    fingerprint   Content-addressable extraction record. SHA256 of
                  source_uri + extraction_date + payload_hash. Optional
                  schema metadata (column fingerprint + dtype map +
                  row count) for tabular sources.

    store         Append-only Polars-native parquet store, year/month
                  Hive-partitioned. Default path data/clio/track/;
                  override via CLIO_TRACK_DIR env or per-call.

    lineage       Walk parent_fingerprint_id chains (trace) and find
                  downstream descendants (descendants).

    audit         Minimal AuditEnvelope for inclusion in adapter result
                  envelopes (e.g. ic_result.clio_fingerprint_id).
                  Composition rather than coupling — adapters that don't
                  use clio carry no clio dependency in their envelope.
"""

from clio.track.audit import AuditEnvelope
from clio.track.fingerprint import (
    Fingerprint,
    column_fingerprint_of,
    payload_hash_of,
)
from clio.track.lineage import descendants, trace
from clio.track.store import iterate, read, scan, write

__all__ = [
    "Fingerprint",
    "AuditEnvelope",
    "column_fingerprint_of",
    "payload_hash_of",
    "write",
    "read",
    "scan",
    "iterate",
    "trace",
    "descendants",
]
