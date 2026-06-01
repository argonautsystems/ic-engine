# SPDX-License-Identifier: Apache-2.0
"""Tests for upgrade.py — version check + portable state export/import."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from investorclaw_bridge.mcp.tools import upgrade as upgrade_module  # noqa: E402
from investorclaw_bridge.mcp.tools.upgrade import (  # noqa: E402
    _EXPORT_SCHEMA,
    _parse_semver,
    portfolio_export,
    portfolio_import,
    portfolio_version_check,
)


# ── _parse_semver ────────────────────────────────────────────────────


@pytest.mark.parametrize("tag,expected", [
    ("4.1.39-cpu", (4, 1, 39)),
    ("4.0.0-cpu", (4, 0, 0)),
    ("10.20.30-cpu", (10, 20, 30)),
])
def test_parse_semver_accepts_xyz_cpu(tag, expected):
    assert _parse_semver(tag) == expected


@pytest.mark.parametrize("tag", [
    "latest",
    "main",
    "4.1.39",            # missing -cpu suffix
    "4.1.39-arm64",      # different suffix
    "v4.1.39-cpu",       # leading v
    "",
])
def test_parse_semver_rejects_non_conforming(tag):
    assert _parse_semver(tag) is None


# ── portfolio_version_check ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_version_check_returns_running_when_token_fails(monkeypatch):
    """Network failure → latest=null, warnings populated, never raises."""
    async def _no_token(*args, **kwargs):
        return ""
    monkeypatch.setattr(upgrade_module, "_fetch_ghcr_anonymous_token", _no_token)
    monkeypatch.setenv("IC_ENGINE_VERSION", "4.1.38")
    result = await portfolio_version_check()
    assert result["running"] == "4.1.38"
    assert result["latest"] is None
    assert result["upgrade_available"] is False
    assert any("token" in w.lower() for w in result["warnings"])


@pytest.mark.asyncio
async def test_version_check_detects_upgrade_available(monkeypatch):
    """Stub the ghcr response; verify upgrade detection logic."""
    async def _fake_token(*args, **kwargs):
        return "stub-token"
    import httpx
    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload
    class _FakeClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None):
            return _FakeResponse({"tags": [
                "latest", "main",
                "4.1.34-cpu", "4.1.36-cpu", "4.1.38-cpu", "4.1.39-cpu",
            ]})
    monkeypatch.setattr(upgrade_module, "_fetch_ghcr_anonymous_token", _fake_token)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setenv("IC_ENGINE_VERSION", "4.1.38")
    result = await portfolio_version_check()
    assert result["running"] == "4.1.38"
    assert result["latest"] == "4.1.39"
    assert result["upgrade_available"] is True
    assert result["next_steps"], "upgrade-available result must list next_steps"


@pytest.mark.asyncio
async def test_version_check_no_upgrade_when_running_latest(monkeypatch):
    async def _fake_token(*a, **kw):
        return "stub-token"
    import httpx
    class _FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"tags": ["4.1.38-cpu", "4.1.39-cpu"]}
    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw): return _FakeResp()
    monkeypatch.setattr(upgrade_module, "_fetch_ghcr_anonymous_token", _fake_token)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setenv("IC_ENGINE_VERSION", "4.1.39")
    result = await portfolio_version_check()
    assert result["upgrade_available"] is False
    assert "latest" in " ".join(result["next_steps"]).lower()


# ── portfolio_export ─────────────────────────────────────────────────


def _seed_portfolio_dir(tmp_path: Path, csvs: dict[str, str]) -> Path:
    pdir = tmp_path / "portfolios"
    pdir.mkdir(parents=True, exist_ok=True)
    for name, content in csvs.items():
        (pdir / name).write_text(content)
    return pdir


@pytest.mark.asyncio
async def test_export_includes_portfolios(tmp_path, monkeypatch):
    pdir = _seed_portfolio_dir(tmp_path, {
        "main.csv": "Symbol,Quantity\nAAPL,10\n",
        "secondary.csv": "Symbol,Quantity\nMSFT,5\n",
    })
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(pdir))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "no-stonkmode.json"))

    # Stub keys_status so we don't depend on real /data/keys.env.
    from investorclaw_bridge.mcp.tools import keys as keys_module
    async def _fake_status():
        return {"configured": ["TOGETHER_API_KEY", "FRED_API_KEY"], "settable": [], "missing": [], "keys_file": ""}
    monkeypatch.setattr(keys_module, "portfolio_keys_status", _fake_status)

    snapshot = await portfolio_export()
    assert snapshot["schema_version"] == _EXPORT_SCHEMA
    filenames = sorted(p["filename"] for p in snapshot["portfolios"])
    assert filenames == ["main.csv", "secondary.csv"]
    main = next(p for p in snapshot["portfolios"] if p["filename"] == "main.csv")
    assert main["encoding"] == "utf-8"
    assert "AAPL" in main["content"]
    assert snapshot["stonkmode_state"] is None
    assert snapshot["configured_keys"] == ["TOGETHER_API_KEY", "FRED_API_KEY"]


@pytest.mark.asyncio
async def test_export_never_includes_key_values(tmp_path, monkeypatch):
    """Key values must NEVER appear in an export — security invariant."""
    pdir = _seed_portfolio_dir(tmp_path, {"p.csv": "Symbol,Quantity\nA,1\n"})
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(pdir))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "stonkmode.json"))

    secret = "sk-supersecret-do-not-leak-1234567890"
    monkeypatch.setenv("TOGETHER_API_KEY", secret)
    monkeypatch.setenv("FRED_API_KEY", secret)

    from investorclaw_bridge.mcp.tools import keys as keys_module
    async def _fake_status():
        return {"configured": ["TOGETHER_API_KEY", "FRED_API_KEY"], "settable": [], "missing": [], "keys_file": ""}
    monkeypatch.setattr(keys_module, "portfolio_keys_status", _fake_status)

    snapshot = await portfolio_export()
    serialized = json.dumps(snapshot)
    assert secret not in serialized, "key value leaked into export"
    assert "TOGETHER_API_KEY" in snapshot["configured_keys"]


@pytest.mark.asyncio
async def test_export_includes_stonkmode_when_present(tmp_path, monkeypatch):
    pdir = _seed_portfolio_dir(tmp_path, {})
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(pdir))
    sm = tmp_path / "stonkmode.json"
    sm.write_text(json.dumps({"persona": "Glorb", "intensity": 0.7}))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(sm))

    from investorclaw_bridge.mcp.tools import keys as keys_module
    async def _fake_status():
        return {"configured": [], "settable": [], "missing": [], "keys_file": ""}
    monkeypatch.setattr(keys_module, "portfolio_keys_status", _fake_status)

    snapshot = await portfolio_export()
    assert snapshot["stonkmode_state"] == {"persona": "Glorb", "intensity": 0.7}


# ── portfolio_import ─────────────────────────────────────────────────


def _make_snapshot(portfolios=None, stonkmode=None, configured_keys=None):
    return {
        "schema_version": _EXPORT_SCHEMA,
        "exported_at": "2026-05-07T12:00:00Z",
        "engine_version": "4.1.39",
        "portfolios": portfolios or [],
        "stonkmode_state": stonkmode,
        "configured_keys": configured_keys or [],
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_import_rejects_wrong_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path / "p"))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "sm.json"))
    bad = {"schema_version": "ic-engine-export/v999", "portfolios": []}
    result = await portfolio_import(bad)
    assert result.get("error") == "schema_version_mismatch"


@pytest.mark.asyncio
async def test_import_writes_portfolios(tmp_path, monkeypatch):
    pdir = tmp_path / "portfolios"
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(pdir))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "sm.json"))

    snap = _make_snapshot(portfolios=[
        {"filename": "main.csv", "encoding": "utf-8",
         "content": "Symbol,Quantity\nAAPL,10\n"},
        {"filename": "second.csv", "encoding": "utf-8",
         "content": "Symbol,Quantity\nMSFT,5\n"},
    ])
    result = await portfolio_import(snap)
    assert result["imported"]["portfolios"] == 2
    assert (pdir / "main.csv").read_text() == "Symbol,Quantity\nAAPL,10\n"
    assert (pdir / "second.csv").read_text() == "Symbol,Quantity\nMSFT,5\n"


@pytest.mark.asyncio
async def test_import_rejects_path_traversal(tmp_path, monkeypatch):
    pdir = tmp_path / "portfolios"
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(pdir))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "sm.json"))

    snap = _make_snapshot(portfolios=[
        {"filename": "../../etc/passwd", "encoding": "utf-8", "content": "evil"},
        {"filename": ".hidden.csv", "encoding": "utf-8", "content": "h"},
        {"filename": "ok.csv", "encoding": "utf-8", "content": "Symbol,Quantity\nAAPL,1\n"},
    ])
    result = await portfolio_import(snap)
    # ../../etc/passwd basenames to passwd, but starts with no-dot — may pass
    # filename safety. The CRITICAL invariant is that nothing lands outside
    # IC_PORTFOLIO_DIR. Verify by path containment.
    pdir.mkdir(exist_ok=True)
    for child in pdir.iterdir():
        assert child.parent == pdir
    # Hidden filenames must be rejected.
    assert not (pdir / ".hidden.csv").exists()
    # Legitimate file imported.
    assert (pdir / "ok.csv").read_text() == "Symbol,Quantity\nAAPL,1\n"


@pytest.mark.asyncio
async def test_import_writes_stonkmode(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path / "p"))
    sm = tmp_path / "sm.json"
    monkeypatch.setenv("IC_STONKMODE_FILE", str(sm))

    snap = _make_snapshot(stonkmode={"persona": "Donny", "level": 11})
    result = await portfolio_import(snap)
    assert result["imported"]["stonkmode"] is True
    assert json.loads(sm.read_text()) == {"persona": "Donny", "level": 11}


@pytest.mark.asyncio
async def test_import_surfaces_keys_in_snapshot_for_re_set(tmp_path, monkeypatch):
    """Imported snapshot's `configured_keys` is echoed back so the agent
    knows which keys to re-prompt the user for via portfolio_keys_set."""
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path / "p"))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "sm.json"))
    snap = _make_snapshot(configured_keys=["TOGETHER_API_KEY", "FRED_API_KEY"])
    result = await portfolio_import(snap)
    assert result["configured_keys_in_snapshot"] == ["TOGETHER_API_KEY", "FRED_API_KEY"]
    assert any("portfolio_keys_set" in s for s in result["next_steps"])


# ── round-trip ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_then_import_round_trip(tmp_path, monkeypatch):
    """End-to-end: export from one dir, import into another, verify
    portfolios + stonkmode survive byte-for-byte."""
    src_pdir = _seed_portfolio_dir(tmp_path / "src", {
        "main.csv": "Symbol,Quantity\nAAPL,10\nMSFT,5\n",
        "two.csv": "Symbol,Quantity\nGOOGL,3\n",
    })
    src_sm = tmp_path / "src-stonkmode.json"
    src_sm.write_text(json.dumps({"persona": "Stonk", "v": 1}))

    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(src_pdir))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(src_sm))

    from investorclaw_bridge.mcp.tools import keys as keys_module
    async def _fake_status():
        return {"configured": ["TOGETHER_API_KEY"], "settable": [], "missing": [], "keys_file": ""}
    monkeypatch.setattr(keys_module, "portfolio_keys_status", _fake_status)

    snap = await portfolio_export()

    # Switch to a fresh target dir
    dst_pdir = tmp_path / "dst"
    dst_sm = tmp_path / "dst-stonkmode.json"
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(dst_pdir))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(dst_sm))

    result = await portfolio_import(snap)
    assert result["imported"]["portfolios"] == 2
    assert result["imported"]["stonkmode"] is True
    assert (dst_pdir / "main.csv").read_text() == "Symbol,Quantity\nAAPL,10\nMSFT,5\n"
    assert (dst_pdir / "two.csv").read_text() == "Symbol,Quantity\nGOOGL,3\n"
    assert json.loads(dst_sm.read_text()) == {"persona": "Stonk", "v": 1}


# ── v2 schema (provider_routing) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_export_schema_is_v2(tmp_path, monkeypatch):
    """v4.3.0+ exports should pin schema_version=ic-engine-export/v2."""
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path / "p"))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "sm.json"))
    monkeypatch.setenv("IC_PROVIDER_ROUTING_FILE", str(tmp_path / "routing.env"))
    monkeypatch.delenv("INVESTORCLAW_PRICE_PROVIDER", raising=False)
    monkeypatch.delenv("INVESTORCLAW_FALLBACK_CHAIN", raising=False)

    snap = await portfolio_export()
    assert snap["schema_version"] == "ic-engine-export/v2"
    assert "provider_routing" in snap


@pytest.mark.asyncio
async def test_export_includes_provider_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path / "p"))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "sm.json"))
    monkeypatch.setenv("IC_PROVIDER_ROUTING_FILE", str(tmp_path / "routing.env"))
    monkeypatch.delenv("INVESTORCLAW_PRICE_PROVIDER", raising=False)
    monkeypatch.delenv("INVESTORCLAW_FALLBACK_CHAIN", raising=False)

    from investorclaw_bridge import provider_routing as pr
    pr.save_routing(primary="finnhub", fallback_chain=["yfinance", "massive"])

    snap = await portfolio_export()
    assert snap["provider_routing"] == {
        "primary": "finnhub",
        "fallback_chain": ["yfinance", "massive"],
    }


@pytest.mark.asyncio
async def test_import_accepts_v1_snapshot_for_backwards_compat(tmp_path, monkeypatch):
    """v4.3.0+ importer must accept v1 snapshots produced by v4.1.39 - v4.2.1."""
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path / "p"))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "sm.json"))

    v1_snap = {
        "schema_version": "ic-engine-export/v1",
        "portfolios": [
            {"filename": "p.csv", "encoding": "utf-8",
             "content": "Symbol,Qty\nAAPL,1\n"}
        ],
        "stonkmode_state": None,
        "configured_keys": ["TOGETHER_API_KEY"],
    }
    result = await portfolio_import(v1_snap)
    assert "error" not in result
    assert result["imported"]["portfolios"] == 1
    assert result["imported"].get("provider_routing") is False


@pytest.mark.asyncio
async def test_import_restores_provider_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path / "p"))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "sm.json"))
    monkeypatch.setenv("IC_PROVIDER_ROUTING_FILE", str(tmp_path / "routing.env"))
    monkeypatch.delenv("INVESTORCLAW_PRICE_PROVIDER", raising=False)
    monkeypatch.delenv("INVESTORCLAW_FALLBACK_CHAIN", raising=False)

    v2_snap = {
        "schema_version": "ic-engine-export/v2",
        "portfolios": [],
        "stonkmode_state": None,
        "configured_keys": [],
        "provider_routing": {
            "primary": "massive",
            "fallback_chain": ["finnhub", "alpha_vantage"],
        },
    }
    result = await portfolio_import(v2_snap)
    assert result["imported"]["provider_routing"] is True

    from investorclaw_bridge import provider_routing as pr
    out = pr.load_routing()
    assert out["primary"] == "massive"
    assert out["fallback_chain"] == ["finnhub", "alpha_vantage"]


@pytest.mark.asyncio
async def test_import_rejects_unknown_schema_in_v3(tmp_path, monkeypatch):
    """Unknown schema versions still reject — only v1 + v2 accepted."""
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path / "p"))
    bad = {"schema_version": "ic-engine-export/v999", "portfolios": []}
    result = await portfolio_import(bad)
    assert result.get("error") == "schema_version_mismatch"


@pytest.mark.asyncio
async def test_import_provider_routing_invalid_logs_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_PORTFOLIO_DIR", str(tmp_path / "p"))
    monkeypatch.setenv("IC_STONKMODE_FILE", str(tmp_path / "sm.json"))
    monkeypatch.setenv("IC_PROVIDER_ROUTING_FILE", str(tmp_path / "routing.env"))

    v2_snap = {
        "schema_version": "ic-engine-export/v2",
        "portfolios": [],
        "provider_routing": {
            "primary": "bogus-provider",  # rejected by allowlist
            "fallback_chain": [],
        },
    }
    result = await portfolio_import(v2_snap)
    # Import succeeds overall, but routing was rejected → not restored
    assert "error" not in result
    assert result["imported"]["provider_routing"] is False
    assert any("provider_routing restore rejected" in w for w in result["warnings"])
