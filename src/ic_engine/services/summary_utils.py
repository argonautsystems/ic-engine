# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Lightweight helpers for compact/CDM portfolio summary compatibility."""

from __future__ import annotations

from typing import Any, Dict

SUMMARY_FIELD_ALIASES = {
    "total_value": ("total_value", "total_portfolio_value", "totalPortfolioValue"),
    "net_value": ("net_value", "net_worth", "netValue"),
    "equity_value": ("equity_value", "equityValue"),
    "bond_value": ("bond_value", "bondValue"),
    "cash_value": ("cash_value", "cashValue"),
    "margin_value": ("margin_value", "marginValue"),
    "crypto_value": ("crypto_value", "cryptoValue"),
    "futures_value": ("futures_value", "futuresValue"),
    "metals_value": ("metals_value", "metalsValue"),
    "equity_pct": ("equity_pct", "equityPct"),
    "bond_pct": ("bond_pct", "bondPct"),
    "cash_pct": ("cash_pct", "cashPct"),
    "margin_pct": ("margin_pct", "marginPct"),
    "crypto_pct": ("crypto_pct", "cryptoPct"),
    "futures_pct": ("futures_pct", "futuresPct"),
    "metals_pct": ("metals_pct", "metalsPct"),
    "unrealized_gl": (
        "unrealized_gl",
        "total_unrealized_gain_loss",
        "totalUnrealizedGainLoss",
    ),
    "unrealized_gl_pct": (
        "unrealized_gl_pct",
        "total_unrealized_gain_loss_pct",
        "totalUnrealizedGainLossPct",
        "unrealizedPct",
    ),
    "total_cost_basis": ("total_cost_basis", "totalCostBasis"),
    "position_count": ("position_count", "asset_count", "assetCount"),
}


def _first_present(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def extract_summary_block(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the summary block from compact, wrapped, or CDM holdings payloads."""
    if not isinstance(payload, dict):
        return {}

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return {}

    summary = data.get("summary")
    if isinstance(summary, dict) and summary:
        return summary

    portfolio = data.get("portfolio")
    if isinstance(portfolio, dict) and isinstance(portfolio.get("summary"), dict):
        return portfolio["summary"]

    return {}


def normalize_summary_fields(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize compact snake_case and CDM camelCase summary keys."""
    if not isinstance(summary, dict):
        summary = {}

    normalized: Dict[str, Any] = {}
    for target, aliases in SUMMARY_FIELD_ALIASES.items():
        value = _first_present(summary, *aliases)
        if value is not None:
            normalized[target] = value

    total = normalized.get("total_value", 0.0)
    if "net_value" not in normalized:
        normalized["net_value"] = total

    for key in (
        "total_value",
        "equity_value",
        "bond_value",
        "cash_value",
        "margin_value",
        "crypto_value",
        "futures_value",
        "metals_value",
        "equity_pct",
        "bond_pct",
        "cash_pct",
        "margin_pct",
        "crypto_pct",
        "futures_pct",
        "metals_pct",
        "unrealized_gl",
        "unrealized_gl_pct",
    ):
        normalized.setdefault(key, 0.0)

    return normalized
