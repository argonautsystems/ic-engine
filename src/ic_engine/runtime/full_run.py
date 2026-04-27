# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""v2.5 full-pipeline entry point that returns a signed envelope."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from ic_engine.internal.pipeline import PortfolioPipeline
from ic_engine.internal.stages import PipelineResult, StageResult
from ic_engine.runtime.envelope import (
    CANONICAL_SECTIONS,
    Envelope,
    attach_hmac,
    new_ic_result,
    portfolio_id_for_holdings,
    utc_now_iso,
)

DEFAULT_SECTION_TTLS: dict[str, int] = {
    "holdings": 300,
    "performance": 300,
    "bonds": 3600,
    "bond_yields": 3600,
    "analyst": 1800,
    "news": 30,
    "synthesize": 300,
    "optimize": 300,
    "cashflow": 300,
    "peer": 300,
}

_STAGE_TO_SECTION = {
    "synthesis": "synthesize",
    "optimization": "optimize",
}


def _section_name(stage_name: str) -> str:
    return _STAGE_TO_SECTION.get(stage_name, stage_name)


def _stage_payload(result: StageResult) -> dict[str, Any]:
    if isinstance(result.data, dict):
        return result.data
    if result.data is None:
        return {}
    return {"value": result.data}


def envelope_from_pipeline_result(
    pipeline_result: PipelineResult,
    holdings_path: str | Path,
    ttl_by_section: dict[str, int] | None = None,
    command: str = "run_full",
) -> Envelope:
    """Convert a PipelineResult into the canonical signed envelope."""
    ttl_map = {**DEFAULT_SECTION_TTLS, **(ttl_by_section or {})}
    generated_at = utc_now_iso()
    run_id = str(uuid.uuid4())
    sections: dict[str, dict[str, Any]] = {}
    section_meta = {}
    failed_sections: list[str] = []

    stage_by_section = {
        _section_name(stage_name): result for stage_name, result in pipeline_result.stages.items()
    }

    for section in CANONICAL_SECTIONS:
        result = stage_by_section.get(section)
        if result is None:
            sections[section] = {}
            failed_sections.append(section)
            section_meta[section] = {
                "computed_at": generated_at,
                "ttl_seconds": ttl_map.get(section, 300),
                "source": section,
                "status": "missing",
                "error": "Section did not run",
            }
            continue

        sections[section] = _stage_payload(result)
        meta = {
            "computed_at": generated_at,
            "ttl_seconds": ttl_map.get(section, 300),
            "source": result.stage_name,
            "status": result.status,
        }
        if result.error:
            meta["error"] = result.error
        section_meta[section] = meta
        if result.status == "failed":
            failed_sections.append(section)

    envelope: Envelope = {
        "schema_version": "v2.5.0",
        "generated_at": generated_at,
        "portfolio_id": portfolio_id_for_holdings(holdings_path),
        "ic_result": new_ic_result(command=command, run_id=run_id),
        "sections": sections,
        "section_meta": section_meta,
        "failed_sections": failed_sections,
    }
    return attach_hmac(envelope)


async def run_full_async(
    holdings_path: str | Path,
    *,
    ttl_by_section: dict[str, int] | None = None,
    config_path: Path | None = None,
    cache_dir: Path | None = None,
) -> Envelope:
    """Run all deterministic sections and return a signed envelope."""
    pipeline = PortfolioPipeline(config_path=config_path, cache_dir=cache_dir)
    pipeline_result = await pipeline.run_full(str(Path(holdings_path).expanduser()))
    return envelope_from_pipeline_result(
        pipeline_result,
        holdings_path,
        ttl_by_section=ttl_by_section,
        command="run_full",
    )


def run_full(
    holdings_path: str | Path,
    *,
    ttl_by_section: dict[str, int] | None = None,
    config_path: Path | None = None,
    cache_dir: Path | None = None,
) -> Envelope:
    """Synchronous entry point for the v2.5 full pipeline."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            run_full_async(
                holdings_path,
                ttl_by_section=ttl_by_section,
                config_path=config_path,
                cache_dir=cache_dir,
            )
        )
    raise RuntimeError(
        "run_full() cannot be called from a running event loop; use run_full_async()"
    )
