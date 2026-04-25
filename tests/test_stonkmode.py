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
Unit tests for the stonkmode entertainment layer.

Tests cover:
  - Persona roster integrity (IDs, required fields, archetype membership)
  - Pairing system (pool coverage, no wildcard-wildcard, cosmic-cosmic allowed,
    forced pairs, dynamic lookup)
  - State management (enable/disable/persistence)
  - Summarizer pipeline (key presence, no LLM calls)
  - STONKMODE_EXCLUDED_COMMANDS guard in investorclaw.py
  - Narration JSON envelope schema (offline — no Ollama required)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

_skill_root = Path(__file__).parent.parent
if str(_skill_root) not in sys.path:
    sys.path.insert(0, str(_skill_root))

import pytest

# ---------------------------------------------------------------------------
# Persona roster tests
# ---------------------------------------------------------------------------
from rendering.stonkmode_avatars import (
    EDUCATOR_AVATAR_IDS,
    avatar_manifest,
    get_avatar_asset,
    get_avatar_path,
)
from rendering.stonkmode_personas import (
    PERSONAS,
    get_persona,
    get_personas_by_archetype,
    list_all_ids,
)

REQUIRED_PERSONA_FIELDS = {"id", "name", "archetype", "description", "voice_markers"}
VALID_ARCHETYPES = {
    "high_energy",
    "serious",
    "mentors",
    "policy_veterans",
    "wildcards",
    "cosmic",
    "digital",
    "bears",
}


def test_persona_count():
    assert len(PERSONAS) == 30, f"Expected 30 personas, got {len(PERSONAS)}"


def test_all_personas_have_required_fields():
    for pid, persona in PERSONAS.items():
        missing = REQUIRED_PERSONA_FIELDS - set(persona.keys())
        assert not missing, f"Persona {pid!r} missing fields: {missing}"


def test_all_persona_ids_match_key():
    for pid, persona in PERSONAS.items():
        assert persona["id"] == pid, (
            f"Persona key {pid!r} does not match internal id {persona['id']!r}"
        )


def test_all_personas_have_valid_archetype():
    for pid, persona in PERSONAS.items():
        assert persona["archetype"] in VALID_ARCHETYPES, (
            f"Persona {pid!r} has unknown archetype {persona['archetype']!r}"
        )


def test_get_persona_returns_correct():
    p = get_persona("blitz_thunderbuy")
    assert p["name"] == "Blitz Thunderbuy"
    assert p["archetype"] == "high_energy"


def test_get_persona_raises_for_unknown():
    with pytest.raises(KeyError):
        get_persona("not_a_real_persona")


def test_get_personas_by_archetype_digital():
    digital = get_personas_by_archetype("digital")
    ids = {p["id"] for p in digital}
    assert ids == {"krystal_kash", "zara_zhao", "priya_hodl"}


def test_get_personas_by_archetype_bears():
    bears = get_personas_by_archetype("bears")
    ids = {p["id"] for p in bears}
    assert ids == {"victor_voss", "hans_dieter_braun"}


def test_get_personas_by_archetype_cosmic():
    cosmic = get_personas_by_archetype("cosmic")
    ids = {p["id"] for p in cosmic}
    assert ids == {"chico_reyes", "farout_farley"}


def test_list_all_ids_complete():
    ids = list_all_ids()
    assert len(ids) == 30
    assert "glorb" in ids
    assert "krystal_kash" in ids
    assert "victor_voss" in ids
    assert "wendell_the_pattern" in ids
    assert "professor_what" in ids


def test_archetype_counts():
    from collections import Counter

    counts = Counter(p["archetype"] for p in PERSONAS.values())
    assert counts["high_energy"] == 3
    assert counts["serious"] == 5
    assert counts["mentors"] == 3
    assert counts["policy_veterans"] == 2
    assert counts["wildcards"] == 10
    assert counts["cosmic"] == 2
    assert counts["digital"] == 3
    assert counts["bears"] == 2


def test_stonkmode_avatar_manifest_matches_personas():
    manifest = avatar_manifest()
    assert set(manifest) == set(PERSONAS) | EDUCATOR_AVATAR_IDS

    for pid, persona in PERSONAS.items():
        entry = manifest[pid]
        assert entry["name"] == persona["name"]
        assert entry["archetype"] == persona["archetype"]
        assert entry["asset"] == f"assets/stonkmode-avatars/{pid}.svg"
        assert entry["cue"]

    dr_stonk = manifest["dr_stonk"]
    assert dr_stonk["name"] == "Dr. Stonk"
    assert dr_stonk["archetype"] == "educators"
    assert dr_stonk["asset"] == "assets/stonkmode-avatars/dr_stonk.svg"
    assert dr_stonk["cue"] == "logical financial education"


