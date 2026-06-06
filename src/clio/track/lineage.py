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

"""clio.track.lineage — walk parent-chains and descendant-trees over fingerprints.

Multi-step extractions form a chain via parent_fingerprint_id:

    PDF source --vision--> Fingerprint A --schema_map--> Fingerprint B --enrich--> Fingerprint C

`trace(C.id)` returns [A, B, C] (root-to-leaf order).
`descendants(A.id)` returns [A, B, C] (depth-first, root-first).

These walk the parent_fingerprint_id pointer chain stored in the
clio.track.store. They do not modify the store; they're read-only views
over fingerprint provenance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from clio.track.fingerprint import Fingerprint
from clio.track.store import _row_to_fingerprint, scan


def trace(fingerprint_id: str, *, track_dir: Optional[Path] = None) -> list[Fingerprint]:
    """Walk the parent_fingerprint_id chain for a given fingerprint.

    Returns the root-to-leaf list. If the fingerprint has no parent
    (top-of-chain), returns a single-element list. If the fingerprint is
    not found in the store, returns an empty list.

    Cycles are guarded by a visited set — if a malformed chain points back
    on itself, traversal stops at the cycle (silent rather than raising;
    caller can detect a cycle by checking len(result) against expectations).

    Args:
        fingerprint_id: Leaf fingerprint id to trace from.
        track_dir: Override the store root.

    Returns:
        List of Fingerprint records, root first, leaf last.
    """
    df = scan(track_dir=track_dir).collect()
    by_id: dict[str, dict] = {row["fingerprint_id"]: row for row in df.iter_rows(named=True)}

    chain: list[Fingerprint] = []
    visited: set[str] = set()
    current_id: Optional[str] = fingerprint_id
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        row = by_id.get(current_id)
        if row is None:
            break
        chain.append(_row_to_fingerprint(row))
        current_id = row.get("parent_fingerprint_id")

    chain.reverse()  # accumulator was leaf-first; flip to root-first
    return chain


def descendants(fingerprint_id: str, *, track_dir: Optional[Path] = None) -> list[Fingerprint]:
    """Find all fingerprints whose parent chain transitively includes the given id.

    Depth-first traversal, root-first ordering. The given fingerprint itself
    is included as the first element (or omitted if not found in the store).

    Useful for impact-analysis: "if I revoke fingerprint X, what downstream
    extractions does that invalidate?"

    Args:
        fingerprint_id: Root fingerprint id to walk down from.
        track_dir: Override the store root.

    Returns:
        List of Fingerprint records, root first, depth-first ordering for
        the rest. May contain duplicates if the lineage tree has shared
        ancestry (rare in append-only-write practice).
    """
    df = scan(track_dir=track_dir).collect()
    by_id: dict[str, dict] = {row["fingerprint_id"]: row for row in df.iter_rows(named=True)}

    # Build child-map: parent_id -> list of child fingerprint_ids.
    children: dict[str, list[str]] = {}
    for row in df.iter_rows(named=True):
        parent = row.get("parent_fingerprint_id")
        if parent:
            children.setdefault(parent, []).append(row["fingerprint_id"])

    out: list[Fingerprint] = []
    visited: set[str] = set()

    def _dfs(node_id: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        row = by_id.get(node_id)
        if row is None:
            return
        out.append(_row_to_fingerprint(row))
        for child_id in children.get(node_id, []):
            _dfs(child_id)

    _dfs(fingerprint_id)
    return out
