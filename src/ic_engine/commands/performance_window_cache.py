# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Persistent incremental OHLCV panel for performance-window requests."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd
import polars as pl

logger = logging.getLogger(__name__)

PANEL_SCHEMA_VERSION = 2
_OHLCV_FIELDS = ("Open", "High", "Low", "Close", "Volume")
_COMMON_WINDOWS = ("1w", "1mo", "3mo", "ytd", "1y")


def panel_root() -> Path:
    return Path(os.environ.get("INVESTORCLAW_OHLCV_PANEL_DIR", "/data/ohlcv_panel")).expanduser()


def _safe_symbol(symbol: str) -> str:
    # No dots in the stem: a dot would let Path.with_suffix / extension handling
    # truncate the disambiguating digest (e.g. "BRK.B.<hash>" -> "BRK.B"), so
    # distinct symbols could collide on the same panel/meta/lock file.
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in symbol.upper())
    digest = hashlib.sha256(symbol.upper().encode("utf-8")).hexdigest()[:10]
    return f"{cleaned}_{digest}"


def _symbol_paths(symbol: str) -> tuple[Path, Path]:
    base = panel_root() / "symbols" / _safe_symbol(symbol)
    # Append extensions instead of with_suffix so the digest is never dropped.
    return base.parent / f"{base.name}.parquet", base.parent / f"{base.name}.meta.json"


def _symbol_dividend_path(symbol: str) -> Path:
    base = panel_root() / "dividends" / _safe_symbol(symbol)
    return base.parent / f"{base.name}.dividends.json"


def _symbol_lock_path(symbol: str) -> Path:
    base = panel_root() / "locks" / _safe_symbol(symbol)
    return base.parent / f"{base.name}.lock"


