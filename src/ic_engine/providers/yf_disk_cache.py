# Copyright 2026 InvestorClaw Contributors
# SPDX-License-Identifier: Apache-2.0
"""Disk-backed cache for yfinance.download to survive subprocess restarts.

The bug it fixes:
  Each ic-engine subprocess that calls `yf.download(symbols, period=...)`
  goes to Yahoo Finance over the network. After 1-2 such bursts (each
  fetching 200+ symbols), Yahoo IP-rate-limits us with `YFRateLimitError`
  or `Invalid Crumb` 401 responses. Subsequent subprocesses fail or stall.

The fix:
  Monkey-patch `yfinance.download` with a cache-aware wrapper. The cache
  is keyed on (sorted symbols, kwargs, today's date) and persisted to
  parquet under `$INVESTORCLAW_YF_CACHE_DIR` (default
  `/data/reports/.yf-cache/`). Subsequent subprocess invocations with the
  same call signature read the parquet directly — no network at all.

Cache TTL = same calendar date. Tomorrow's first call refreshes; cached
files for prior days stay on disk for diagnostics but are ignored.

Activation:
  Import this module once at process startup; it patches `yfinance.download`
  in place. We do this from `ic_engine/__init__.py` so any analyzer that
  imports yfinance gets the cached version transparently.

Disable for diagnostics:
  Set `INVESTORCLAW_YF_CACHE=disabled` to bypass the cache entirely.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger("ic_engine.yf_disk_cache")

CACHE_DIR = Path(
    os.environ.get(
        "INVESTORCLAW_YF_CACHE_DIR",
        "/data/reports/.yf-cache",
    )
)


def _cache_key(symbols: Any, kwargs: dict) -> str:
    """Build a deterministic cache key from call args."""
    if isinstance(symbols, str):
        sym_list = sorted(symbols.split())
    else:
        sym_list = sorted(map(str, symbols))

    # Only include kwargs that affect the result; skip progress/threads/etc.
    relevant = {
        k: kwargs.get(k)
        for k in (
            "period",
            "start",
            "end",
            "interval",
            "auto_adjust",
            "actions",
            "prepost",
        )
        if k in kwargs
    }
    payload = {
        "symbols": sym_list,
        "kwargs": {k: str(relevant[k]) for k in sorted(relevant)},
        "date": date.today().isoformat(),
    }
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:24]


def _save(df, cache_path: Path, meta_path: Path) -> None:
    """Persist a yfinance DataFrame to parquet, preserving MultiIndex columns."""
    import pandas as pd

    if df is None or df.empty:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(df.columns, pd.MultiIndex):
            # Parquet doesn't support MultiIndex columns; flatten + record original tuples.
            saved = df.copy()
            saved.columns = ["::".join(map(str, c)) for c in df.columns]
            saved.to_parquet(cache_path)
            meta_path.write_text(
                json.dumps(
                    {
                        "multiindex": True,
                        "columns": [list(c) for c in df.columns],
                        "names": list(df.columns.names),
                    }
                )
            )
        else:
            df.to_parquet(cache_path)
            meta_path.write_text(json.dumps({"multiindex": False}))
    except Exception as e:
        logger.warning(f"yf cache write failed: {type(e).__name__}: {e}")


def _load(cache_path: Path, meta_path: Path):
    """Load a yfinance DataFrame from parquet, restoring MultiIndex columns."""
    import pandas as pd

    try:
        df = pd.read_parquet(cache_path)
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        if meta.get("multiindex"):
            tuples = [tuple(c) for c in meta["columns"]]
            df.columns = pd.MultiIndex.from_tuples(tuples, names=meta.get("names"))
        return df
    except Exception as e:
        logger.warning(f"yf cache read failed: {type(e).__name__}: {e}")
        return None


def install() -> None:
    """Install the cached yfinance.download wrapper.

    Idempotent — safe to call multiple times.
    """
    if os.environ.get("INVESTORCLAW_YF_CACHE", "").lower() == "disabled":
        logger.info("yf_disk_cache: disabled via env")
        return

    try:
        import yfinance as yf
    except ImportError:
        logger.info("yf_disk_cache: yfinance not installed; nothing to wrap")
        return

    if getattr(yf.download, "_ic_engine_cached", False):
        return  # already installed

    real_download = yf.download

    def cached_download(symbols, *args, **kwargs):
        key = _cache_key(symbols, kwargs)
        cache_path = CACHE_DIR / f"{key}.parquet"
        meta_path = CACHE_DIR / f"{key}.meta.json"

        # Cache hit
        if cache_path.exists():
            df = _load(cache_path, meta_path)
            if df is not None:
                logger.info(f"yf cache HIT key={key}")
                return df

        # Cache miss → live fetch
        logger.info(f"yf cache MISS key={key}; fetching from Yahoo")
        df = real_download(symbols, *args, **kwargs)
        # Save only on success
        try:
            if df is not None and hasattr(df, "empty") and not df.empty:
                _save(df, cache_path, meta_path)
        except Exception:
            # Failure to cache is non-fatal; data still flows back to caller.
            pass
        return df

    cached_download._ic_engine_cached = True  # type: ignore[attr-defined]
    yf.download = cached_download
    logger.info(f"yf_disk_cache: installed (cache_dir={CACHE_DIR})")
