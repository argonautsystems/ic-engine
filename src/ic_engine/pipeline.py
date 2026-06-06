# Copyright 2026 InvestorClaw Contributors
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


"""
ic-engine unified pipeline orchestrator.

Single entry point for full portfolio analysis: load → normalize →
validate → analyze → export. Invoked by the router via the `run` /
`pipeline` aliases (router.COMMANDS) which resolve to `../pipeline.py`
relative to commands/ — i.e. this file at the engine package root.

Ported from InvestorClaw v2.2.x's top-level pipeline.py during Phase 2 of
IC_DECOMPOSITION (ic-engine v2.4.2). v2.4.6 fixes four codex P2s that
were preserved per scope across the v2.4.x cycle:

  1. Output directory now honors INVESTOR_CLAW_REPORTS_DIR /
     get_reports_dir() (with the fleet's dated-subdir convention)
     instead of hardcoding ~/portfolio_reports.
  2. holdings_summary.json + performance.json land at the reports_dir
     top level — where every downstream consumer (EOD report, FA
     discussion, dashboard) looks for them — instead of being buried
     under .raw/.
  3. The detailed CSV/Excel exports get the FULL CDM holdings (with
     equity/bond/cash/margin buckets) instead of the compact summary
     payload, which was missing the per-account detail the exporter
     iterates over.
  4. Uppercase .CSV inputs no longer self-overwrite. The legacy
     str.replace(".csv", ".json") only matched lowercase, so a file
     ending in .CSV produced an output path equal to the input,
     clobbering the source.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Optional, Union

from ic_engine.commands.analyze_performance_polars import PerformanceAnalyzer
from ic_engine.commands.export_report import ReportExporter
from ic_engine.config.path_resolver import get_reports_dir, secure_file_permissions
from ic_engine.config.schema import normalize_portfolio, validate_portfolio
from ic_engine.runtime.full_run import run_full as _runtime_run_full
from ic_engine.services.portfolio_utils import load_holdings_list

_CDM_SUMMARY_FIELD_MAP = {
    "totalPortfolioValue": "total_value",
    "equityValue": "equity_value",
    "equityPct": "equity_pct",
    "bondValue": "bond_value",
    "bondPct": "bond_pct",
    "cashValue": "cash_value",
    "cashPct": "cash_pct",
    "marginValue": "margin_value",
    "marginPct": "margin_pct",
    "cryptoValue": "crypto_value",
    "cryptoPct": "crypto_pct",
    "futuresValue": "futures_value",
    "futuresPct": "futures_pct",
    "metalsValue": "metals_value",
    "metalsPct": "metals_pct",
    "netValue": "net_value",
    "totalCostBasis": "total_cost_basis",
    "totalUnrealizedGainLoss": "unrealized_gl",
    "totalUnrealizedGainLossPct": "unrealized_gl_pct",
}

_LEGACY_SUMMARY_ALIASES = {
    "total_value": ("total_portfolio_value",),
    "net_value": ("net_worth",),
    "unrealized_gl": ("total_unrealized_gain_loss",),
    "unrealized_gl_pct": ("total_unrealized_gain_loss_pct",),
}

_CDM_SUMMARY_PERCENT_DERIVATIONS = (
    ("equity_pct", "equityPct", "equity_value"),
    ("bond_pct", "bondPct", "bond_value"),
    ("cash_pct", "cashPct", "cash_value"),
    ("margin_pct", "marginPct", "margin_value"),
    ("crypto_pct", "cryptoPct", "crypto_value"),
    ("futures_pct", "futuresPct", "futures_value"),
    ("metals_pct", "metalsPct", "metals_value"),
)
_CDM_SUMMARY_PERCENT_VALUE_KEYS = {
    pct_key: value_key for pct_key, _pct_camel_key, value_key in _CDM_SUMMARY_PERCENT_DERIVATIONS
}

_PIPELINE_SUMMARY_SOURCE_KEY = "_pipeline_source"
_COMPACT_SUMMARY_BUCKETS = (
    "equity",
    "bond",
    "cash",
    "margin",
    "crypto",
    "futures",
    "metals",
    "option",
)


def run_full(holdings_file: str, **kwargs):
    """v2.5 signed-envelope pipeline entry point, exposed from the legacy module."""
    return _runtime_run_full(holdings_file, **kwargs)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _paths_equal(candidate: object, expected: Path) -> bool:
    if not isinstance(candidate, str) or not candidate:
        return False
    try:
        return Path(candidate).expanduser().resolve() == expected.expanduser().resolve()
    except OSError:
        return False


def _summary_matches_input_identity(
    summary_payload: object, analyzer_input: Path, input_sha256: str
) -> Optional[bool]:
    if not isinstance(summary_payload, dict):
        return False

    source = summary_payload.get(_PIPELINE_SUMMARY_SOURCE_KEY)
    if isinstance(source, dict) and source.get("sha256"):
        return source.get("sha256") == input_sha256

    output_file = summary_payload.get("output_file")
    if output_file:
        return None if _paths_equal(output_file, analyzer_input) else False

    if summary_payload.get("_pipeline_compat_note"):
        return False

    return None


def _should_preserve_holdings_summary(
    summary_path: Path, analyzer_input: Path, input_sha256: str
) -> bool:
    if not summary_path.exists():
        return False

    try:
        with open(summary_path, "r") as f:
            summary_payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False

    identity_match = _summary_matches_input_identity(
        summary_payload,
        analyzer_input,
        input_sha256,
    )
    if identity_match is not None:
        return identity_match

    try:
        return summary_path.stat().st_mtime >= analyzer_input.stat().st_mtime
    except OSError:
        return False


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _translate_cdm_summary(cdm_summary: dict) -> dict:
    if not isinstance(cdm_summary, dict):
        cdm_summary = {}

    translated = {}
    for camel_key, snake_key in _CDM_SUMMARY_FIELD_MAP.items():
        for summary_key in (
            snake_key,
            *_LEGACY_SUMMARY_ALIASES.get(snake_key, ()),
            camel_key,
        ):
            value = cdm_summary.get(summary_key)
            if value is not None:
                translated[snake_key] = value
                break
        else:
            if snake_key == "net_value":
                translated[snake_key] = translated.get("total_value", 0.0)
            else:
                translated[snake_key] = 0.0

    total_value = _safe_float(translated.get("total_value"))
    if total_value > 0:
        for pct_key, pct_camel_key, value_key in _CDM_SUMMARY_PERCENT_DERIVATIONS:
            if cdm_summary.get(pct_key) is None and cdm_summary.get(pct_camel_key) is None:
                translated[pct_key] = _safe_float(translated.get(value_key)) / total_value * 100

    return translated


def _cdm_summary_source_keys(snake_key: str) -> tuple[str, ...]:
    camel_keys = tuple(
        camel_key
        for camel_key, mapped_snake_key in _CDM_SUMMARY_FIELD_MAP.items()
        if mapped_snake_key == snake_key
    )
    return (snake_key, *_LEGACY_SUMMARY_ALIASES.get(snake_key, ()), *camel_keys)


def _cdm_summary_has_present_value(cdm_summary: dict, snake_key: str) -> bool:
    if not isinstance(cdm_summary, dict):
        return False
    return any(
        summary_key in cdm_summary and cdm_summary[summary_key] is not None
        for summary_key in _cdm_summary_source_keys(snake_key)
    )


def _cdm_summary_float(cdm_summary: dict, snake_key: str) -> Optional[float]:
    if not isinstance(cdm_summary, dict):
        return None

    for summary_key in _cdm_summary_source_keys(snake_key):
        if summary_key not in cdm_summary or cdm_summary[summary_key] is None:
            continue
        try:
            return float(cdm_summary[summary_key])
        except (TypeError, ValueError):
            continue

    return None


def _translated_summary_value_is_provider_backed(cdm_summary: dict, snake_key: str) -> bool:
    if _cdm_summary_has_present_value(cdm_summary, snake_key):
        return True

    value_key = _CDM_SUMMARY_PERCENT_VALUE_KEYS.get(snake_key)
    return (
        value_key is not None
        and _cdm_summary_has_present_value(cdm_summary, "total_value")
        and _cdm_summary_has_present_value(cdm_summary, value_key)
    )


def _merge_translated_summary_over_buckets(bucket_summary: dict, cdm_summary: dict) -> dict:
    merged = dict(bucket_summary)
    translated_summary = _translate_cdm_summary(cdm_summary)

    for key, value in translated_summary.items():
        if key not in merged or _translated_summary_value_is_provider_backed(cdm_summary, key):
            merged[key] = value

    return merged


def _derive_position_count(portfolio: dict) -> dict:
    if not isinstance(portfolio, dict):
        return {}

    counts = {}
    for key in ("equity", "bond", "cash", "crypto", "futures", "metals"):
        bucket = portfolio.get(key)
        if isinstance(bucket, dict):
            counts[key] = len(bucket)

    # Mirror _build_compact_holdings: the "option" count is emitted ONLY
    # when option positions exist, so legacy option-free summaries keep
    # their exact pre-options shape.
    option_bucket = portfolio.get("option")
    if isinstance(option_bucket, dict) and option_bucket:
        counts["option"] = len(option_bucket)

    return counts


def _bucket_position_value(position: dict) -> float:
    for value_key in ("value", "market_value", "marketValue"):
        if value_key in position and position[value_key] is not None:
            return _safe_float(position[value_key])

    shares = _safe_float(position.get("shares") or position.get("quantity"))
    price = _safe_float(
        position.get("current_price") or position.get("currentPrice") or position.get("price")
    )
    return shares * price if shares and price else 0.0


def _bucket_to_holding_objects(bucket: Any, asset_type: str) -> dict:
    if not isinstance(bucket, dict):
        return {}

    from ic_engine.models.holdings import Holding

    holdings = {}
    for symbol, position in bucket.items():
        if not isinstance(position, dict):
            continue

        value = _bucket_position_value(position)
        shares = _safe_float(position.get("shares") or position.get("quantity"))
        current_price = _safe_float(
            position.get("current_price") or position.get("currentPrice") or position.get("price")
        )
        if not shares:
            shares = 1.0
        if not current_price:
            current_price = value

        cost_basis = _safe_float(position.get("cost_basis") or position.get("costBasis"))
        purchase_price = _safe_float(
            position.get("purchase_price")
            or position.get("cost_basis_price")
            or position.get("costBasisPrice")
        )
        if not purchase_price:
            purchase_price = cost_basis / shares if cost_basis and shares else current_price

        holding_asset_type = str(
            position.get("asset_type") or position.get("assetType") or asset_type
        )
        security_type = position.get("security_type") or position.get("securityType")

        holdings[str(symbol)] = Holding(
            symbol=str(position.get("symbol") or symbol),
            asset_type=holding_asset_type,
            shares=shares,
            current_price=current_price,
            purchase_price=purchase_price,
            market_value=value,
            sector=position.get("sector") or "Unknown",
            security_type=security_type,
            is_etf=(security_type == "etf" or holding_asset_type == "etf"),
            account=position.get("account") or position.get("accountId"),
            account_type=position.get("account_type") or position.get("accountType"),
            data_provider=position.get("data_provider") or position.get("dataProvider"),
            espp_status=position.get("espp_status") or position.get("esppStatus"),
            managed_status=position.get("managed_status") or position.get("managedStatus"),
            cusip=position.get("cusip"),
            coupon_rate=_safe_float(position.get("coupon_rate") or position.get("coupon")),
            maturity_date=position.get("maturity_date") or position.get("maturityDate"),
            bond_name=position.get("bond_name") or position.get("security_name"),
            contract_symbol=position.get("contract_symbol") or position.get("contractSymbol"),
            expiry_date=position.get("expiry_date") or position.get("expiryDate"),
            notional_value=_safe_float(
                position.get("notional_value") or position.get("notionalValue")
            ),
            blockchain=position.get("blockchain"),
            metal_type=position.get("metal_type") or position.get("metalType"),
            troy_oz=_safe_float(position.get("troy_oz") or position.get("troyOz")),
        )

    return holdings


def _build_compact_summary_from_buckets(
    portfolio: dict, total_summary: dict, output_file: str
) -> Optional[dict]:
    if not isinstance(portfolio, dict):
        return None
    if not any(
        isinstance(portfolio.get(bucket_name), dict) and portfolio[bucket_name]
        for bucket_name in _COMPACT_SUMMARY_BUCKETS
    ):
        return None

    try:
        from ic_engine.commands.fetch_holdings import _build_compact_holdings
    except Exception:
        return None

    bucket_data = {
        bucket_name: _bucket_to_holding_objects(portfolio.get(bucket_name), bucket_name)
        for bucket_name in _COMPACT_SUMMARY_BUCKETS
    }
    if not any(bucket_data.values()):
        return None

    bucket_total_value = sum(
        holding.value for holdings in bucket_data.values() for holding in holdings.values()
    )
    total_value = _cdm_summary_float(total_summary, "total_value")
    if total_value is None:
        total_value = bucket_total_value

    return _build_compact_holdings(
        bucket_data["equity"],
        bucket_data["bond"],
        bucket_data["cash"],
        bucket_data["margin"],
        total_value,
        total_summary,
        output_file,
        crypto_data=bucket_data["crypto"],
        futures_data=bucket_data["futures"],
        metals_data=bucket_data["metals"],
        # Empty dict when the portfolio has no options, which preserves
        # _build_compact_holdings' only-emit-option-keys-when-present
        # behavior (option_value/option_pct/position_count.option).
        options_data=bucket_data["option"],
    )


def run_pipeline(
    holdings_file: str,
    output_dir: Optional[Union[str, Path]] = None,
):
    """Run full pipeline: load → normalize → validate → analyze → export.

    Args:
        holdings_file: path to a holdings file (.json with CDM-shape, or
            .csv/.CSV which is auto-converted via PortfolioFetcher).
        output_dir: where to write reports. If None, falls back to
            ic_engine.config.path_resolver.get_reports_dir() which honors
            INVESTOR_CLAW_REPORTS_DIR + INVESTOR_CLAW_DATED_REPORTS env
            vars and is the canonical fleet-wide reports root.

    Returns dict with keys:
        normalized_holdings — full CDM snapshot path (under .raw/)
        holdings_summary    — compact summary path (reports_dir top-level,
                              consumed by EOD/FA/dashboard)
        performance         — performance metrics path (top-level)
        reports_dir         — the resolved reports directory
    """

    holdings_path = Path(holdings_file).expanduser()
    if not holdings_path.exists():
        raise FileNotFoundError(f"Holdings file not found: {holdings_file}")

    # Output paths. get_reports_dir() handles INVESTOR_CLAW_REPORTS_DIR
    # env override + the dated-subdir convention + 0700 perms. If the
    # caller passed an explicit override, honor that instead.
    if output_dir is None:
        output_dir = get_reports_dir()
    else:
        output_dir = Path(output_dir).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)

    raw_dir = output_dir / ".raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Auto-detect CSV input and convert to JSON via the canonical
    # PortfolioFetcher.main(input, output). Writing into .raw/ makes
    # PortfolioFetcher publish its canonical holdings_summary.json at the
    # reports_dir top level because it promotes a .raw parent by design.
    if holdings_path.suffix.lower() == ".csv":
        from ic_engine.commands.fetch_holdings import PortfolioFetcher

        holdings_path_json = raw_dir / "holdings.json"
        if holdings_path_json.resolve() == holdings_path.resolve():
            # Defensive: never let conversion overwrite the source, even if
            # a caller points output_dir/.raw at the CSV itself through an
            # unusual path setup.
            holdings_path_json = raw_dir / f"{holdings_path.stem}.pipeline.json"
        fetcher = PortfolioFetcher()
        fetcher.main(str(holdings_path), str(holdings_path_json))
        with open(holdings_path_json, "r") as f:
            raw = json.load(f)
        # Performance analyzer expects the JSON path; carry it forward.
        analyzer_input = holdings_path_json
    else:
        with open(holdings_path, "r") as f:
            raw = json.load(f)
        analyzer_input = holdings_path

    # Normalize + validate
    data = normalize_portfolio(raw)
    validate_portfolio(data)

    # Load holdings list
    load_holdings_list(data)

    # Verbose CDM snapshot lives under .raw/ (intermediate; rarely read
    # except for debugging and audit).
    holdings_cdm_out = raw_dir / "holdings.json"

    # Compact summary + performance live at the reports_dir TOP LEVEL,
    # which is where EOD report, FA discussion, dashboard, and other
    # downstream consumers look for them. Pre-v2.4.6 these were buried
    # under .raw/ alongside the CDM snapshot, breaking the consumer
    # discovery path.
    holdings_summary_out = output_dir / "holdings_summary.json"
    performance_out = output_dir / "performance.json"

    # Save CDM normalized holdings snapshot (full detail, audit/trace use).
    # In the router's normal no-arg flow, the input is already
    # `<reports_dir>/.raw/holdings.json`. Rewriting that same file with
    # normalize_portfolio(data) would replace the raw CDM positions array with
    # the compact canonical projection, breaking lookup.query_holdings_symbol.
    if holdings_cdm_out.resolve() == analyzer_input.resolve():
        holdings_cdm_out = analyzer_input
    else:
        with open(holdings_cdm_out, "w") as f:
            json.dump(data, f, indent=2)

    # Compact summary for EOD/FA/dashboard consumers. The canonical
    # snake-case shape (with `top_equity`, `sector_weights`,
    # `sector_weights_ex_espp`, `accounts`, `summary.total_value`,
    # `summary.equity_pct`, etc.) is built by
    # `_build_compact_holdings()` in commands/fetch_holdings.py. The
    # shapes EOD/FA read are not extractable from the CDM verbatim
    # (the CDM uses camelCase like `totalPortfolioValue`/`equityPct`
    # and lacks `top_equity`/`sector_weights` derived columns), so we
    # MUST NOT overwrite an existing compact summary with a lossy
    # CDM-derived stand-in. If `<reports_dir>/holdings_summary.json`
    # already exists (typical case: ic-holdings ran first and wrote
    # the canonical compact shape), leave it alone. If it's missing
    # (caller invoked `pipeline run` on a raw CDM file from outside
    # the fetch flow), write a best-effort compact derivation as a
    # fallback so EOD/FA at least see the totals they expect. Pipeline-
    # generated fallbacks carry an input hash so a later run with a
    # different holdings file regenerates the summary even if the old
    # summary has a newer mtime.
    analyzer_input_sha256 = _file_sha256(analyzer_input)
    preserve_holdings_summary = _should_preserve_holdings_summary(
        holdings_summary_out,
        analyzer_input,
        analyzer_input_sha256,
    )
    if not preserve_holdings_summary:
        portfolio = data.get("portfolio", {}) if isinstance(data.get("portfolio"), dict) else {}
        cdm_summary = data.get("summary") or portfolio.get("summary") or {}
        if not isinstance(cdm_summary, dict):
            cdm_summary = {}
        compact_summary = _build_compact_summary_from_buckets(
            portfolio,
            cdm_summary,
            str(holdings_cdm_out),
        )
        if compact_summary is None:
            # Translate only the CDM summary fields consumers read. Prefer
            # existing snake_case if both spellings are present, and do not pass
            # through unmapped CDM camelCase keys.
            compact_summary = {
                "summary": _translate_cdm_summary(cdm_summary),
                "top_equity": data.get("top_equity") or portfolio.get("top_equity", []),
                "sector_weights": (
                    data.get("sector_weights") or portfolio.get("sector_weights", {})
                ),
                "sector_breakdown": (
                    data.get("sector_breakdown") or portfolio.get("sector_breakdown", {})
                ),
                "accounts": data.get("accounts") or portfolio.get("accounts", {}),
            }
        if cdm_summary:
            # Summary totals from a provider are authoritative, but top
            # holdings and sector weights still need the compact bucket
            # derivation above.
            translated_summary = _merge_translated_summary_over_buckets(
                compact_summary.get("summary", {}),
                cdm_summary,
            )
            position_count = _derive_position_count(portfolio)
            if position_count:
                translated_summary["position_count"] = position_count
            compact_summary["summary"] = translated_summary
        compact_summary[_PIPELINE_SUMMARY_SOURCE_KEY] = {
            "sha256": analyzer_input_sha256,
            "path": str(analyzer_input.expanduser().resolve()),
        }
        compact_summary["_pipeline_compat_note"] = (
            "Best-effort compact summary derived from CDM by ic_engine.pipeline. "
            "For full snake-case fidelity (incl. sector_weights_ex_espp, top_crypto, "
            "etc.) run `ic-holdings` first to produce the canonical compact shape."
        )
        with open(holdings_summary_out, "w") as f:
            json.dump(compact_summary, f, indent=2)
    # else: a canonical holdings_summary.json already exists (likely from
    # ic-holdings); preserve it untouched.

    # Run performance analysis on the JSON-shaped holdings (CSV inputs are
    # converted to JSON above so analyzer always receives a JSON path).
    # Write to TOP LEVEL (where EOD/FA/dashboard look) AND mirror under
    # .raw/ (where ic_engine.commands.lookup.query_performance_top still
    # reads from via _load_raw). Cheap dual-write keeps both consumer
    # discovery paths working without forcing a lookup.py change.
    analyzer = PerformanceAnalyzer()
    analyzer.analyze_portfolio(str(analyzer_input), str(performance_out))
    raw_performance_out = raw_dir / "performance.json"
    try:
        raw_performance_out.write_text(
            performance_out.read_text(encoding="utf-8"), encoding="utf-8"
        )
        secure_file_permissions(raw_performance_out)
    except OSError:
        # Non-fatal: top-level performance.json is the canonical artifact;
        # the .raw/ mirror is just a back-compat shim for lookup.
        pass

    # Export reports — feed the FULL CDM holdings file (which has
    # portfolio.equity/bond/cash/margin buckets), not the compact summary.
    # ReportExporter.load_data re-runs normalize_portfolio internally and
    # iterates the per-account buckets; the compact-summary shape was
    # silently dropping equity/bond/cash/margin detail and producing
    # empty per-account CSVs.
    exporter = ReportExporter()
    exporter.load_data(str(holdings_cdm_out), str(performance_out))
    exporter.export_to_csv(str(output_dir / "portfolio_report"))

    return {
        "normalized_holdings": str(holdings_cdm_out),
        "holdings_summary": str(holdings_summary_out),
        "performance": str(performance_out),
        "reports_dir": str(output_dir),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m ic_engine.pipeline <holdings.json>")
        exit(1)

    result = run_pipeline(sys.argv[1])

    print("\nPipeline complete:")
    for k, v in result.items():
        print(f"  {k}: {v}")
