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

"""clio.drift.remap — auto-resolve drift events via clio.extract.schema_map.

Some drift events are structurally resolvable: a column_renamed event can
often be resolved by running the prior column-name list against the
current column-name list under sentence-transformer cosine similarity.
If the mapping recovers a confident match for every renamed column, the
drift event becomes auto-resolved with method "schema_map_auto" and the
recovered mapping is recorded in metadata.

Currently auto-resolves: column_renamed.

Other event types are not auto-resolvable in v0.1:
  * column_added — new columns may need new processing logic; surfacing for
    human review is appropriate.
  * column_removed — data loss; usually a real upstream change worth
    knowing about.
  * dtype_changed — could indicate corruption (e.g. numeric column suddenly
    string), surface for human review.
  * row_count_anomaly, confidence_dropped — operational signals, not
    structural drift.
  * extractor_version_change — info-level audit only.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

from clio.drift.detect import DriftEvent

if TYPE_CHECKING:
    from clio.extract.schema_map import SchemaMapper

logger = logging.getLogger(__name__)


def auto_remap(
    drift_events: list[DriftEvent],
    schema_mapper: "SchemaMapper",
) -> list[DriftEvent]:
    """Attempt to auto-resolve resolvable drift events via schema_map.

    For each `column_renamed` event in the input, runs the candidate-old vs
    candidate-new column lists through the SchemaMapper. If every old
    column maps to a new column above the mapper's threshold, returns a
    new DriftEvent with auto_resolved=True and the recovered mapping in
    metadata. Events that don't auto-resolve are returned with
    auto_resolved=False (and resolution_method="schema_map_auto_failed").

    Non-resolvable event types pass through unchanged.

    The mapping is intentionally conservative — partial resolution
    (only some old columns matched) is treated as failure; callers should
    surface those events for human review rather than silently ignoring
    the unmatched columns.

    Args:
        drift_events: Events from clio.drift.detect.
        schema_mapper: A SchemaMapper instance (already-warmed-up models
            speed this up significantly across batches).

    Returns:
        New list of DriftEvents with column_renamed entries possibly
        auto-resolved. Original list is not mutated.
    """
    resolved: list[DriftEvent] = []

    for event in drift_events:
        if event.event_type != "column_renamed":
            resolved.append(event)
            continue

        old_names = event.metadata.get("candidate_old_names") or []
        new_names = event.metadata.get("candidate_new_names") or []
        if not old_names or not new_names:
            logger.debug(
                "column_renamed event %s has empty candidate lists; skipping auto-remap",
                event.drift_id,
            )
            resolved.append(event)
            continue

        try:
            mapping_results = schema_mapper.map_columns(
                source_columns=old_names,
                target_columns=new_names,
            )
        except Exception as exc:  # pragma: no cover — narrow exception surface
            logger.warning("auto-remap failed for drift %s: %s", event.drift_id, exc)
            resolved.append(
                dataclasses.replace(
                    event,
                    auto_resolved=False,
                    resolution_method="schema_map_auto_failed",
                    metadata={**event.metadata, "auto_remap_error": str(exc)},
                )
            )
            continue

        # Conservative: every old column must map to something above threshold.
        if len(mapping_results) != len(old_names):
            logger.debug(
                "partial auto-remap for drift %s: %d/%d mapped above threshold; treating as failed",
                event.drift_id,
                len(mapping_results),
                len(old_names),
            )
            resolved.append(
                dataclasses.replace(
                    event,
                    auto_resolved=False,
                    resolution_method="schema_map_auto_failed",
                    metadata={
                        **event.metadata,
                        "partial_mapping": {
                            src: {"target": r.target_column, "score": r.confidence.value}
                            for src, r in mapping_results.items()
                        },
                    },
                )
            )
            continue

        recovered_map = {
            src: {"target": r.target_column, "score": r.confidence.value}
            for src, r in mapping_results.items()
        }
        resolved.append(
            dataclasses.replace(
                event,
                auto_resolved=True,
                resolution_method="schema_map_auto",
                metadata={**event.metadata, "recovered_mapping": recovered_map},
            )
        )
        logger.info(
            "auto-remap succeeded for drift %s: %d columns mapped",
            event.drift_id,
            len(recovered_map),
        )

    return resolved
