# Phase 2 Data Stages — Developer Guide

This document describes the three foundation stages introduced in Phase 2
of InvestorClaw's staged pipeline architecture:

```
holdings_input.csv / .json
     │
     ▼
 P0: HoldingsLoader.load()              [Phase 1]  → PortfolioData
     │
     ▼
 P1a: DataDownloader.download()         [Phase 2]  → {sym → cache Path}
     │
     ▼
 P1b: DataExtractor.extract()           [Phase 2]  → {sym → pl.DataFrame}
     │
     ▼
 P1c: DataTransformer.transform()       [Phase 2]  → {sym → canonical pl.DataFrame}
     │
     ▼
 P2+: PerformanceStage, BondsStage, …   [Phase 1 commands]
```

The three stages live in:

* `internal/data_downloader.py`
* `internal/data_extractor.py`
* `internal/data_transformer.py`

Each has a single clearly-defined responsibility; they communicate via
typed dataclasses (`DownloadResult`, `ExtractionResult`, `TransformConfig`)
and plain dicts of `polars.DataFrame`.

Reference patterns are documented at the top of each module:
ETLANTIS HTTPDownloader (retry/rate-limit/cache), RiskyEats SunBiz parser
(cache-first + integrity checks), ETLANTIS GenericT0Transform
(config-driven schema standardization).

---

## 1. DataDownloader (P1a)

Retry / rate-limit / cache-TTL wrapper around provider-specific adapters.

```python
from pathlib import Path
from internal.data_downloader import DataDownloader

downloader = DataDownloader(cache_dir=Path("~/.investorclaw/data_cache"))

results = downloader.download(
    provider="yfinance",
    symbols=["AAPL", "MSFT", "GOOG"],
    date_range=("2024-01-01", "2024-12-31"),
)
# results: Dict[symbol, DownloadResult]
print(downloader.stats.summary())
```

### Supported providers (out of the box)

| Provider       | Endpoint                         | API key env               | Rate limit (default) | Cache TTL |
|----------------|----------------------------------|---------------------------|----------------------|-----------|
| `yfinance`     | yfinance SDK                     | —                         | 0.5 s (≈ 2 req/s)    | 24 h      |
| `finnhub`      | `/stock/candle`                  | `FINNHUB_API_KEY`         | 1.05 s               | 24 h      |
| `polygon`      | `/v2/aggs/ticker/.../range/…`    | `POLYGON_API_KEY`         | 12.5 s (5/min free)  | 24 h      |
| `alphavantage` | `TIME_SERIES_DAILY`              | `ALPHA_VANTAGE_API_KEY`   | 12.5 s (5/min free)  | 24 h      |
| `newsapi`      | `/everything`                    | `NEWSAPI_KEY`             | 1.0 s                | 1 h       |

Override any of these via `DataProviderConfig`:

```python
from internal.data_downloader import DataDownloader, DataProviderConfig

configs = {
    "finnhub": DataProviderConfig(
        provider_name="finnhub",
        base_url="https://finnhub.io/api/v1/",
        rate_limit_delay=0.2,          # paid tier: 300/min
        retry_count=5,
        cache_max_age_hours=6.0,       # fresher cache
        api_key_env="FINNHUB_PREMIUM_KEY",
    )
}
downloader = DataDownloader(provider_configs=configs)
```

### Retry + skip classification

| HTTP status                 | Action                                                 |
|-----------------------------|--------------------------------------------------------|
| 200                         | success, cache result                                  |
| 404                         | **skip** (symbol not available; not a hard failure)    |
| 429, 500, 502, 503, 504     | **retry** with linear backoff (`retry_delay * attempt`)|
| Other / network errors      | retry up to `retry_count` then record failure          |

The exact code lists are configurable via
`DataProviderConfig.retry_status_codes` / `skip_status_codes`.

### Cache layout

Cache files live under `cache_dir` (default `~/.investorclaw/data_cache/`):

```
yfinance__AAPL__<sha1>.bin          # canonical key used for freshness check
yfinance__AAPL__<sha1>.parquet      # companion (pandas DataFrames)
finnhub__AAPL__<sha1>.bin           # JSON payload (REST providers)
```

A cache entry is **fresh** if `mtime >= now - cache_max_age_hours`.
Set `cache_max_age_hours=0` to disable caching.

### Statistics

`downloader.stats` (a `DownloadStats` dataclass) tracks aggregate and
per-provider counters: `requests`, `cache_hits`, `cache_misses`,
`retries`, `skipped_404`, `failures`. Call `.summary()` for a dict
suitable for logging or metric emission.

---

## 2. DataExtractor (P1b)

Parses the cache files produced by `DataDownloader` into standardized
Polars DataFrames, with integrity checks and statistics.

```python
from internal.data_extractor import DataExtractor, DEFAULT_SCHEMAS

extractor = DataExtractor()
result = extractor.extract(
    raw_data={"AAPL": results["AAPL"].cache_path,
              "MSFT": results["MSFT"].cache_path},
    schema=DEFAULT_SCHEMAS["yfinance_ohlcv"],
)

if not result.integrity_valid:
    print("Missing:", result.missing_symbols)
    print("Errors:", result.errors)
print(result.summary())
```

### Integrity checks

