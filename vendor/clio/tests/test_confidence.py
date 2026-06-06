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

"""Tests for clio.extract.confidence — Protocol conformance + ensemble aggregation."""

from clio.extract.confidence import (
    ConfidenceScore,
    CosineConfidence,
    EnsembleConfidence,
    aggregate_minimum,
)


def test_cosine_confidence_passes_above_threshold():
    c = CosineConfidence(value=0.8, threshold=0.65)
    assert c.passed is True


def test_cosine_confidence_fails_below_threshold():
    c = CosineConfidence(value=0.5, threshold=0.65)
    assert c.passed is False


def test_cosine_confidence_default_threshold():
    """Default threshold is the cleanroom-calibrated 0.65."""
    c = CosineConfidence(value=0.7)
    assert c.threshold == 0.65
    assert c.passed is True


def test_cosine_confidence_method_label():
    c = CosineConfidence(value=0.7)
    assert c.method == "cosine"


def test_cosine_confidence_conforms_to_protocol():
    c = CosineConfidence(value=0.7, metadata={"target": "x"})
    assert isinstance(c, ConfidenceScore)


def test_ensemble_confidence_conforms_to_protocol():
    e = EnsembleConfidence(value=0.5, threshold=0.5)
    assert isinstance(e, ConfidenceScore)


def test_aggregate_minimum_picks_lowest_value():
    components = [
        CosineConfidence(value=0.9, threshold=0.65),
        CosineConfidence(value=0.7, threshold=0.65),
    ]
    agg = aggregate_minimum(components)
    assert agg.value == 0.7


def test_aggregate_minimum_uses_max_threshold():
    components = [
        CosineConfidence(value=0.9, threshold=0.65),
        EnsembleConfidence(value=0.7, threshold=0.5),
    ]
    agg = aggregate_minimum(components)
    assert agg.threshold == 0.65


def test_aggregate_minimum_metadata_lists_components():
    components = [
        CosineConfidence(value=0.9, threshold=0.65),
        CosineConfidence(value=0.7, threshold=0.65),
    ]
    agg = aggregate_minimum(components)
    assert agg.metadata["aggregation_rule"] == "minimum"
    assert len(agg.metadata["components"]) == 2


def test_aggregate_minimum_empty_returns_zero_value():
    agg = aggregate_minimum([])
    assert agg.value == 0.0
    assert agg.metadata.get("reason") == "no_components"


def test_vision_confidence_conforms_to_protocol():
    """Phase 1.5c reconciliation: vision.VisionConfidence is now a formal
    member of the ConfidenceScore Protocol."""
    from clio.extract.vision import VisionConfidence

    vc = VisionConfidence(value=0.7)
    assert isinstance(vc, ConfidenceScore)
    assert vc.method == "structural"
    assert vc.passed is True


def test_vision_confidence_aggregates_with_cosine():
    """Mixed-method aggregation: vision + schema_map confidence in one ensemble."""
    from clio.extract.vision import VisionConfidence

    components = [
        VisionConfidence(value=0.8, threshold=0.5),
        CosineConfidence(value=0.7, threshold=0.65),
    ]
    agg = aggregate_minimum(components)
    assert agg.value == 0.7
    methods = {c["method"] for c in agg.metadata["components"]}
    assert methods == {"structural", "cosine"}