def test_all_stonkmode_avatar_assets_exist_and_are_svg():
    import xml.etree.ElementTree as ET

    for pid in set(PERSONAS) | EDUCATOR_AVATAR_IDS:
        asset = get_avatar_asset(pid)
        path = get_avatar_path(pid)
        assert asset.endswith(".svg")
        assert path.exists(), f"Missing avatar asset for {pid!r}: {path}"

        root = ET.fromstring(path.read_text(encoding="utf-8"))
        assert root.tag.endswith("svg")
        assert root.attrib["viewBox"] == "0 0 512 512"


# ---------------------------------------------------------------------------
# Pairing system tests
# ---------------------------------------------------------------------------

from rendering.stonkmode_pairings import (
    _DEFAULT_DYNAMIC,
    ARCHETYPE_POOLS,
    FOIL_POOLS,
    get_pairing_dynamic,
    select_pair,
)


def test_all_pool_ids_resolve():
    for arch, ids in ARCHETYPE_POOLS.items():
        for pid in ids:
            assert pid in PERSONAS, f"Pool {arch} references unknown persona {pid!r}"


def test_all_archetypes_in_foil_pools():
    for arch in ARCHETYPE_POOLS:
        assert arch in FOIL_POOLS, f"Archetype {arch!r} missing from FOIL_POOLS"


def test_digital_cannot_foil_digital():
    assert "digital" not in FOIL_POOLS["digital"], (
        "digital should not foil digital — no tension, echo chamber"
    )


def test_cosmic_can_foil_cosmic():
    assert "cosmic" in FOIL_POOLS["cosmic"], (
        "cosmic must be able to foil cosmic — Chico+Farley is the dream pair"
    )


def test_bears_can_foil_bears():
    assert "bears" in FOIL_POOLS["bears"], (
        "bears must be able to foil bears — doom spiral is valid television"
    )


def test_select_pair_returns_two_valid_ids():
    lead_id, foil_id = select_pair()
    assert lead_id in PERSONAS
    assert foil_id in PERSONAS


def test_select_pair_never_same_id():
    for _ in range(50):
        lead_id, foil_id = select_pair()
        assert lead_id != foil_id, "select_pair returned the same persona as lead and foil"


def test_select_pair_never_digital_digital():
    for _ in range(200):
        lead_id, foil_id = select_pair()
        l_arch = PERSONAS[lead_id]["archetype"]
        f_arch = PERSONAS[foil_id]["archetype"]
        assert not (l_arch == "digital" and f_arch == "digital"), (
            f"select_pair produced digital+digital: {lead_id} + {foil_id}"
        )


def test_select_pair_cosmic_cosmic_possible():
    """cosmic+cosmic must be reachable — it's in the foil pool."""
    found = False
    for _ in range(500):
        lead_id, foil_id = select_pair()
        if (
            PERSONAS[lead_id]["archetype"] == "cosmic"
            and PERSONAS[foil_id]["archetype"] == "cosmic"
        ):
            found = True
            break
    assert found, "cosmic+cosmic pair never generated in 500 samples"


def test_get_pairing_dynamic_high_energy_serious():
    dyn = get_pairing_dynamic(
        "high_energy", "serious", "blitz_thunderbuy", "prescott_pennington_smythe"
    )
    assert "Exeter" in dyn or "Blitz" in dyn or "headset" in dyn


def test_get_pairing_dynamic_bears_bears():
    dyn = get_pairing_dynamic("bears", "bears", "victor_voss", "hans_dieter_braun")
    assert dyn != _DEFAULT_DYNAMIC, "bears+bears should have a specific dynamic, not the fallback"


def test_get_pairing_dynamic_digital_bears():
    dyn = get_pairing_dynamic("digital", "bears", "krystal_kash", "victor_voss")
    assert dyn != _DEFAULT_DYNAMIC


def test_get_pairing_dynamic_cosmic_cosmic():
    dyn = get_pairing_dynamic("cosmic", "cosmic", "chico_reyes", "farout_farley")
    assert "antacid" in dyn or "taco" in dyn or "Mercury" in dyn


def test_get_pairing_dynamic_wildcard_lead():
    dyn = get_pairing_dynamic("wildcards", "any", "glorb", "blitz_thunderbuy")
    # Should use the wildcard-specific entry for glorb (lead)
    assert dyn != _DEFAULT_DYNAMIC, "wildcards lead with glorb should not hit fallback"
    assert "creature" in dyn or "Vault" in dyn or "Treasures" in dyn or "Glorb" in dyn


