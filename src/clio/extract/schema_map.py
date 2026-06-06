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

"""clio.extract.schema_map — semantic column-name mapping via embeddings.

Foundation primitive for handling source-schema drift across heterogeneous
inputs. Given two column-name lists (source vs canonical/target), use a
sentence-transformer model to compute embedding cosine similarity, and
return the best mapping for each source column above a threshold.

This is the ETL feature that lets a pipeline survive a CSV provider
renaming a column from "Postal_Region_Code" to "ZIP" without manual
remapping — the embeddings recognize them as semantically equivalent
even though the strings share no characters.

Battle-tested defaults:
    * Model: all-MiniLM-L6-v2 (small, fast, high-recall on column-name
      semantics; ~80MB download, ~50ms/cold-load on Apple Silicon).
    * Threshold: 0.65 cosine similarity. Empirically calibrated on the
      RiskyEats DBPR schema-drift corpus; tighter thresholds miss real
      drift, looser thresholds produce noisy mappings.

Usage:
    from clio.extract.schema_map import SchemaMapper

    mapper = SchemaMapper()  # default model + threshold
    results = mapper.map_columns(
        source_columns=["Postal_Region_Code", "FullAddress", "MerchantName"],
        target_columns=["zip_code", "address", "business_name"],
    )
    for src, result in results.items():
        if result.confidence.passed:
            print(f"{src} -> {result.target_column} (cosine {result.confidence.value:.3f})")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from clio.extract.confidence import CosineConfidence
from clio.runtime.hardware import detect_device

logger = logging.getLogger(__name__)


# Model + threshold defaults from the cleanroom LLM_Mapper.py v4.0
# (battle-tested on the RiskyEats DBPR schema-drift corpus).
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_THRESHOLD = 0.65


@dataclass(frozen=True)
class MappingResult:
    """Outcome of mapping a single source column to its best target match.

    The MappingResult carries the cosine score and threshold via a
    ConfidenceScore-conforming CosineConfidence so downstream code (drift
    detection, audit envelopes) can reason about it via the Protocol.
    """

    source_column: str
    target_column: str
    confidence: CosineConfidence


class SchemaMapper:
    """Semantic column-name mapper using sentence-transformer embeddings.

    The model is loaded lazily on first call to map_columns(). For
    long-running adapters that want to pay the load cost upfront, call
    warm_up() explicitly during initialization.

    Args:
        model_name: Sentence-transformer model identifier. Default is the
            cleanroom-calibrated "all-MiniLM-L6-v2".
        threshold: Minimum cosine similarity to accept a mapping. Default
            0.65 from cleanroom calibration.
        device: Override device detection. None (default) auto-detects via
            clio.runtime.hardware.detect_device(). Pass "cpu", "mps", or
            "cuda" to force a specific device.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        threshold: float = DEFAULT_THRESHOLD,
        device: Optional[str] = None,
    ):
        self.model_name = model_name
        self.threshold = threshold
        self._device_override = device
        self._model = None
        self._device: Optional[str] = None

    @property
    def device(self) -> Optional[str]:
        """The device the model is currently loaded on (None if not loaded)."""
        return self._device

    def warm_up(self) -> None:
        """Eagerly load the sentence-transformer model.

        Optional. map_columns() will lazy-load on first call if this isn't
        invoked. Useful for adapters that want to pay the ~50-200ms load
        cost during initialization rather than on the first user request.

        Raises:
            ImportError: if torch / sentence-transformers aren't installed.
        """
        if self._model is not None:
            return

        from sentence_transformers import SentenceTransformer

        device = self._device_override or detect_device()
        logger.info("loading sentence-transformer model %s on device=%s", self.model_name, device)
        self._model = SentenceTransformer(self.model_name, device=device)
        self._device = device
        logger.info("model loaded; ready for inference")

    def map_columns(
        self,
        source_columns: Iterable[str],
        target_columns: Iterable[str],
    ) -> dict[str, MappingResult]:
        """Map each source column to its best-matching target column.

        For each source column, finds the target with the highest cosine
        similarity over sentence-transformer embeddings. Identity mappings
        (source == target as strings) are skipped — those don't need
        semantic mapping. Mappings below threshold are also skipped.

        Target columns are pre-processed by replacing underscores with
        spaces — this helps cosine match snake_case targets to
        natural-language source names (e.g. source "ZIP Code" matches
        target "zip_code" via the "zip code" descriptor).

        Args:
            source_columns: Column names from the input data.
            target_columns: Canonical/target column names the source should
                be mapped to.

        Returns:
            Dict keyed by source column name. Source columns whose best
            match is below threshold or is the identity are absent from
            the result.
        """
        source_list = [str(c) for c in source_columns]
        target_list = [str(c) for c in target_columns]

        if not source_list or not target_list:
            return {}

        if self._model is None:
            self.warm_up()

        import torch
        from sentence_transformers import util

        # Replace underscores with spaces so snake_case targets match natural
        # source descriptions cleanly under the embedding model.
        target_descriptions = [t.replace("_", " ").strip() for t in target_list]

        embeddings_source = self._model.encode(
            source_list, convert_to_tensor=True, device=self._device
        )
        embeddings_target = self._model.encode(
            target_descriptions, convert_to_tensor=True, device=self._device
        )
        cosine_scores = util.cos_sim(embeddings_source, embeddings_target)

        results: dict[str, MappingResult] = {}
        for i, source_col in enumerate(source_list):
            best_idx = int(torch.argmax(cosine_scores[i]).item())
            best_score = float(cosine_scores[i][best_idx].item())
            target_col = target_list[best_idx]

            # Identity skip: don't report no-op mappings.
            if source_col == target_col:
                continue

            confidence = CosineConfidence(
                value=best_score,
                threshold=self.threshold,
                metadata={
                    "model": self.model_name,
                    "device": self._device,
                    "target_description": target_descriptions[best_idx],
                },
            )

            if not confidence.passed:
                continue

            results[source_col] = MappingResult(
                source_column=source_col,
                target_column=target_col,
                confidence=confidence,
            )

        if results:
            logger.info(
                "mapped %d/%d source columns above threshold %.2f",
                len(results),
                len(source_list),
                self.threshold,
            )
        else:
            logger.info("no source columns mapped above threshold %.2f", self.threshold)

        return results
