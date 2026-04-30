#!/usr/bin/env python3
# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Top-level deterministic-first ask/refresh entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ic_engine.config.path_resolver import find_portfolio_file, get_reports_dir
from ic_engine.config.user_messages import (
    CACHE_HIT_BANNER,
    NARRATOR_FABRICATION_REFUSAL,
    PIPELINE_PARTIAL_FAILURE,
    PIPELINE_TOTAL_FAILURE,
    WAIT_FIRST_RUN,
    WAIT_PARTIAL_REFRESH,
    WAIT_REFRESH,
)
from ic_engine.runtime.envelope_cache import (
    cache_status,
    get_or_run,
    load_cached_envelope,
)
from ic_engine.runtime.full_run import DEFAULT_SECTION_TTLS
from ic_engine.runtime.narrator import FabricationError, narrate


def _skill_dir() -> Path:
    return Path(os.environ.get("INVESTORCLAW_SKILL_DIR") or os.getcwd()).expanduser()


def _resolve_portfolio(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    found = find_portfolio_file(_skill_dir())
    return Path(found).expanduser() if found else None


def _eta_for_section(section: str) -> int:
    ttl = DEFAULT_SECTION_TTLS.get(section, 60)
    if section == "news":
        return 30
    return min(max(ttl, 5), 60)


def _emit_wait_messages(status: dict, force_refresh: bool) -> None:
    if force_refresh:
        print(WAIT_REFRESH, flush=True)
        return
    if not status["exists"] or not status["valid"]:
        print(WAIT_FIRST_RUN, flush=True)
        return
    for section in status["stale_sections"]:
        print(
            WAIT_PARTIAL_REFRESH.format(section=section, eta_seconds=_eta_for_section(section)),
            flush=True,
        )
    for section in status["missing_sections"]:
        print(
            WAIT_PARTIAL_REFRESH.format(section=section, eta_seconds=_eta_for_section(section)),
            flush=True,
        )


def _failure_reasons(envelope: dict) -> str:
    reasons = []
    for section in envelope.get("failed_sections", []):
        meta = envelope.get("section_meta", {}).get(section, {})
        reason = meta.get("error") or "section failed"
        reasons.append(f"{section}: {reason}")
    return "; ".join(reasons)


def _age_seconds(envelope: dict) -> int:
    generated_at = str(envelope.get("generated_at", ""))
    try:
        if generated_at.endswith("Z"):
            generated_at = generated_at[:-1] + "+00:00"
        generated = datetime.fromisoformat(generated_at)
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - generated).total_seconds()))
    except ValueError:
        return 0


def _print_ic_result(envelope: dict, command: str) -> None:
    ic = envelope["ic_result"]
    print(
        json.dumps(
            {
                "ic_result": {
                    "hmac": ic["hmac"],
                    "engine_version": ic["engine_version"],
                    "command": command,
                    "run_id": ic["run_id"],
                }
            }
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ask a question against the v2.5 envelope cache.")
    parser.add_argument("question", nargs="*", help="Natural-language question to answer")
    parser.add_argument("--portfolio", help="Explicit holdings/portfolio file path")
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use the existing cache when present, even if one or more sections are stale.",
    )
    parser.add_argument(
        "--refresh-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    holdings_path = _resolve_portfolio(args.portfolio)
    if holdings_path is None or not holdings_path.exists():
        print(
            PIPELINE_TOTAL_FAILURE.format(
                error="No portfolio file found",
                portfolio_path=str(holdings_path or _skill_dir() / "portfolios"),
            )
        )
        return 1

    # Cold-cache safety: if a CSV/XLS portfolio was selected and there's no
    # materialized holdings.json yet, run the bootstrap (same path setup uses)
    # to convert it BEFORE cache_status hashes the path and BEFORE get_or_run
    # reaches HoldingsLoader (which only knows json.load). Rebind holdings_path
    # to the materialized JSON so the rest of the pipeline operates on the
    # correct artifact and cache lookups hit the right key.
    from ic_engine.runtime.router import auto_bootstrap_holdings
    materialized = auto_bootstrap_holdings(
        "ask", _skill_dir(), get_reports_dir(), portfolio_path=holdings_path
    )
    if materialized is not None:
        holdings_path = materialized

    status = cache_status(holdings_path)
    force_refresh = bool(args.refresh_only)
    if force_refresh or (status["needs_run"] and not args.no_refresh):
        _emit_wait_messages(status, force_refresh)

    try:
        if args.no_refresh and status["exists"] and status["valid"]:
            envelope = load_cached_envelope(holdings_path)
            if envelope is None:
                envelope = get_or_run(holdings_path, force_refresh=False)
        else:
            envelope = get_or_run(holdings_path, force_refresh=force_refresh)
    except Exception as exc:
        print(
            PIPELINE_TOTAL_FAILURE.format(
                error=str(exc),
                portfolio_path=str(holdings_path),
            )
        )
        return 1

    if envelope.get("failed_sections"):
        print(
            PIPELINE_PARTIAL_FAILURE.format(
                failed_sections=", ".join(envelope["failed_sections"]),
                failure_reasons=_failure_reasons(envelope),
            )
        )

    if args.refresh_only:
        print(f"Refresh complete (envelope hash: {envelope['ic_result']['hmac']}).")
        _print_ic_result(envelope, "refresh")
        return 0

    question = " ".join(args.question).strip()
    if not question:
        parser.error(
            'ask requires a question, for example: investorclaw ask "What is in my portfolio?"'
        )

    if not status["needs_run"] and status["valid"]:
        print(
            CACHE_HIT_BANNER.format(
                age_seconds=_age_seconds(envelope),
                envelope_hmac_short=envelope["ic_result"]["hmac"][:12],
            )
        )

    try:
        result = narrate(envelope, question)
        print(result.answer)
    except FabricationError:
        print(NARRATOR_FABRICATION_REFUSAL.format(missing_data_class="requested numeric data"))
    _print_ic_result(envelope, "ask")
    return 0


if __name__ == "__main__":
    sys.exit(main())