@contextlib.contextmanager
def _symbol_file_lock(symbol: str) -> Iterator[None]:
    """Exclusive per-symbol lock covering panel/meta/dividend read-modify-write."""
    path = _symbol_lock_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _atomic_write_parquet(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _engine_version() -> str:
    try:
        from ic_engine import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def _result_cache_key(holdings_hash: str, start_date: str, end_date: str, period: str) -> str:
    payload = json.dumps(
        {
            "holdings_hash": holdings_hash,
            "start_date": start_date,
            "end_date": end_date,
            # Include the requested period token so a "custom" request with the
            # same start/end as a "1w" token does not collide and return the
            # other request's period metadata.
            "period": period or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def result_cache_path(
    holdings_hash: str, start_date: str, end_date: str, period: str = ""
) -> Path:
    key = _result_cache_key(holdings_hash, start_date, end_date, period)
    return panel_root() / "results" / f"performance_window.{key}.json"


def _result_lock_path(
    holdings_hash: str, start_date: str, end_date: str, period: str
) -> Path:
    key = _result_cache_key(holdings_hash, start_date, end_date, period)
    return panel_root() / "locks" / f"result.{key}.lock"


@contextlib.contextmanager
def result_cache_lock(
    holdings_hash: str, start_date: str, end_date: str, period: str = ""
) -> Iterator[None]:
    """Serialize load→compute→save for one result-cache key.

    Concurrent same-key writers (e.g. the warmth cron and an agent request
    landing in the same second) would otherwise race the atomic replace with
    different ``generated_at``/``run_id``/HMAC; the lock makes the first writer
    compute and the rest read the fresh cache.
    """
    path = _result_lock_path(holdings_hash, start_date, end_date, period)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _envelope_age_seconds(envelope: dict[str, Any]) -> float | None:
    meta = (envelope.get("section_meta") or {}).get("performance_window") or {}
    stamp = meta.get("computed_at") or envelope.get("generated_at")
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - parsed).total_seconds()
    except Exception:
        return None


def load_result_cache(
    holdings_hash: str, start_date: str, end_date: str, period: str = ""
) -> dict[str, Any] | None:
    path = result_cache_path(holdings_hash, start_date, end_date, period)
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        section_meta = (envelope.get("section_meta") or {}).get("performance_window") or {}
        # Engine-version stamp (signed into section_meta at build time): a code
        # change to the window math invalidates the cache so a redeploy does not
        # serve stale-shaped envelopes.
        if section_meta.get("engine_version") != _engine_version():
            logger.info("performance-window result cache version miss for %s", path)
            return None
        # TTL freshness: lets the warmth cron / intraday refresh re-pull a window
        # whose start/end have not changed (same trading day) once it is stale.
        ttl = section_meta.get("ttl_seconds")
        age = _envelope_age_seconds(envelope)
        if ttl is not None and age is not None and age > float(ttl):
            logger.info("performance-window result cache stale (%ss > %ss) for %s", age, ttl, path)
            return None
        from ic_engine.runtime.envelope import validate_envelope

        validate_envelope(envelope)
        return envelope
    except Exception as exc:
        logger.warning("performance-window result cache read failed for %s: %s", path, exc)
        return None


def save_result_cache(
    holdings_hash: str,
    start_date: str,
    end_date: str,
    envelope: dict[str, Any],
    period: str = "",
) -> None:
    path = result_cache_path(holdings_hash, start_date, end_date, period)
    # The envelope is written byte-for-byte as signed; freshness/version live in
    # the (signed) section_meta so HMAC validation on load still passes.
    try:
        _atomic_write_text(path, json.dumps(envelope, indent=2, sort_keys=True))
    except OSError as exc:
        logger.warning("performance-window result cache write failed for %s: %s", path, exc)


def _load_meta(symbol: str) -> dict[str, Any]:
    _panel_path, meta_path = _symbol_paths(symbol)
    if not meta_path.exists():
        return {"schema_version": PANEL_SCHEMA_VERSION, "symbol": symbol.upper()}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("schema_version") != PANEL_SCHEMA_VERSION:
            return {"schema_version": PANEL_SCHEMA_VERSION, "symbol": symbol.upper()}
        meta["attempted_ranges"] = _normalize_ranges(meta.get("attempted_ranges") or [])
        return meta
    except (OSError, json.JSONDecodeError):
        return {"schema_version": PANEL_SCHEMA_VERSION, "symbol": symbol.upper()}


def _save_meta(symbol: str, meta: dict[str, Any]) -> None:
    _panel_path, meta_path = _symbol_paths(symbol)
    meta = {**meta, "schema_version": PANEL_SCHEMA_VERSION, "symbol": symbol.upper()}
    try:
        _atomic_write_text(meta_path, json.dumps(meta, indent=2, sort_keys=True))
    except OSError as exc:
        logger.warning("OHLCV panel meta write failed for %s: %s", symbol, exc)


def _load_symbol_panel(symbol: str) -> pd.DataFrame:
    panel_path, _meta_path = _symbol_paths(symbol)
    if not panel_path.exists():
        return pd.DataFrame(columns=list(_OHLCV_FIELDS))
    try:
        df = pd.read_parquet(panel_path)
        if "Date" in df.columns:
            df = df.set_index(pd.to_datetime(df["Date"]))
            df = df.drop(columns=["Date"])
        else:
            df.index = pd.to_datetime(df.index)
        df.index = df.index.tz_localize(None).normalize()
        keep = [c for c in _OHLCV_FIELDS if c in df.columns]
        return df[keep].sort_index().groupby(level=0).last()
    except Exception as exc:
        logger.warning("OHLCV panel read failed for %s: %s", symbol, exc)
        return pd.DataFrame(columns=list(_OHLCV_FIELDS))


def _save_symbol_panel(symbol: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    out = df.copy()
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    out = out[[c for c in _OHLCV_FIELDS if c in out.columns]].sort_index().groupby(level=0).last()
    panel_path, _meta_path = _symbol_paths(symbol)
    try:
        _atomic_write_parquet(panel_path, out.reset_index(names="Date"))
    except Exception as exc:
        logger.warning("OHLCV panel write failed for %s: %s", symbol, exc)


def _polars_to_indexed_pandas(price_pl: pl.DataFrame) -> pd.DataFrame:
    price_pd = price_pl.to_pandas()
    date_col = next((c for c in ("Date", "Datetime", "index") if c in price_pd.columns), None)
    if date_col:
        price_pd = price_pd.set_index(pd.to_datetime(price_pd[date_col]))
        price_pd = price_pd.drop(columns=[date_col])
    else:
        price_pd.index = pd.to_datetime(price_pd.index)
    price_pd.index = price_pd.index.tz_localize(None).normalize()
    return price_pd.sort_index()


def _extract_metric(price_pd: pd.DataFrame, symbol: str, metric: str) -> pd.Series | None:
    if isinstance(price_pd.columns, pd.MultiIndex):
        for key in ((metric, symbol), (symbol, metric)):
            if key in price_pd.columns:
                return price_pd[key]
    flat = f"{metric}_{symbol}"
    if flat in price_pd.columns:
        return price_pd[flat]
    if metric in price_pd.columns:
        return price_pd[metric]
    return None


def _symbol_frame_from_fetch(price_pl: pl.DataFrame, symbol: str) -> pd.DataFrame:
    if price_pl is None or price_pl.is_empty():
        return pd.DataFrame(columns=list(_OHLCV_FIELDS))
    price_pd = _polars_to_indexed_pandas(price_pl)
    out = pd.DataFrame(index=price_pd.index)
    for metric in _OHLCV_FIELDS:
        series = _extract_metric(price_pd, symbol, metric)
        if series is not None:
            out[metric] = pd.to_numeric(series, errors="coerce")
    if "Close" not in out.columns:
        return pd.DataFrame(columns=list(_OHLCV_FIELDS))
    out = out.dropna(subset=["Close"])
    return out.sort_index().groupby(level=0).last()


def _merge_panel(existing: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    frames = [df for df in (existing, fetched) if df is not None and not df.empty]
    if not frames:
        return pd.DataFrame(columns=list(_OHLCV_FIELDS))
    merged = pd.concat(frames).sort_index().groupby(level=0).last()
    return merged[[c for c in _OHLCV_FIELDS if c in merged.columns]]


def _looks_like_split(existing: pd.DataFrame, fetched: pd.DataFrame) -> bool:
    if existing is None or existing.empty or fetched is None or fetched.empty:
        return False
    if "Close" not in existing.columns or "Close" not in fetched.columns:
        return False
    first_new_date = pd.Timestamp(fetched.index.min())
    prev = pd.to_numeric(
        existing.loc[existing.index < first_new_date, "Close"], errors="coerce"
    ).dropna()
    new = pd.to_numeric(fetched["Close"], errors="coerce").dropna()
    if prev.empty or new.empty:
        return False
    prev_close = float(prev.iloc[-1])
    new_close = float(new.iloc[0])
    if prev_close <= 0 or new_close <= 0:
        return False
    # Any large day-over-day discontinuity (≥1.5x either direction) signals a
    # split/adjustment-basis change rather than a normal return — this covers
    # odd-ratio (7:1, 20:1) and reverse splits, not just {2,3,4,5,10}. A genuine
    # >50% single-session move on adjusted closes is essentially never a real
    # equity return, so the worst case is a (rare) extra full refetch.
    ratio = max(prev_close / new_close, new_close / prev_close)
    return ratio >= 1.5


def _fetch_dividend_events(
    symbol: str, start_date: str, end_date: str, aggregate_value: float
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch dated dividend events for a symbol/range.

    Returns ``(events, ok)``. ``ok`` is True when a provider actually responded
    (even with zero dividends) so the caller can distinguish a legitimate empty
    snapshot (e.g. a dividend was removed -> replace the store) from a transient
    failure (keep the prior store, retry later). ``ok`` is False only when every
    source raised and no aggregate fallback was available.
    """
    sym = symbol.upper()
    events: list[dict[str, Any]] = []
    ok = False
    try:
        from ic_engine.providers.price_provider import MassiveProvider

        # Authoritative variant: ok=False on a transient/entitlement failure,
        # ok=True (even with []) on a genuine response, so the caller never wipes
        # a valid stored dividend on a blip.
        rows, massive_ok = MassiveProvider().get_dividends_authoritative(sym, limit=1000)
        ok = ok or massive_ok
        for row in rows or []:
            raw_date = row.get("ex_date") or row.get("pay_date")
            if not raw_date:
                continue
            event_date = str(raw_date)[:10]
            if start_date <= event_date <= end_date:
                amount = float(row.get("cash_amount") or 0.0)
                if amount > 0.0:
                    events.append({"date": event_date, "amount": amount, "source": "massive"})
        if events:
            return events, True
    except Exception as exc:
        logger.debug("Massive dividend-event fetch failed for %s: %s", sym, exc)

    try:
        import yfinance as yf

        div_data = yf.Ticker(sym).dividends
        ok = True  # yfinance call completed (a raise is caught below)
        if div_data is not None and not div_data.empty:
            for idx, value in div_data.items():
                event_date = pd.Timestamp(idx).date().isoformat()
                if start_date <= event_date <= end_date:
                    amount = float(value or 0.0)
                    if amount > 0.0:
                        events.append({"date": event_date, "amount": amount, "source": "yfinance"})
        if events:
            return events, True
    except Exception as exc:
        logger.debug("yfinance dividend-event fetch failed for %s: %s", sym, exc)

    if aggregate_value > 0.0:
        # Compatibility for tests/mocks that model only the legacy aggregate.
        return [
            {"date": end_date, "amount": float(aggregate_value), "source": "aggregate_fallback"}
        ], True
    return events, ok


def _normalize_dividend_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, float], dict[str, Any]] = {}
    for event in events or []:
        try:
            event_date = pd.Timestamp(event.get("date")).date().isoformat()
            amount = float(event.get("amount") or 0.0)
        except Exception:
            continue
        if amount <= 0.0:
            continue
        key = (event_date, round(amount, 12))
        merged[key] = {
            "date": event_date,
            "amount": amount,
            "source": str(event.get("source") or "provider"),
        }
    return sorted(merged.values(), key=lambda row: (row["date"], row["amount"]))


def _load_dividend_events(symbol: str) -> list[dict[str, Any]]:
    path = _symbol_dividend_path(symbol)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("symbol") != symbol.upper():
            return []
        return _normalize_dividend_events(payload.get("events") or [])
    except (OSError, json.JSONDecodeError):
        return []


def _save_dividend_events(symbol: str, events: list[dict[str, Any]]) -> None:
    path = _symbol_dividend_path(symbol)
    payload = {
        "schema_version": PANEL_SCHEMA_VERSION,
        "symbol": symbol.upper(),
        "events": _normalize_dividend_events(events),
    }
    try:
        _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))
    except OSError as exc:
        logger.warning("OHLCV panel dividend-event write failed for %s: %s", symbol, exc)


def _sum_dividends(events: Iterable[dict[str, Any]], start_date: str, end_date: str) -> float:
    total = 0.0
    for event in events or []:
        try:
            event_date = pd.Timestamp(event.get("date")).date().isoformat()
            amount = float(event.get("amount") or 0.0)
        except Exception:
            continue
        if start_date <= event_date <= end_date:
            total += amount
    return total


def _fetch_symbol_range(
    analyzer: Any, symbol: str, start_date: str, end_date: str
) -> tuple[pd.DataFrame, float]:
    """Fetch only the price bars for [start_date, end_date] (delta path).

    Dividends are synced separately, at most once per symbol per window, so the
    per-range delta fetch does not re-pull full dividend history for every hole.
    Returns the OHLCV frame plus the legacy aggregate dividend value (used only
    as a compatibility fallback for mocks that model the aggregate, not events).
    """
    logger.info("OHLCV panel delta fetch %s %s..%s", symbol, start_date, end_date)
    try:
        price_pl, dividends, fetched_symbols = analyzer.fetch_equity_data(
            [symbol], start_date, end_date, exact_range=True
        )
    except ValueError as exc:
        # A typed "no data" outcome means the provider responded and the range
        # legitimately has no bars (delisted / market-closed span) — a SUCCESS
        # with an empty result, which the caller finalizes as attempted. Any
        # other error (network/API/transient) propagates so the hole stays
        # retryable rather than becoming permanent.
        #
        # KNOWN BOUND: the underlying panel collapses a both-providers-down
        # transient into the same empty -> "No data returned", so a simultaneous
        # Massive+yfinance outage during one delta could finalize a PAST day's
        # hole. This self-heals: today is never finalized (_record_attempted),
        # the result-cache TTL recomputes relative windows, and any later
        # corporate-action rebuild re-fetches the full window. Acceptable vs the
        # alternative of refetching a genuinely-delisted symbol every call.
        if "No data returned" in str(exc):
            return pd.DataFrame(columns=list(_OHLCV_FIELDS)), 0.0
        raise
    fetched_set = {str(s).upper() for s in fetched_symbols}
    aggregate_value = float(dividends.get(symbol.upper(), 0.0) or 0.0)
    if symbol.upper() not in fetched_set:
        return pd.DataFrame(columns=list(_OHLCV_FIELDS)), aggregate_value
    return _symbol_frame_from_fetch(price_pl, symbol.upper()), aggregate_value


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(int((end - start).days) + 1)]


def _normalize_ranges(ranges: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[tuple[date, date]] = []
    for row in ranges or []:
        try:
            s = pd.Timestamp(row.get("start")).date()
            e = pd.Timestamp(row.get("end")).date()
        except Exception:
            continue
        if s <= e:
            normalized.append((s, e))
    if not normalized:
        return []
    normalized.sort()
    merged: list[list[date]] = []
    for s, e in normalized:
        if not merged or s > merged[-1][1] + timedelta(days=1):
            merged.append([s, e])
        else:
            merged[-1][1] = max(merged[-1][1], e)
    return [{"start": s.isoformat(), "end": e.isoformat()} for s, e in merged]


def _add_attempted_range(meta: dict[str, Any], start: date, end: date) -> None:
    if start > end:
        return
    ranges = list(meta.get("attempted_ranges") or [])
    ranges.append({"start": start.isoformat(), "end": end.isoformat()})
    meta["attempted_ranges"] = _normalize_ranges(ranges)
    meta["attempted_through"] = max(
        str(meta.get("attempted_through") or "0001-01-01"), end.isoformat()
    )


def _is_attempted(day: date, meta: dict[str, Any]) -> bool:
    for row in _normalize_ranges(meta.get("attempted_ranges") or []):
        if row["start"] <= day.isoformat() <= row["end"]:
            return True
    return False


def _missing_ranges(
    panel: pd.DataFrame, meta: dict[str, Any], start: date, end: date
) -> list[tuple[date, date]]:
    present = {pd.Timestamp(idx).date() for idx in getattr(panel, "index", [])}
    missing = [
        day for day in _date_range(start, end)
        if day not in present and not _is_attempted(day, meta)
    ]
    if not missing:
        return []
    ranges: list[tuple[date, date]] = []
    run_start = prev = missing[0]
    for day in missing[1:]:
        if day == prev + timedelta(days=1):
            prev = day
            continue
        ranges.append((run_start, prev))
        run_start = prev = day
    ranges.append((run_start, prev))
    return ranges


def update_and_slice_panel(
    analyzer: Any,
    symbols: Iterable[str],
    start_date: str,
    end_date: str,
) -> tuple[pl.DataFrame, dict[str, float], list[str]]:
    """Load per-symbol panels, fetch only missing deltas, and return a sliced panel."""
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    sliced: dict[str, pd.DataFrame] = {}
    dividends: dict[str, float] = {}
    fetched_symbols: list[str] = []

    for raw_symbol in symbols:
        symbol = str(raw_symbol).upper()
        with _symbol_file_lock(symbol):
            panel = _load_symbol_panel(symbol)
            prior_panel_min = (
                pd.Timestamp(panel.index.min()).date() if not panel.empty else None
            )
            meta = _load_meta(symbol)
            div_events = _load_dividend_events(symbol)
            changed = False
            discontinuity = False
            aggregate_hint = 0.0
            today_anchor = date.today()

            def _record_attempted(
                fstart: date, fend: date, got_dates: set[date]
            ) -> None:
                # Finalize past holes (weekends/holidays) so they aren't retried,
                # but never finalize today/future days whose bar we did not get:
                # an intraday call before the EOD bar exists must be able to
                # refetch today once the data lands.
                last_final = fend
                while (
                    last_final >= today_anchor
                    and last_final not in got_dates
                    and last_final >= fstart
                ):
                    last_final = last_final - timedelta(days=1)
                if last_final >= fstart:
                    _add_attempted_range(meta, fstart, last_final)

            def fetch_and_merge(fetch_start: date, fetch_end: date) -> None:
                nonlocal panel, changed, discontinuity, aggregate_hint
                if fetch_start > fetch_end:
                    return
                try:
                    fetched, aggregate = _fetch_symbol_range(
                        analyzer, symbol, fetch_start.isoformat(), fetch_end.isoformat()
                    )
                    aggregate_hint = max(aggregate_hint, aggregate)
                    if _looks_like_split(panel, fetched):
                        # Fresh bars sit on a different split/adjustment basis than
                        # the cached bars (any large day-over-day discontinuity).
                        discontinuity = True
                    got = {pd.Timestamp(i).date() for i in getattr(fetched, "index", [])}
                    panel = _merge_panel(panel, fetched)
                    _record_attempted(fetch_start, fetch_end, got)
                    changed = True
                except Exception as exc:
                    # Transient provider failure: do NOT record an attempted range,
                    # or the hole becomes a permanent no-refetch. A successful but
                    # empty response (above, got=∅) still finalizes past holes; only
                    # exceptions stay retryable on the next call.
                    logger.warning(
                        "OHLCV panel delta fetch failed for %s %s..%s: %s (retryable)",
                        symbol, fetch_start, fetch_end, exc,
                    )

            for fetch_start, fetch_end in _missing_ranges(panel, meta, start, end):
                fetch_and_merge(fetch_start, fetch_end)

            # Sync dividends at most once per symbol per window end. Fetch the FULL
            # dated history (start "1900-01-01") so a later WIDER same-end window
            # is not starved of older dividends by a prior narrow sync. The fresh
            # snapshot is authoritative and REPLACES the stored events (rather than
            # accumulating), so a corrected amount on an existing ex-date cannot
            # double-count, and a legitimate same-date special+regular pair is
            # still preserved by the (date, amount) identity in normalization.
            dividend_drift = False
            div_synced_through = str(meta.get("dividend_synced_through") or "")
            if end.isoformat() > div_synced_through:
                fresh_events, div_ok = _fetch_dividend_events(
                    symbol, "1900-01-01", end_date, aggregate_hint
                )
                # Only act on an authoritative response. A transient failure
                # (div_ok False) keeps the prior store and does NOT advance
                # dividend_synced_through, so it retries next call.
                if div_ok:
                    fresh_norm = _normalize_dividend_events(fresh_events)
                    # A dividend at ex-date D retroactively re-adjusts every bar
                    # BEFORE D, so any change (new/removed/corrected) to a dividend
                    # with ex-date > the earliest cached bar affects cached rows
                    # and requires a rebuild — including a brand-new dividend that
                    # lands AFTER the cached tail but still re-bases older bars.
                    if prior_panel_min is not None:
                        floor = prior_panel_min.isoformat()
                        prior_affecting = {
                            (e["date"], round(e["amount"], 12))
                            for e in div_events
                            if e["date"] > floor
                        }
                        fresh_affecting = {
                            (e["date"], round(e["amount"], 12))
                            for e in fresh_norm
                            if e["date"] > floor
                        }
                        if prior_affecting != fresh_affecting:
                            dividend_drift = True
                    # Replace with the authoritative snapshot even when empty (a
                    # removed dividend must clear the stale stored event).
                    if fresh_norm != div_events:
                        div_events = fresh_norm
                        changed = True
                    meta["dividend_synced_through"] = end.isoformat()
                    changed = True

            # Corporate-action rebuild trigger: a large price discontinuity OR a
            # dividend change inside the already-cached range (Massive re-adjusts
            # cached bars retroactively for either).
            rebuilt = False
            if discontinuity or dividend_drift:
                logger.info(
                    "OHLCV panel adjustment-basis change for %s; full window rebuild", symbol
                )
                panel = pd.DataFrame(columns=list(_OHLCV_FIELDS))
                meta["attempted_ranges"] = []
                meta["split_rebuilt_at"] = end.isoformat()
                rebuilt = True
                try:
                    fetched, aggregate = _fetch_symbol_range(
                        analyzer, symbol, start.isoformat(), end.isoformat()
                    )
                    aggregate_hint = max(aggregate_hint, aggregate)
                    got = {pd.Timestamp(i).date() for i in getattr(fetched, "index", [])}
                    panel = _merge_panel(panel, fetched)
                    _record_attempted(start, end, got)
                except Exception as exc:
                    logger.warning("OHLCV panel split rebuild failed for %s: %s", symbol, exc)
                changed = True

            if changed:
                if not rebuilt:
                    # Merge with the latest on-disk state while still locked so a
                    # concurrent writer's overlapping refresh is preserved.
                    disk_panel = _load_symbol_panel(symbol)
                    if not disk_panel.empty and disk_panel is not panel:
                        panel = _merge_panel(disk_panel, panel)
                elif panel is None or panel.empty:
                    # Rebuild produced nothing; remove the stale parquet so the old
                    # adjustment basis cannot linger on disk.
                    stale_panel_path, _stale_meta = _symbol_paths(symbol)
                    try:
                        stale_panel_path.unlink()
                    except OSError:
                        pass
                _save_symbol_panel(symbol, panel)
                _save_meta(symbol, meta)
                _save_dividend_events(symbol, div_events)

            if not panel.empty:
                mask = (panel.index >= pd.Timestamp(start)) & (panel.index <= pd.Timestamp(end))
                symbol_slice = panel.loc[mask].sort_index()
            else:
                symbol_slice = pd.DataFrame(columns=list(_OHLCV_FIELDS))
            window_dividend = _sum_dividends(div_events, start_date, end_date)

        if (
            symbol_slice.empty
            or "Close" not in symbol_slice.columns
            or symbol_slice["Close"].dropna().empty
        ):
            dividends[symbol] = window_dividend
            continue
        sliced[symbol] = symbol_slice
        fetched_symbols.append(symbol)
        dividends[symbol] = float(window_dividend)

    if not sliced:
        return pl.DataFrame(), dividends, fetched_symbols

    all_index = sorted(set().union(*(df.index for df in sliced.values())))
    price_pd = pd.DataFrame(index=pd.DatetimeIndex(all_index, name="Date"))
    if len(sliced) == 1:
        only = next(iter(sliced.values())).reindex(price_pd.index)
        for metric in _OHLCV_FIELDS:
            if metric in only.columns:
                price_pd[metric] = only[metric]
    else:
        for symbol, df in sliced.items():
            aligned = df.reindex(price_pd.index)
            for metric in _OHLCV_FIELDS:
                if metric in aligned.columns:
                    price_pd[f"{metric}_{symbol}"] = aligned[metric]
    return pl.from_pandas(price_pd.reset_index()), dividends, fetched_symbols


def prewarm_performance_windows(
    holdings_file: str | Path, *, today: date | None = None
) -> list[dict[str, Any]]:
    """Best-effort panel delta update and result-cache warm for standard periods."""
    from ic_engine.commands.performance_window import build_performance_window

    warmed: list[dict[str, Any]] = []
    for period in _COMMON_WINDOWS:
        try:
            envelope = build_performance_window(holdings_file, period=period, today=today)
            section = envelope.get("sections", {}).get("performance_window", {})
            warmed.append(
                {
                    "period": period,
                    "start_date": section.get("requested_start_date") or section.get("start_date"),
                    "end_date": section.get("requested_end_date") or section.get("end_date"),
                    "status": "success",
                }
            )
        except Exception as exc:
            logger.warning("performance-window prewarm failed for %s: %s", period, exc)
            warmed.append({"period": period, "status": "failed", "error": str(exc)})
    return warmed
