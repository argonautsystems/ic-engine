# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Helpers for locating Stonkmode persona avatar assets."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict

from ic_engine.rendering.stonkmode_personas import PERSONAS

_ROOT = Path(__file__).resolve().parents[1]
AVATAR_DIR = _ROOT / "assets" / "stonkmode-avatars"
MANIFEST_PATH = AVATAR_DIR / "manifest.json"
EDUCATOR_AVATAR_IDS = {"dr_stonk"}


@lru_cache(maxsize=1)
def avatar_manifest() -> Dict[str, dict]:
    """Return the avatar manifest keyed by persona id."""
    with MANIFEST_PATH.open(encoding="utf-8") as f:
        manifest = json.load(f)

    expected_ids = set(PERSONAS) | EDUCATOR_AVATAR_IDS
    missing = expected_ids - set(manifest)
    extra = set(manifest) - expected_ids
    if missing or extra:
        raise ValueError(
            f"Avatar manifest mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
        )
    return manifest


def get_avatar_asset(persona_id: str) -> str:
    """Return the repo-relative avatar asset path for a persona id."""
    return avatar_manifest()[persona_id]["asset"]


def get_avatar_path(persona_id: str) -> Path:
    """Return the absolute avatar asset path for a persona id."""
    asset = get_avatar_asset(persona_id)
    return _ROOT / asset
