# SPDX-License-Identifier: Apache-2.0
"""Pre-built portfolio templates for first-time users.

Drops a starter CSV into ``/data/portfolios/`` so users can experiment with
the dashboard without first uploading a real broker statement. Five
canonical allocations: a single-fund S&P 500 indexer, the Boglehead
three-fund mix, a classic 60/40, the Ray-Dalio All-Weather, and a
conservative income tilt.

Share counts are sized to a ~$100K starter portfolio using late-2025 /
early-2026 representative prices. The engine refreshes prices on first
regenerate, so the exact starter price doesn't matter — these values
exist to make the template render cleanly until Massive / yfinance fills
in real quotes.

Templates are NOT investment advice. They are well-known canonical
allocations cited in the Boglehead / Ray-Dalio / classic-60-40
literature, surfaced as starter configurations only. The dashboard
disclaimer continues to apply.
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path
from typing import Any


# Canonical CSV header — matches what ic-engine's auto_setup.py writes.
_CSV_FIELDS = ["symbol", "shares", "price", "asset_type"]

# Slug regex — alphanumeric + hyphen, prevents path traversal.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


PORTFOLIO_TEMPLATES: list[dict[str, Any]] = [
    {
        "slug": "spx-indexer",
        "name": "S&P 500 Indexer",
        "description": "Single-fund US large-cap exposure. Set-and-forget.",
        "rationale": (
            "The simplest possible portfolio: one S&P 500 ETF. Roughly "
            "matches the S&P 500 index return minus a tiny expense ratio."
        ),
        "rows": [
            {"symbol": "VOO", "shares": 196, "price": 510.00, "asset_type": "equity"},
        ],
    },
    {
        "slug": "boglehead-3fund",
        "name": "Boglehead Three-Fund",
        "description": "US total stock + international + US total bond.",
        "rationale": (
            "Classic Boglehead allocation: low-cost broad-market index "
            "funds across three asset classes. Targets ~60% US equity, "
            "~22% international equity, ~18% US bonds."
        ),
        "rows": [
            {"symbol": "VTI",  "shares": 200, "price": 290.00, "asset_type": "equity"},
            {"symbol": "VXUS", "shares": 350, "price":  65.00, "asset_type": "equity"},
            {"symbol": "BND",  "shares": 270, "price":  73.00, "asset_type": "bond"},
        ],
    },
    {
        "slug": "sixty-forty",
        "name": "60/40 Stock-Bond",
        "description": "Classic balanced allocation.",
        "rationale": (
            "60% broad US equity, 40% US bonds. The textbook moderate-"
            "risk allocation used in pension and endowment portfolios "
            "for decades."
        ),
        "rows": [
            {"symbol": "VTI", "shares": 207, "price": 290.00, "asset_type": "equity"},
            {"symbol": "BND", "shares": 547, "price":  73.00, "asset_type": "bond"},
        ],
    },
    {
        "slug": "all-weather",
        "name": "All-Weather (Dalio)",
        "description": "Stocks, long bonds, intermediate bonds, gold, commodities.",
        "rationale": (
            "Ray Dalio's All-Weather allocation: 30% stocks, 40% long "
            "Treasuries, 15% intermediate Treasuries, 7.5% gold, 7.5% "
            "commodities. Designed to perform across inflation / "
            "deflation / growth / recession regimes."
        ),
        "rows": [
            {"symbol": "VTI", "shares": 103, "price": 290.00, "asset_type": "equity"},
            {"symbol": "TLT", "shares": 444, "price":  90.00, "asset_type": "bond"},
            {"symbol": "IEI", "shares": 130, "price": 115.00, "asset_type": "bond"},
            {"symbol": "GLD", "shares":  32, "price": 235.00, "asset_type": "equity"},
            {"symbol": "DBC", "shares": 313, "price":  24.00, "asset_type": "equity"},
        ],
    },
    {
        "slug": "conservative-income",
        "name": "Conservative Income",
        "description": "Bonds, dividend equity, cash.",
        "rationale": (
            "50% US bonds, 30% high-dividend equity, 20% cash. Tilts "
            "toward income generation and capital preservation over "
            "growth — appropriate for shorter time horizons."
        ),
        "rows": [
            {"symbol": "BND",  "shares":   685, "price":  73.00, "asset_type": "bond"},
            {"symbol": "VYM",  "shares":   230, "price": 130.00, "asset_type": "equity"},
            {"symbol": "CASH", "shares": 20000, "price":   1.00, "asset_type": "cash"},
        ],
    },
]


def list_templates() -> list[dict[str, Any]]:
    """Return template metadata for UI rendering (no row detail).

    Adds a ``positions`` summary string for compact display: "VTI 200 +
    VXUS 350 + BND 270" etc.
    """
    out = []
    for t in PORTFOLIO_TEMPLATES:
        positions = " + ".join(
            f"{r['symbol']} {r['shares']}" for r in t["rows"]
        )
        notional = sum(r["shares"] * r["price"] for r in t["rows"])
        out.append({
            "slug": t["slug"],
            "name": t["name"],
            "description": t["description"],
            "rationale": t["rationale"],
            "positions": positions,
            "notional": round(notional, 2),
            "row_count": len(t["rows"]),
        })
    return out


def _find_template(slug: str) -> dict[str, Any] | None:
    if not _SLUG_RE.match(slug or ""):
        return None
    for t in PORTFOLIO_TEMPLATES:
        if t["slug"] == slug:
            return t
    return None


def apply_template(
    slug: str, portfolio_dir: Path | None = None
) -> dict[str, Any]:
    """Write a template's CSV into the portfolio dir.

    Args:
        slug: template slug. Validated against the registry; an unknown
            or malformed slug returns ``{"error": "unknown_template", ...}``.
        portfolio_dir: target directory. Defaults to ``IC_PORTFOLIO_DIR``
            env (``/data/portfolios`` in-container).

    Returns:
        On success: ``{"applied": True, "filename", "path", "rows", "name"}``
        On error:   ``{"error": "...", "detail": "..."}``
    """
    template = _find_template(slug)
    if template is None:
        return {
            "error": "unknown_template",
            "detail": f"No template registered for slug {slug!r}.",
            "available": [t["slug"] for t in PORTFOLIO_TEMPLATES],
        }

    pdir = portfolio_dir or Path(
        os.environ.get("IC_PORTFOLIO_DIR", "/data/portfolios")
    )
    try:
        pdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": "portfolio_dir_unwritable", "detail": str(e)}

    filename = f"template-{template['slug']}.csv"
    dest = pdir / filename
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            for row in template["rows"]:
                writer.writerow({k: row[k] for k in _CSV_FIELDS})
        try:
            tmp.chmod(0o644)
        except OSError:
            pass
        os.replace(tmp, dest)
    except Exception as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"error": "csv_write_failed", "detail": str(e)}

    return {
        "applied": True,
        "name": template["name"],
        "filename": filename,
        "path": str(dest),
        "rows": len(template["rows"]),
    }
