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

"""clio.extract.confidence — common confidence-score interface across extractors.

Different extractors compute confidence differently:
  * Vision extraction: structural — fraction of pages returning parseable JSON,
    whether parsed payload is non-empty, schema-validation success.
  * Schema mapping: similarity-based — cosine over sentence-transformer
    embeddings of source vs target column descriptions.
  * Future NER: classifier output probabilities.
  * Future ensembles: weighted aggregation of multiple methods.

These are fundamentally different metrics. They share a contract — a numeric
value, a threshold to gate decisions, a pass/fail flag, and method-specific
metadata for audit purposes — but not an algorithm. This module defines the
shared contract as a runtime-checkable Protocol so downstream code (drift
detection, audit envelopes, adapter fallback logic) can reason about
confidence uniformly without false unification of the underlying scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ConfidenceScore(Protocol):
    """Common interface across all clio extraction subsystems.

    Implementations are typically frozen dataclasses with method-specific
    fields. The Protocol pins only the four fields downstream code relies on.

    Attributes:
        value: Confidence score in the closed interval [0.0, 1.0]. Higher is
            more confident. Implementations should clamp to this range.
        method: Method identifier — "structural", "cosine", "ensemble", "ner",
            etc. Used by audit logs and drift detection to know which
            scoring algorithm produced the value.
        threshold: The minimum value required for the extraction to be
            considered acceptable. Below this, callers should treat the
            result as low-confidence and consider fallback paths.
        passed: True iff value >= threshold. Provided as a property/field so
            callers don't repeat the comparison.
        metadata: Method-specific details — e.g. for cosine, the per-column
            score breakdown; for structural, the per-page parse success;
            for ensemble, the component scores. Audit logs preserve this
            verbatim; downstream code reads only by-key, not by-shape.
    """

    value: float
    method: str
    threshold: float
    passed: bool
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CosineConfidence:
    """Similarity-based confidence for embedding-driven extractors.

    Used by clio.extract.schema_map. The cosine score over
    sentence-transformer embeddings IS the confidence — there's no separate
    structural check. The threshold is the cleanroom-derived 0.65 default,
    overridable per-instance.
    """

    value: float
    method: str = "cosine"
    threshold: float = 0.65
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.value >= self.threshold


@dataclass(frozen=True)
class EnsembleConfidence:
    """Aggregated confidence across multiple methods.

    Reserved for future use — when an extractor combines vision + schema_map +
    structural checks, this aggregates the component scores. Component scores
    live in metadata for audit traceability.

    Aggregation rule (default): minimum of component scores. Stricter
    aggregations (geometric mean, weighted average) can be specified by the
    caller via metadata["aggregation_rule"].
    """

    value: float
    method: str = "ensemble"
    threshold: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.value >= self.threshold


def aggregate_minimum(scores: "list[ConfidenceScore]") -> EnsembleConfidence:
    """Aggregate a list of ConfidenceScore values via minimum.

    The result's threshold is the maximum threshold across components — the
    overall extraction passes only when EVERY component would have passed
    on its own. This is the strict-default aggregation; callers can write
    their own aggregator for looser policies.
    """
    if not scores:
        return EnsembleConfidence(value=0.0, threshold=0.0, metadata={"reason": "no_components"})
    min_value = min(s.value for s in scores)
    max_threshold = max(s.threshold for s in scores)
    return EnsembleConfidence(
        value=min_value,
        threshold=max_threshold,
        metadata={
            "aggregation_rule": "minimum",
            "components": [
                {"method": s.method, "value": s.value, "threshold": s.threshold, "passed": s.passed}
                for s in scores
            ],
        },
    )
