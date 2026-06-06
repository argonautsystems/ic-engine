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

"""clio — foundation library for AI-driven semantic ETL.

Three subsystems:

    extract/    unstructured -> structured via AI (vision LLMs, schema mapping,
                normalization, NER) with confidence scoring.
    track/      persistent provenance: schema fingerprints, lineage parquet store,
                audit envelopes that compose with caller-side result envelopes.
    drift/      semantic drift detection over fingerprints; auto-remap when drift
                fits a known shape, alarm when it doesn't.

clio is the substrate. Domain libraries (ic-engine for portfolios, etlantis for
public-records ETL) sit on top of clio. Adapter libraries (InvestorClaw,
InvestorClaude, RiskyEats, rvmaps) sit on top of those. The three layers are
deliberate: clio stays free of domain assumptions so successor domains can
adopt it without inheriting portfolio or hospitality conventions.
"""

__version__ = "0.1.0"