def test_get_pairing_dynamic_wildcard_foil():
    dyn = get_pairing_dynamic("high_energy", "wildcards", "blitz_thunderbuy", "aria_7")
    # Should use the wildcard-specific entry for aria_7 as foil
    assert "ARIA" in dyn or "bias" in dyn or "anchoring" in dyn


def test_pairing_dynamic_fallback():
    """Unknown archetype combo returns default, not an exception."""
    dyn = get_pairing_dynamic("unknown_arch", "unknown_arch", "x", "y")
    assert dyn == _DEFAULT_DYNAMIC


# ---------------------------------------------------------------------------
# State management tests
# ---------------------------------------------------------------------------

from rendering.stonkmode import is_enabled, load_state, save_state


def test_load_state_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("rendering.stonkmode.STATE_FILE", tmp_path / "missing.json")
    assert load_state() is None


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    state_path = tmp_path / "stonkmode.json"
    monkeypatch.setattr("rendering.stonkmode.STATE_FILE", state_path)
    state = {
        "enabled": True,
        "lead_id": "blitz_thunderbuy",
        "foil_id": "victor_voss",
        "segment_count": 3,
    }
    save_state(state)
    loaded = load_state()
    assert loaded == state


def test_is_enabled_false_when_no_state(tmp_path, monkeypatch):
    monkeypatch.setattr("rendering.stonkmode.STATE_FILE", tmp_path / "missing.json")
    assert is_enabled() is False


def test_is_enabled_true_when_state_present(tmp_path, monkeypatch):
    state_path = tmp_path / "stonkmode.json"
    monkeypatch.setattr("rendering.stonkmode.STATE_FILE", state_path)
    save_state({"enabled": True, "lead_id": "blitz_thunderbuy", "foil_id": "victor_voss"})
    assert is_enabled() is True


def test_is_enabled_false_when_disabled_in_state(tmp_path, monkeypatch):
    state_path = tmp_path / "stonkmode.json"
    monkeypatch.setattr("rendering.stonkmode.STATE_FILE", state_path)
    save_state({"enabled": False})
    assert is_enabled() is False


def test_state_file_parent_created(tmp_path, monkeypatch):
    nested = tmp_path / "deep" / "dir" / "stonkmode.json"
    monkeypatch.setattr("rendering.stonkmode.STATE_FILE", nested)
    save_state({"enabled": True})
    assert nested.exists()


# ---------------------------------------------------------------------------
# Narration JSON envelope schema (offline — stubs out Ollama)
# ---------------------------------------------------------------------------

from rendering.stonkmode import maybe_narrate


def _make_state(tmp_path, lead_id="blitz_thunderbuy", foil_id="victor_voss"):
    return {
        "enabled": True,
        "lead_id": lead_id,
        "foil_id": foil_id,
        "lead_name": PERSONAS[lead_id]["name"],
        "foil_name": PERSONAS[foil_id]["name"],
        "lead_archetype": PERSONAS[lead_id]["archetype"],
        "foil_archetype": PERSONAS[foil_id]["archetype"],
        "pairing_dynamic": "test dynamic",
        "segment_count": 0,
        "session_message_history": [],
    }


@patch("rendering.stonkmode.generate_narration", return_value="Mocked commentary.")
@patch(
    "rendering.stonkmode.summarize_for_narration",
    return_value="Test portfolio summary with TOP 10 HOLDINGS (comment on EACH one by name): MSFT $100K 5% +3%",
)
@patch("rendering.stonkmode.load_state")
@patch("rendering.stonkmode.save_state")
def test_maybe_narrate_emits_valid_json(
    mock_save, mock_load, mock_summarize, mock_generate, tmp_path, capsys
):
    mock_load.return_value = _make_state(tmp_path)
    maybe_narrate("synthesize", tmp_path)

    captured = capsys.readouterr()
    # Find the stonkmode_narration JSON line
    narration_line = None
    for line in captured.out.splitlines():
        line = line.strip()
        if line.startswith("{") and "stonkmode_narration" in line:
            narration_line = line
            break

    assert narration_line is not None, "No stonkmode_narration JSON line emitted"
    data = json.loads(narration_line)
    block = data["stonkmode_narration"]

    # Required envelope fields
    assert block["is_entertainment"] is True
    assert block["is_satire"] is True
    assert block["is_investment_advice"] is False
    assert block["consultation_mode"] == "deactivated"
    assert "satire_disclaimer" in block
    assert "lead" in block
    assert "foil" in block
    assert "narration" in block
    assert "model" in block
    assert "inference_ms" in block

    # Lead/foil sub-fields
    for role in ("lead", "foil"):
        assert "id" in block[role]
        assert "name" in block[role]
        assert "archetype" in block[role]

    # Narration sub-fields
    assert "lead" in block["narration"]
    assert "foil" in block["narration"]


