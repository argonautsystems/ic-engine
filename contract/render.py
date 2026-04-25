#!/usr/bin/env python3
# Copyright 2026 InvestorClaw Contributors
# Licensed under the Apache License, Version 2.0
"""Render canonical routing-rules block into per-runtime SKILL.md files.

Phase 0.5 prototype of the IC_DECOMPOSITION_SPEC contract centralization.
Reads contract/routing_rules.md.template and contract/routes.toml, then
substitutes runtime-specific values (slash prefix, command form, forbidden
paths) and writes the result between BEGIN/END markers in each target
SKILL.md file.

Usage:
    uv run python contract/render.py             # render all runtimes
    uv run python contract/render.py --check     # diff-only, no write
    uv run python contract/render.py --runtime=claude --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # py<3.11
    import tomli as tomllib  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DIR = PROJECT_ROOT / "contract"
TEMPLATE_PATH = CONTRACT_DIR / "routing_rules.md.template"
ROUTES_PATH = CONTRACT_DIR / "routes.toml"

BEGIN_MARKER = "<!-- BEGIN_ROUTING_RULES -->"
END_MARKER = "<!-- END_ROUTING_RULES -->"


def _render_for_runtime(template: str, routes: dict, runtime: str) -> str:
    """Substitute runtime-specific placeholders into the template."""
    cfg = routes["runtimes"][runtime]
    forbidden = routes["contract"]["forbidden_paths"]
    bullets = "\n".join(f"  - {path}" for path in forbidden)

    return (
        template
        .replace("{{slash_prefix}}", cfg["slash_prefix"])
        .replace("{{command_form}}", cfg["command_form"])
        .replace("{{forbidden_paths_bullets}}", bullets)
    )


def _splice(target_text: str, rendered: str) -> str:
    """Replace content between BEGIN/END markers with the rendered block.

    Marker preservation: the BEGIN/END marker comments stay in the file;
    only the content between them is replaced. If markers are missing, the
    function raises — refusing to silently append (which would create
    multiple copies on re-render).
    """
    begin_idx = target_text.find(BEGIN_MARKER)
    end_idx = target_text.find(END_MARKER)
    if begin_idx < 0 or end_idx < 0 or end_idx < begin_idx:
        raise RuntimeError(
            f"Markers {BEGIN_MARKER} / {END_MARKER} not found (or out of order) "
            "in target file. Add them around the routing-rules section before "
            "running the renderer."
        )

    before = target_text[: begin_idx + len(BEGIN_MARKER)]
    after = target_text[end_idx:]
    return f"{before}\n{rendered}\n{after}"


def render_runtime(runtime: str, *, check_only: bool = False) -> int:
    """Render the routing-rules block into the target SKILL.md.

    Returns 0 on success / no-change, 1 on diff-without-write (check mode),
    2 on render error (markers missing, etc).
    """
    routes = tomllib.loads(ROUTES_PATH.read_text(encoding="utf-8"))
    if runtime not in routes["runtimes"]:
        print(f"ERROR: unknown runtime '{runtime}'. Known: {list(routes['runtimes'].keys())}", file=sys.stderr)
        return 2

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = _render_for_runtime(template, routes, runtime)

    target_rel = routes["runtimes"][runtime]["target_skill_md"]
    target_abs = PROJECT_ROOT / target_rel
    if not target_abs.exists():
        print(f"ERROR: target {target_rel} does not exist", file=sys.stderr)
        return 2

    current = target_abs.read_text(encoding="utf-8")
    try:
        spliced = _splice(current, rendered)
    except RuntimeError as exc:
        print(f"ERROR rendering {target_rel}: {exc}", file=sys.stderr)
        return 2

    if spliced == current:
        print(f"  {runtime:8s} → {target_rel}: no change")
        return 0

    if check_only:
        print(f"  {runtime:8s} → {target_rel}: WOULD CHANGE", file=sys.stderr)
        return 1

    target_abs.write_text(spliced, encoding="utf-8")
    print(f"  {runtime:8s} → {target_rel}: rendered")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="InvestorClaw routing-rules contract renderer")
    parser.add_argument("--runtime", help="render only this runtime (default: all)")
    parser.add_argument("--check", action="store_true",
                        help="diff-only mode — fail if any target would change")
    args = parser.parse_args()

    routes = tomllib.loads(ROUTES_PATH.read_text(encoding="utf-8"))
    runtimes = [args.runtime] if args.runtime else list(routes["runtimes"].keys())

    print(f"Rendering routing-rules contract (canonical engine version: "
          f"{routes['contract']['canonical_engine_version']})")
    overall = 0
    for rt in runtimes:
        rc = render_runtime(rt, check_only=args.check)
        overall = max(overall, rc)
    return overall


if __name__ == "__main__":
    sys.exit(main())
