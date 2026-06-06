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

"""clio.drift — semantic drift detection over the tracking store.

Subsystems (all landed Phase 1.5b):

    detect    Pairwise compare two fingerprints (compare) or compare a new
              fingerprint against historical context for the same source URI
              (detect_against_history). Emits typed DriftEvent records.

    remap     Auto-resolve resolvable drift events. Currently handles
              column_renamed via clio.extract.schema_map; other event types
              pass through unchanged for human review.

    alarm     Surface drift events to a configured target (log or file).
              severity_of() aggregates a batch's max severity.

Event taxonomy (see clio.drift.detect):
    column_added            (warn)
    column_removed          (error)
    column_renamed          (warn, auto-resolvable)
    dtype_changed           (error)
    row_count_anomaly       (warn)
    confidence_dropped      (warn)
    extractor_version_change (info)
"""

from clio.drift.alarm import severity_of, surface
from clio.drift.detect import DriftEvent, compare, detect_against_history
from clio.drift.remap import auto_remap

__all__ = [
    "DriftEvent",
    "compare",
    "detect_against_history",
    "auto_remap",
    "severity_of",
    "surface",
]
