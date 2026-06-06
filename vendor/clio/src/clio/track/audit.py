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

"""clio.track.audit — composable audit envelope for adapter result envelopes.

Adapters that drive a clio extraction (e.g. an InvestorClaw command running
broker-statement vision extraction; a RiskyEats loader running CSV
schema_map; an rvmaps script running geospatial extraction) carry their
own result envelope (e.g. `ic_result` for the claws-family adapters).
The AuditEnvelope is a minimal struct that those adapters can include in
their result envelope as an optional `clio_fingerprint_id: str | None`
field.

Intentionally minimal — we only carry the fingerprint_id reference.
Adapters that need the full Fingerprint look it up via
`clio.track.store.read(fingerprint_id)`. Adapters that don't care can
ignore the field. Composition, not coupling.

Usage from an adapter:

    # In InvestorClaw or another adapter, after a clio extraction:
    from clio.track.audit import AuditEnvelope
    import clio

    fp = ...  # Fingerprint produced by the extraction
    audit = AuditEnvelope(clio_fingerprint_id=fp.fingerprint_id, clio_version=clio.__version__)

    ic_result = {
        "data": {...},
        "metadata": {...},
        "clio_fingerprint_id": audit.clio_fingerprint_id,  # optional field
        "clio_version": audit.clio_version,
    }
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuditEnvelope:
    """Minimal audit reference for inclusion in adapter result envelopes.

    The full Fingerprint lives in clio.track.store; the AuditEnvelope just
    carries the id reference and the clio version that produced it. This
    keeps adapter result envelopes lightweight (one optional string field
    in the common case) while preserving the audit chain.

    Attributes:
        clio_fingerprint_id: SHA256 hex from Fingerprint.fingerprint_id. Look
            up the full Fingerprint via clio.track.store.read().
        clio_version: clio package version that produced the extraction.
            Captured at extraction time so future drift detection can flag
            extractor-version changes.
    """

    clio_fingerprint_id: str
    clio_version: str
