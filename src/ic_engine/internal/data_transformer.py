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
DataTransformer — Phase 2 Stage P1c
===================================

Config-driven schema-standardization stage for InvestorClaw.

Adapts the ETLANTIS GenericT0Transform pattern to financial data. Reads a
JSON rules file (``config/data_transform_rules.json``) describing, per
source:

* ``field_mappings``: raw-column → canonical-column renames.
* ``type_coercions``: canonical-column → polars dtype.
* ``derived_fields``: name → polars expression string for computed columns.
* ``drop_columns`` / ``add_columns`` / ``null_values``: standard cleanup.

Output is a dict of Polars DataFrames keyed by source, all conforming to a
CDM-compatible canonical schema (``symbol, date, open, high, low, close,
adj_close, volume, source``).

Separation of concerns
----------------------
This stage doesn't download, parse provider-specific formats, or run
integrity checks — those are the downloader's and extractor's jobs. It
only normalizes schemas.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transform configuration
# ---------------------------------------------------------------------------


@dataclass
class TransformConfig:
    """Per-source transformation rules."""

    source: str
    field_mappings: Dict[str, str] = field(default_factory=dict)
    type_coercions: Dict[str, str] = field(default_factory=dict)
    derived_fields: Dict[str, str] = field(default_factory=dict)
    drop_columns: List[str] = field(default_factory=list)
    add_columns: Dict[str, Any] = field(default_factory=dict)
    null_values: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TransformConfig":
        return cls(
            source=d["source"],
            field_mappings=dict(d.get("field_mappings") or {}),
            type_coercions=dict(d.get("type_coercions") or {}),
            derived_fields=dict(d.get("derived_fields") or {}),
            drop_columns=list(d.get("drop_columns") or []),
            add_columns=dict(d.get("add_columns") or {}),
            null_values=list(d.get("null_values") or []),
        )


# ---------------------------------------------------------------------------
# DataTransformer
# ---------------------------------------------------------------------------