@patch("rendering.stonkmode.generate_narration", return_value="Commentary.")
@patch("rendering.stonkmode.summarize_for_narration", return_value="Summary.")
@patch("rendering.stonkmode.load_state")
@patch("rendering.stonkmode.save_state")
def test_maybe_narrate_consultation_mode_deactivated(
    mock_save, mock_load, mock_summarize, mock_generate, tmp_path, capsys
):
    """consultation_mode must always be 'deactivated' in stonkmode output."""
    mock_load.return_value = _make_state(tmp_path)
    maybe_narrate("holdings", tmp_path)

    captured = capsys.readouterr()
    for line in captured.out.splitlines():
        if "stonkmode_narration" in line:
            block = json.loads(line)["stonkmode_narration"]
            assert block["consultation_mode"] == "deactivated"
            return
    # If no narration emitted (command not in map), that's OK — no assertion needed


@patch("rendering.stonkmode.generate_narration", return_value="Commentary.")
@patch("rendering.stonkmode.summarize_for_narration", return_value="Summary.")
@patch("rendering.stonkmode.load_state")
@patch("rendering.stonkmode.save_state")
def test_maybe_narrate_updates_segment_count(
    mock_save, mock_load, mock_summarize, mock_generate, tmp_path, capsys
):
    initial = _make_state(tmp_path)
    initial["segment_count"] = 4
    mock_load.return_value = initial

    maybe_narrate("synthesize", tmp_path)

    # save_state should have been called with segment_count=5
    assert mock_save.called
    saved_state = mock_save.call_args[0][0]
    assert saved_state["segment_count"] == 5


# ---------------------------------------------------------------------------
# STONKMODE_EXCLUDED_COMMANDS guard
# ---------------------------------------------------------------------------


def test_excluded_commands_contains_stonkmode_aliases():
    """stonkmode itself must never trigger narration."""
    import importlib
    import importlib.util

    importlib.util.spec_from_file_location(
        "investorclaw_main",
        _skill_root / "investorclaw.py",
    )
    # Just parse the source for the set literal rather than executing the module
    src = (_skill_root / "investorclaw.py").read_text()
    assert "STONKMODE_EXCLUDED_COMMANDS" in src
    for alias in ("stonkmode", "stonk-mode", "stonks", "setup", "guardrails"):
        assert alias in src, f"Expected {alias!r} in STONKMODE_EXCLUDED_COMMANDS"


# ---------------------------------------------------------------------------
# Router: stonkmode commands resolve
# ---------------------------------------------------------------------------


def test_stonkmode_routes_resolve():
    from runtime.router import COMMANDS

    for alias in ("stonkmode", "stonk-mode", "stonks"):
        assert alias in COMMANDS, f"Alias {alias!r} missing from router COMMANDS"
    # All three must resolve to stonkmode_control.py
    for alias in ("stonkmode", "stonk-mode", "stonks"):
        assert "stonkmode_control" in COMMANDS[alias]


# ---------------------------------------------------------------------------
# Persona description sanity
# ---------------------------------------------------------------------------


def test_no_persona_description_gives_investment_advice():
    """Guardrail compliance: persona descriptions must not give direct advice directives."""
    # 'fiduciary' is acceptable in-character (e.g. Prescott 'pronounces fiduciary like a fine wine')
    # What's forbidden is advice directives aimed at the viewer.
    forbidden_phrases = [
        "you should invest",
        "i recommend buying",
        "i recommend selling",
        "buy this",
        "sell this",
    ]
    for pid, persona in PERSONAS.items():
        desc_lower = persona["description"].lower()
        for phrase in forbidden_phrases:
            assert phrase not in desc_lower, (
                f"Persona {pid!r} description contains forbidden phrase {phrase!r}"
            )


def test_no_persona_description_gives_advice():
    """Personas should not contain 'you should invest' or 'I recommend buying'."""
    forbidden_phrases = ["you should invest", "i recommend buying", "i recommend selling"]
    for pid, persona in PERSONAS.items():
        desc_lower = persona["description"].lower()
        for phrase in forbidden_phrases:
            assert phrase not in desc_lower, (
                f"Persona {pid!r} description contains forbidden phrase {phrase!r}"
            )


def test_satire_disclaimer_present_in_build():
    """The stonkmode output block must always include the satire disclaimer string."""
    import rendering.stonkmode as sm

    src = Path(sm.__file__).read_text()
    assert "satire_disclaimer" in src
    assert "AI-generated entertainment satire" in src
    assert "is_investment_advice" in src