* **Empty DataFrame** → symbol added to `missing_symbols`.
* **All-NaN price column** (`schema.nan_sensitive_columns`) → flagged.
* **Missing required column** (`schema.required_columns`) → flagged.
* **Parse error** → recorded in `result.errors` (per symbol).

`result.integrity_valid` is `False` if any symbol tripped any check.

### Built-in schemas

| Key                      | Format   | Required columns                                  |
|--------------------------|----------|---------------------------------------------------|
| `yfinance_ohlcv`         | parquet  | Open / High / Low / Close / Volume                |
| `finnhub_candles`        | json     | c / h / l / o / t / v                             |
| `polygon_aggs`           | json     | c / h / l / o / t / v (under `results[...]`)      |
| `alphavantage_daily`     | json     | 1. open … 5. volume (under `Time Series (Daily)`) |
| `newsapi_everything`     | json     | title / publishedAt / source (under `articles`)   |

Register new schemas by passing a dict to the constructor:

```python
extractor = DataExtractor(schemas={
    "my_source": ExtractionSchema(source="my_source", format="csv",
                                   required_columns=["date","close"]),
})
```

---

## 3. DataTransformer (P1c)

Config-driven schema standardization. Reads
`config/data_transform_rules.json` and applies, per source:

* `field_mappings`     — raw column → canonical column
* `type_coercions`     — canonical column → polars dtype
* `derived_fields`     — name → safe expression (see below)
* `drop_columns`       — columns to remove before mapping
* `add_columns`        — literal columns (e.g. `source: yfinance`)
* `null_values`        — string values to treat as null

```python
from pathlib import Path
from internal.data_transformer import DataTransformer

transformer = DataTransformer.from_rules_file(
    Path("config/data_transform_rules.json"))

standardized = transformer.transform(
    extracted_data=result.data,      # {sym → Polars DataFrame}
    source="yfinance_ohlcv",
)
```

### Canonical schema

After transformation, every DataFrame conforms to:

```
symbol : Utf8
date   : Date          (or preserved as index for yfinance)
open   : Float64
high   : Float64
low    : Float64
close  : Float64
adj_close : Float64
volume : Int64
source : Utf8          (literal: "yfinance" | "finnhub" | "polygon" | ...)
```

### Safe derived-field expression language

To avoid exposing an `eval` attack surface in a JSON config, `derived_fields`
uses a small colon-separated DSL:

| Expression                     | Meaning                                  |
|--------------------------------|------------------------------------------|
| `copy:close`                   | alias another column                     |
| `from_epoch:t`                 | Unix seconds → polars Date               |
| `divide:volume:1000`           | `volume / 1000` (float)                  |
| `multiply:close:100`           | `close * 100`                            |
| `weight_pct:ratio`             | `ratio * 100`                            |

Unknown ops are logged and skipped — they never crash the pipeline.

---

## 4. Adding a new data source

1. **Downloader adapter** — add a `_foo_adapter(...)` function in
   `data_downloader.py` that takes `(symbol, date_range, api_key, timeout)`
   and raises `_ProviderError(..., status_code=...)` on failure. Register
   it in `DataDownloader._invoke_adapter()` and add a default config in
   `DEFAULT_PROVIDER_CONFIGS`.
2. **Extractor schema** — add an `ExtractionSchema` entry in
   `DEFAULT_SCHEMAS` describing the required columns and format. If the
   payload is a JSON dict with records nested under a key, set
   `json_records_path=["foo", "bar"]`.
3. **Transform rule** — add a new entry to `transform_rules` in
   `config/data_transform_rules.json` with `field_mappings` and
   `type_coercions` mapping your columns into the canonical schema, plus
   `add_columns: {"source": "foo"}`.
4. **Tests** — add cases to `internal/test_data_stages.py` covering a
   happy-path download (with mocked adapter), extract, and transform.

---

## 5. Running the tests

```bash
# From the InvestorClaw project root:
python -m unittest internal.test_data_stages -v
```

All provider adapters are mocked via `unittest.mock.patch` so the tests
make **no real network calls** — they run in <2 s offline.

---

## 6. Integration with the Phase 1 command suite

The new stages are *additive*. Nothing in the existing command suite
(`commands/portfolio_complete.py` et al.) has been changed by this phase.
Commands that currently invoke `yfinance`/`finnhub` directly can be
migrated incrementally to go through `DataDownloader` → `DataExtractor` →
`DataTransformer` instead — the immediate payoff is that the second run
of the same portfolio becomes an all-cache-hit path.

Suggested one-liner integration for new call sites:

```python
from internal.data_downloader import DataDownloader
from internal.data_extractor  import DataExtractor, DEFAULT_SCHEMAS
from internal.data_transformer import DataTransformer
from pathlib import Path

def load_prices(symbols, date_range):
    dl  = DataDownloader()
    ex  = DataExtractor()
    tx  = DataTransformer.from_rules_file(
        Path(__file__).resolve().parent.parent
        / "config" / "data_transform_rules.json"
    )
    paths = {s: r.cache_path for s, r in
             dl.download("yfinance", symbols, date_range).items()
             if r.success}
    extracted = ex.extract(paths, DEFAULT_SCHEMAS["yfinance_ohlcv"])
    return tx.transform(extracted.data, "yfinance_ohlcv")
```
