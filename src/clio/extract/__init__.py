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

"""clio.extract — unstructured input to structured output via AI.

Subsystems:

    vision        PDF/image -> JSON via vision LLM, parameterized prompt + schema.
                  Lifted Phase 1.5a from ic-engine pdf_extraction_dual_mode.py;
                  domain-specific heuristics (broker formats, account-section
                  parsing) intentionally NOT lifted — those stay in domain
                  libraries.

    schema_map    CSV column drift remapping via sentence-transformer embeddings
                  with cosine threshold. Lifted Phase 1.5b from cleanroom
                  LLM_Mapper.py (model all-MiniLM-L6-v2, threshold 0.65 — both
                  battle-tested defaults from the RiskyEats DBPR schema-drift
                  corpus).

    normalize     Name + address normalization for matching. Lifted Phase 1.5b
                  from cleanroom T1_normalization_utils.py. Eight string
                  transforms framework-agnostic; two pandas/polars helpers for
                  column-whitespace stripping.

    text          NER + relation extraction. Deferred to v0.2+ — no concrete
                  consumer yet.

    confidence    Common ConfidenceScore Protocol across subsystems; each
                  subsystem keeps its native scoring algorithm.
                  CosineConfidence (similarity-based, used by schema_map),
                  EnsembleConfidence (aggregated, reserved for future use).
                  vision.VisionConfidence conforms to the same shape.
"""

from clio.extract.confidence import (
    ConfidenceScore,
    CosineConfidence,
    EnsembleConfidence,
    aggregate_minimum,
)

__all__ = [
    "ConfidenceScore",
    "CosineConfidence",
    "EnsembleConfidence",
    "aggregate_minimum",
]
