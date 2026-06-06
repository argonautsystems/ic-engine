# clio

Foundation library for AI-driven semantic ETL: extraction, tracking, drift detection.

`clio` is the substrate layer in a three-layer fleet architecture:

| Layer            | Role                                                                | Examples                                                                            |
| ---------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Foundation       | AI-driven primitives — extract structure from unstructured input, track provenance, detect drift, auto-remap | **`clio`** (this library)                                                            |
| Domain substrate | Domain-specific models, providers, computation, routing, built on top of `clio` | `ic-engine` (portfolio), `etlantis` (public-records ETL)                            |
| Adapter          | Runtime-specific glue: install scripts, manifests, slash commands, marketplace metadata | `InvestorClaw` (claws-runtime), `InvestorClaude` (Claude Code), `RiskyEats`, `rvmaps` |

The split is deliberate: domain assumptions stay out of `clio` so successor domains adopt it without inheriting portfolio or hospitality conventions, and adapters can swap their domain substrate without re-implementing extraction, lineage, or drift handling.

## Status

**Alpha. v0.1.0** — first surface complete. All four subsystems shipped:

- `clio.extract` — vision (PDF/image → JSON via litellm-backed vision LLM, parameterized prompt + schema), schema_map (sentence-transformer cosine), normalize (8 string transforms + pandas/polars helpers), confidence (Protocol + cosine + ensemble + min-aggregator).
- `clio.track` — content-addressable Fingerprint (SHA256), Polars-native Hive-partitioned parquet store, lineage trace + descendants, AuditEnvelope for adapter composition.
- `clio.drift` — eight-event taxonomy, pairwise + against-history compare, auto-remap via schema_map, log/file alarm targets.
- `clio.runtime` — hardware probing (NVIDIA / AMD / Intel / Apple Metal + macOS/Linux/WSL2 memory), `detect_device()` bridge for torch/sentence-transformers ("cuda" | "mps" | "cpu") with `CLIO_DEVICE` env override.

71 tests passing. uv.lock committed for reproducible installs.

## Subsystems

```
clio/
├── extract/          unstructured input → structured output via AI
│   ├── vision.py         PDF/image → JSON via vision LLM (parameterized)
│   ├── schema_map.py     CSV column drift remapping (sentence-transformer cosine, threshold 0.65)
│   ├── normalize.py      name + address normalization (8 transforms + DataFrame helpers)
│   ├── confidence.py     ConfidenceScore Protocol + Cosine + Ensemble + min-aggregator
│   └── text.py           NER + relation extraction (deferred to v0.2+)
├── track/            content-addressable provenance + lineage
│   ├── fingerprint.py    SHA256(source_uri ∥ extraction_date ∥ payload_hash)
│   ├── store.py          Polars-native parquet, Hive year/month partitioning
│   ├── lineage.py        trace() walks parent chain root-to-leaf; descendants() returns subtree
│   └── audit.py          minimal AuditEnvelope (fingerprint_id + clio_version)
├── drift/            semantic drift detection over fingerprints
│   ├── detect.py         8-event taxonomy: column_added/removed/renamed,
│   │                     dtype_changed, row_count_anomaly, confidence_dropped,
│   │                     extractor_version_change. compare() + detect_against_history().
│   ├── remap.py          auto_remap() resolves column_renamed via schema_map
│   └── alarm.py          surface() to log/JSONL file; severity_of() aggregates
└── runtime/          AI-aware execution
    ├── hardware.py       CPU/GPU/memory probing + detect_device() torch bridge
    ├── model_cache.py    sentence-transformers + vision-model cache (planned)
    └── gpu_memory.py     GPU memory budgeter (planned)
```

## Install

`clio` is published on the public GitLab mirror at <https://gitlab.com/perlowja/clio>. Install from source for now (PyPI publication will follow v0.1.0).

```bash
git clone https://gitlab.com/perlowja/clio.git
cd clio
uv sync
```

Or as a dependency in another project:

```toml
# pyproject.toml
dependencies = [
    "clio @ git+https://gitlab.com/perlowja/clio.git@v0.1.0",
]
```

## Quick start: vision extraction

```python
from clio.extract.vision import extract

result = extract(
    pdf_path="/path/to/document.pdf",
    prompt='''Extract all entities mentioned in this document.
              Return JSON of shape: {"entities": [{"name": str, "role": str}]}''',
    model="claude-sonnet-4-6",
    max_pages=5,
)

if result.succeeded:
    print(result.data["entities"])
    print(f"Confidence: {result.confidence.value} (method: {result.confidence.method})")
```

The prompt and schema are caller-supplied. `clio.extract.vision` is a foundation primitive that takes a document, sends pages to a vision-capable LLM via `litellm`, and parses JSON out of the response. Domain knowledge (broker statement formats, license filing schemas, restaurant menu structures) lives in the calling library, not here.

Provider routing is litellm-shaped. Pass any vision-capable model string:

- `claude-sonnet-4-6`
- `openai/gpt-4o`
- `vertex_ai/gemini-2.5-pro`

Set `CLIO_VISION_API_KEY` (or pass `api_key=` directly), or use the provider's native env var that litellm picks up (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.).

## Quick start: schema mapping (column drift remap)

```python
from clio.extract.schema_map import SchemaMapper
import polars as pl

# Source schema differs from canonical target — drifted column names.
source = pl.DataFrame({"restaurant_nm": ["..."], "addr_line_1": ["..."]})
canonical = pl.DataFrame({"name": ["..."], "address": ["..."]})

mapper = SchemaMapper()  # all-MiniLM-L6-v2, cosine threshold 0.65
mappings = mapper.map_columns(source.columns, canonical.columns)
# {"restaurant_nm": MappingResult(target="name", confidence=CosineConfidence(value=0.78, passed=True)), ...}
```

## Quick start: tracking + drift

```python
from clio.track import Fingerprint, write, scan
from clio.drift import compare, severity_of

fp1 = Fingerprint.compute(source_uri="...", extraction_date="2026-04-26", payload={"holdings": [...]})
write(fp1)

# Later: a new extraction from the same source
fp2 = Fingerprint.compute(source_uri="...", extraction_date="2026-04-27", payload={"holdings": [...]})
events = compare(fp1, fp2)
print(severity_of(events))  # "info" | "warn" | "error"
```

## Hardware detection

```python
from clio.runtime.hardware import HardwareProfile, detect_device

hw = HardwareProfile()
print(hw)  # human-readable summary
print(detect_device())  # "cuda" | "mps" | "cpu" — for torch / sentence-transformers
```

Detects NVIDIA, AMD ROCm, Intel Arc/iGPU, and Apple Metal with unified-memory awareness. `CLIO_DEVICE` env var overrides for CI determinism.

## Methodology

`clio` is the implementation of the AI-driven semantic-ETL methodology covered by the Tina agreement (Feb 2026 DocuSign F5124E6D-...): semantic validation, automated extraction, transformation, and loading. The library is the public face of that methodology — published Apache 2.0 so the substrate is a contributable open core, with domain libraries (`ic-engine`, `etlantis`) and adapters (`InvestorClaw`, `RiskyEats`) layered on top per the consumer's deployment posture.

## License

Apache 2.0. See [LICENSE](./LICENSE).

## Contributing

Source-of-truth bare repo: `root@argonas:/mnt/datapool/git/clio.git` (internal). Public mirror: <https://gitlab.com/perlowja/clio>. Pull requests via GitLab merge requests.

Commit author convention: `Jason Perlow <jperlow@gmail.com>`. Pre-commit: run `ruff check --fix && ruff format` before committing.