class DataTransformer:
    """Apply config-driven field mappings, type coercions, and derived columns.

    Usage::

        rules = Path("config/data_transform_rules.json")
        transformer = DataTransformer.from_rules_file(rules)
        standardized = transformer.transform(
            extracted_data={"AAPL": aapl_df, "MSFT": msft_df},
            source="yfinance_ohlcv",
        )
    """

    # polars dtype aliases (mirrors ETLANTIS GenericT0Transform DTYPE_MAP).
    _DTYPE_ALIASES = {
        "str": "Utf8",
        "utf8": "Utf8",
        "string": "Utf8",
        "int": "Int64",
        "int32": "Int32",
        "int64": "Int64",
        "float": "Float64",
        "float32": "Float32",
        "float64": "Float64",
        "bool": "Boolean",
        "boolean": "Boolean",
        "date": "Date",
        "datetime": "Datetime",
    }

    def __init__(self, rules: Optional[Dict[str, TransformConfig]] = None):
        self.rules: Dict[str, TransformConfig] = rules or {}

    # ----- Constructors -----------------------------------------------------

    @classmethod
    def from_rules_file(cls, path: Path) -> "DataTransformer":
        path = Path(path)
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
        rules: Dict[str, TransformConfig] = {}
        for entry in config.get("transform_rules") or []:
            tc = TransformConfig.from_dict(entry)
            rules[tc.source] = tc
        logger.info("[DataTransformer] Loaded %d source rule(s) from %s", len(rules), path.name)
        return cls(rules=rules)

    # ----- Public API -------------------------------------------------------

    def transform(
        self,
        extracted_data: Dict[str, Any],
        source: str,
    ) -> Dict[str, Any]:
        """Apply transform rules for ``source`` to every DataFrame in
        ``extracted_data``.

        Args:
            extracted_data: Symbol → Polars DataFrame from DataExtractor.
            source:         Source identifier matching a rule in the config.

        Returns:
            Symbol → standardized Polars DataFrame. Sources without rules
            pass through unchanged (with a warning).
        """
        try:
            import polars as pl  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "DataTransformer requires polars. Install with: pip install polars"
            ) from e

        rule = self.rules.get(source)
        if rule is None:
            logger.warning(
                "[DataTransformer] No rule for source=%r; passing through",
                source,
            )
            return dict(extracted_data)

        out: Dict[str, Any] = {}
        for symbol, df in extracted_data.items():
            if df is None or df.is_empty():
                out[symbol] = df
                continue
            try:
                out[symbol] = self._apply_rule(df, rule)
            except Exception as e:
                logger.error(
                    "[DataTransformer] %s/%s: transform failed: %s",
                    source,
                    symbol,
                    e,
                )
                out[symbol] = df  # preserve original on failure

        logger.info(
            "[DataTransformer] source=%s: %d symbols standardized",
            source,
            len(out),
        )
        return out

    # ----- Internal ---------------------------------------------------------

    def _apply_rule(self, df, rule: TransformConfig):
        """Apply a single TransformConfig in the canonical ETLANTIS order."""
        import polars as pl

        # 1. Drop unwanted raw columns first
        if rule.drop_columns:
            existing = [c for c in rule.drop_columns if c in df.columns]
            if existing:
                df = df.drop(existing)

        # 2. Null normalization on string columns (before rename)
        if rule.null_values:
            str_cols = [c for c, t in zip(df.columns, df.dtypes) if t == pl.Utf8]
            if str_cols:
                replace_exprs = []
                for c in str_cols:
                    expr = pl.col(c)
                    for nv in rule.null_values:
                        expr = expr.replace(nv, None)
                    replace_exprs.append(expr)
                df = df.with_columns(replace_exprs)

        # 3. Column renames (raw → canonical)
        if rule.field_mappings:
            valid = {k: v for k, v in rule.field_mappings.items() if k in df.columns}
            if valid:
                df = df.rename(valid)

        # 4. Type coercions
        if rule.type_coercions:
            cast_exprs = []
            for col, type_str in rule.type_coercions.items():
                if col not in df.columns:
                    continue
                alias = self._DTYPE_ALIASES.get(type_str.lower(), type_str)
                dtype = getattr(pl, alias, None)
                if dtype is None:
                    logger.warning(
                        "[DataTransformer] Unknown dtype %r for column %r",
                        type_str,
                        col,
                    )
                    continue
                if dtype in (pl.Date, pl.Datetime):
                    # Dates may be epoch-seconds (int) or ISO strings; try string
                    # parse first, fall back to permissive cast.
                    col_dtype = df.schema.get(col)
                    if col_dtype == pl.Utf8:
                        cast_exprs.append(pl.col(col).str.to_date(strict=False).alias(col))
                    else:
                        cast_exprs.append(pl.col(col).cast(dtype, strict=False).alias(col))
                else:
                    cast_exprs.append(pl.col(col).cast(dtype, strict=False).alias(col))
            if cast_exprs:
                df = df.with_columns(cast_exprs)

        # 5. Derived columns (simple safe expressions)
        if rule.derived_fields:
            df = self._apply_derived(df, rule.derived_fields)

        # 6. Literal add_columns (e.g. {"source": "yfinance"})
        if rule.add_columns:
            lits = [pl.lit(v).alias(k) for k, v in rule.add_columns.items()]
            df = df.with_columns(lits)

        return df

    @staticmethod
    def _apply_derived(df, derived: Dict[str, str]):
        """Apply derived-column expressions from the config.

        For safety, only a small whitelist of expression shapes is supported:
          * ``copy:<existing_col>``            — alias an existing column
          * ``from_epoch:<existing_col>``      — Unix seconds → Date
          * ``divide:<col>:<divisor>``         — col / literal_number
          * ``multiply:<col>:<factor>``        — col * literal_number
          * ``weight_pct:<col>``               — col as percentage (col * 100)

        Anything else is logged and skipped. This avoids exposing an
        ``eval`` surface for config-driven expressions while still covering
        the common financial-data normalization cases (epoch→date,
        split-adjusted scaling, percent conversions).
        """
        import polars as pl

        out_exprs = []
        for name, spec in derived.items():
            if not isinstance(spec, str) or ":" not in spec:
                logger.warning(
                    "[DataTransformer] Derived %r: unrecognized spec %r",
                    name,
                    spec,
                )
                continue
            op, _, args = spec.partition(":")
            op = op.strip().lower()

            if op == "copy":
                col = args.strip()
                if col in df.columns:
                    out_exprs.append(pl.col(col).alias(name))

            elif op == "from_epoch":
                col = args.strip()
                if col in df.columns:
                    out_exprs.append(
                        pl.from_epoch(pl.col(col), time_unit="s")
                        .cast(pl.Date, strict=False)
                        .alias(name)
                    )

            elif op in ("divide", "multiply"):
                parts = args.split(":")
                if len(parts) != 2:
                    logger.warning(
                        "[DataTransformer] Derived %r: bad args for %s: %r",
                        name,
                        op,
                        args,
                    )
                    continue
                col, factor_str = parts[0].strip(), parts[1].strip()
                try:
                    factor = float(factor_str)
                except ValueError:
                    logger.warning(
                        "[DataTransformer] Derived %r: non-numeric factor %r",
                        name,
                        factor_str,
                    )
                    continue
                if col in df.columns:
                    base = pl.col(col).cast(pl.Float64, strict=False)
                    expr = base / factor if op == "divide" else base * factor
                    out_exprs.append(expr.alias(name))

            elif op == "weight_pct":
                col = args.strip()
                if col in df.columns:
                    out_exprs.append(
                        (pl.col(col).cast(pl.Float64, strict=False) * 100.0).alias(name)
                    )

            else:
                logger.warning(
                    "[DataTransformer] Derived %r: unknown op %r",
                    name,
                    op,
                )

        if out_exprs:
            df = df.with_columns(out_exprs)
        return df


__all__ = [
    "DataTransformer",
    "TransformConfig",
]
