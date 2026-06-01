# SPDX-License-Identifier: Apache-2.0
"""Tests for MCP key-management helpers."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from investorclaw_bridge import provider_routing as pr  # noqa: E402
from investorclaw_bridge.mcp.tools import keys as keys_module  # noqa: E402


def test_read_existing_returns_empty_for_permissive_keys_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    keys_file = tmp_path / "keys.env"
    keys_file.write_text("TOGETHER_API_KEY=tgp_v1_not_used\n")
    keys_file.chmod(0o644)
    monkeypatch.setenv("IC_KEYS_FILE", str(keys_file))

    assert keys_module._read_existing() == {}


def test_read_existing_parses_keys_file_with_safe_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    keys_file = tmp_path / "keys.env"
    keys_file.write_text(
        "TOGETHER_API_KEY=tgp_v1_ok\n"
        "FINNHUB_KEY='finnhub_ok'\n"
    )
    keys_file.chmod(0o600)
    monkeypatch.setenv("IC_KEYS_FILE", str(keys_file))

    assert keys_module._read_existing() == {
        "TOGETHER_API_KEY": "tgp_v1_ok",
        "FINNHUB_KEY": "finnhub_ok",
    }


# ──────────────────────────────────────────────────────────────────────
# _maybe_auto_route_massive — auto-pin behavior on key set/delete
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def routing_sandbox(tmp_path, monkeypatch):
    """Isolate routing-file + clear engine env vars between tests."""
    rfile = tmp_path / "provider_routing.env"
    marker_file = tmp_path / "routing_automanaged.json"
    monkeypatch.setenv("IC_PROVIDER_ROUTING_FILE", str(rfile))
    monkeypatch.setenv("IC_ROUTING_AUTOMANAGED_FILE", str(marker_file))
    monkeypatch.delenv("INVESTORCLAW_PRICE_PROVIDER", raising=False)
    monkeypatch.delenv("INVESTORCLAW_FALLBACK_CHAIN", raising=False)
    yield tmp_path


def test_auto_route_sets_massive_when_key_supplied_and_primary_unset(routing_sandbox):
    """Default 'auto' primary -> 'massive' when MASSIVE_API_KEY set."""
    assert pr.load_routing()["primary"] == "auto"
    change = keys_module._maybe_auto_route_massive({"MASSIVE_API_KEY": "live-key"})
    assert change == {
        "primary": "massive",
        "changed": True,
        "reason": "MASSIVE_API_KEY supplied",
    }
    assert pr.load_routing()["primary"] == "massive"
    assert keys_module._read_auto_pinned_providers(
        routing_sandbox / "routing_automanaged.json"
    ) == {"massive"}


def test_auto_route_no_op_when_other_key_set(routing_sandbox):
    """No MASSIVE_API_KEY in updates -> no routing change."""
    change = keys_module._maybe_auto_route_massive({"FINNHUB_KEY": "fh"})
    assert change is None
    assert pr.load_routing()["primary"] == "auto"


def test_auto_route_respects_explicit_non_default_primary(routing_sandbox):
    """User pinned to finnhub -> MASSIVE_API_KEY set must NOT clobber."""
    pr.save_routing(primary="finnhub", fallback_chain=[])
    change = keys_module._maybe_auto_route_massive({"MASSIVE_API_KEY": "live-key"})
    assert change is None
    assert pr.load_routing()["primary"] == "finnhub"


def test_auto_route_reverts_to_auto_when_massive_deleted(routing_sandbox):
    """MASSIVE_API_KEY set -> primary pinned to massive. Then key deletion -> revert to auto."""
    keys_module._maybe_auto_route_massive({"MASSIVE_API_KEY": "live-key"})
    assert pr.load_routing()["primary"] == "massive"
    change = keys_module._maybe_auto_route_massive({"MASSIVE_API_KEY": ""})
    assert change == {
        "primary": "auto",
        "changed": True,
        "reason": "MASSIVE_API_KEY removed",
    }
    assert pr.load_routing()["primary"] == "auto"
    assert not (routing_sandbox / "routing_automanaged.json").exists()


@pytest.mark.asyncio
async def test_delete_massive_respects_explicit_massive_primary(routing_sandbox, monkeypatch):
    """User-pinned primary=massive must survive MASSIVE_API_KEY deletion."""
    pr.save_routing(primary="massive", fallback_chain=[])
    monkeypatch.setattr(keys_module, "_persist", lambda updates: None)

    response = await keys_module.portfolio_keys_delete("MASSIVE_API_KEY")

    assert response == {"deleted": True, "name": "MASSIVE_API_KEY"}
    assert pr.load_routing()["primary"] == "massive"


def test_auto_route_delete_no_op_when_primary_is_not_massive(routing_sandbox):
    """If user explicitly pinned to finnhub, deleting MASSIVE_API_KEY must not change routing."""
    pr.save_routing(primary="finnhub", fallback_chain=[])
    change = keys_module._maybe_auto_route_massive({"MASSIVE_API_KEY": ""})
    assert change is None
    assert pr.load_routing()["primary"] == "finnhub"


def test_auto_route_empty_string_value_treated_as_deletion(routing_sandbox):
    """Whitespace-only MASSIVE_API_KEY value treated as delete signal."""
    keys_module._maybe_auto_route_massive({"MASSIVE_API_KEY": "live-key"})
    change = keys_module._maybe_auto_route_massive({"MASSIVE_API_KEY": "   "})
    assert change == {
        "primary": "auto",
        "changed": True,
        "reason": "MASSIVE_API_KEY removed",
    }


@pytest.mark.asyncio
async def test_set_massive_surfaces_routing_write_failure(routing_sandbox, monkeypatch):
    """Key write still succeeds, but routing failure is visible to caller."""
    monkeypatch.setattr(keys_module, "_persist", lambda updates: None)
    monkeypatch.setattr(
        pr,
        "save_routing",
        lambda **kwargs: {"error": "routing_write_failed", "detail": "disk full"},
    )

    response = await keys_module.portfolio_keys_set({"MASSIVE_API_KEY": "live-key"})

    assert response["configured"] == ["MASSIVE_API_KEY"]
    assert response["routing_change"]["status"] == "routing_write_failed"
    assert response["routing_change"]["error_detail"] == "disk full"


@pytest.mark.asyncio
async def test_delete_massive_surfaces_routing_write_failure(routing_sandbox, monkeypatch):
    """Auto-revert failure on deletion is visible to caller."""
    keys_module._maybe_auto_route_massive({"MASSIVE_API_KEY": "live-key"})
    assert pr.load_routing()["primary"] == "massive"
    monkeypatch.setattr(keys_module, "_persist", lambda updates: None)
    monkeypatch.setattr(
        pr,
        "save_routing",
        lambda **kwargs: {"error": "routing_write_failed", "detail": "read-only volume"},
    )

    response = await keys_module.portfolio_keys_delete("MASSIVE_API_KEY")

    assert response["deleted"] is True
    assert response["routing_change"]["status"] == "routing_write_failed"
    assert response["routing_change"]["error_detail"] == "read-only volume"
    assert pr.load_routing()["primary"] == "massive"
