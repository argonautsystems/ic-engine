# SPDX-License-Identifier: Apache-2.0
"""Tests for keys_backup.py — encrypted passphrase-protected backup of
/data/keys.env for cross-host migration (#96)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

from investorclaw_bridge.mcp.tools import keys_backup as kb_module  # noqa: E402
from investorclaw_bridge.mcp.tools.keys_backup import (  # noqa: E402
    _MAGIC_FOOTER,
    _MAGIC_HEADER,
    _MIN_PASSPHRASE_LEN,
    portfolio_keys_backup,
    portfolio_keys_backups_list,
    portfolio_keys_restore,
)


_GOOD_PASSPHRASE = "correct horse battery staple"  # 28 chars; >= 12 floor.


@pytest.fixture
def keys_setup(tmp_path, monkeypatch):
    """Set up an isolated /data layout in tmp_path and return paths."""
    data = tmp_path / "data"
    data.mkdir()
    keys = data / "keys.env"
    keys.write_text(
        "# InvestorClaw API keys\n"
        "TOGETHER_API_KEY=tk-test-1234567890abcdef\n"
        "FRED_API_KEY=fred-secret-zzz-9999\n"
        "FINNHUB_KEY=finnhub-tttt\n"
    )
    keys.chmod(0o600)
    backups = data / "backups"
    monkeypatch.setenv("IC_KEYS_FILE", str(keys))
    monkeypatch.setenv("IC_KEYS_BACKUP_DIR", str(backups))
    return {"data": data, "keys": keys, "backups": backups}


# ── Backup creation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backup_rejects_short_passphrase(keys_setup):
    result = await portfolio_keys_backup(passphrase="short")
    assert result.get("error") == "passphrase_too_short"
    assert result.get("min_length") == _MIN_PASSPHRASE_LEN


@pytest.mark.asyncio
async def test_backup_rejects_missing_keys_file(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_KEYS_FILE", str(tmp_path / "no-keys.env"))
    monkeypatch.setenv("IC_KEYS_BACKUP_DIR", str(tmp_path / "backups"))
    result = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE)
    assert result.get("error") == "no_keys_file"


@pytest.mark.asyncio
async def test_backup_writes_armored_file(keys_setup):
    result = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE)
    assert "error" not in result
    path = Path(result["path"])
    assert path.parent == keys_setup["backups"]
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    content = path.read_text()
    assert content.startswith(_MAGIC_HEADER)
    assert _MAGIC_FOOTER in content


@pytest.mark.asyncio
async def test_backup_filename_uses_label(keys_setup):
    result = await portfolio_keys_backup(
        passphrase=_GOOD_PASSPHRASE, label="pre-upgrade",
    )
    assert "pre-upgrade" in result["filename"]


@pytest.mark.asyncio
async def test_backup_warns_on_permissive_mode_but_proceeds(keys_setup):
    """v4.3.2 — keys.env at mode 0644 still backs up, but the result
    surfaces a chmod-600 warning so the operator can fix the underlying
    file. Refusing here would leave them stranded with an
    already-too-permissive file."""
    keys_setup["keys"].chmod(0o644)
    result = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE)
    assert "error" not in result, "permissive mode must NOT block backup"
    warnings = result.get("warnings") or []
    assert warnings, "permissive mode must surface a warning"
    msg = warnings[0].lower()
    assert "0o644" in warnings[0] or "mode" in msg
    assert "0600" in warnings[0]
    # The encrypted blob still made it to disk.
    path = Path(result["path"])
    assert path.exists()


@pytest.mark.asyncio
async def test_backup_no_warning_when_mode_is_0600(keys_setup):
    """The 0600 happy path produces no warnings list entry."""
    keys_setup["keys"].chmod(0o600)
    result = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE)
    assert "error" not in result
    assert result.get("warnings") == []


@pytest.mark.asyncio
async def test_backup_label_sanitized(keys_setup):
    """Label sanitization strips path separators + shell metacharacters."""
    result = await portfolio_keys_backup(
        passphrase=_GOOD_PASSPHRASE,
        label="../../etc/passwd; rm -rf /",
    )
    assert "/" not in result["filename"]
    assert ";" not in result["filename"]
    assert "rm" not in result["filename"] or " " not in result["filename"]
    # Filename still ends in .bak under the backups dir
    Path(result["path"]).read_text()  # confirms the file exists at the expected path


@pytest.mark.asyncio
async def test_backup_returns_key_names_not_values(keys_setup):
    """Critical security invariant: backup tool never returns key values."""
    result = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE)
    assert sorted(result["key_names"]) == sorted([
        "TOGETHER_API_KEY", "FRED_API_KEY", "FINNHUB_KEY",
    ])
    serialized = json.dumps(result)
    assert "tk-test" not in serialized, "key value leaked"
    assert "fred-secret" not in serialized
    assert "finnhub-tttt" not in serialized


@pytest.mark.asyncio
async def test_backup_contents_are_actually_encrypted(keys_setup):
    """The armored file must NOT contain plaintext key values, even if
    the format is parsed naively."""
    result = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE)
    raw = Path(result["path"]).read_text()
    for secret in ("tk-test-1234567890abcdef", "fred-secret-zzz-9999",
                   "finnhub-tttt"):
        assert secret not in raw, f"plaintext secret leaked into backup: {secret}"


# ── Restore ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_round_trip(keys_setup):
    """Backup → wipe keys.env → restore → keys are byte-identical."""
    original = keys_setup["keys"].read_bytes()
    result = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE)
    backup_path = result["path"]

    # Wipe the keys file (simulate cross-host migration where new host
    # has no keys.env yet).
    keys_setup["keys"].unlink()
    assert not keys_setup["keys"].exists()

    rr = await portfolio_keys_restore(
        passphrase=_GOOD_PASSPHRASE, backup_path=backup_path,
    )
    assert rr.get("restored") is True
    assert keys_setup["keys"].exists()
    assert keys_setup["keys"].read_bytes() == original
    assert sorted(rr["key_names"]) == sorted([
        "TOGETHER_API_KEY", "FRED_API_KEY", "FINNHUB_KEY",
    ])


@pytest.mark.asyncio
async def test_restore_rejects_wrong_passphrase(keys_setup):
    result = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE)
    keys_setup["keys"].unlink()
    rr = await portfolio_keys_restore(
        passphrase="wrong passphrase ohno",
        backup_path=result["path"],
    )
    assert rr.get("error") == "decryption_failed"
    assert not keys_setup["keys"].exists(), "wrong-passphrase restore wrote keys.env"


@pytest.mark.asyncio
async def test_restore_auto_picks_most_recent_when_no_path(keys_setup):
    import time
    r1 = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE, label="older")
    time.sleep(0.05)
    # Modify keys file so the second backup is distinguishable
    keys_setup["keys"].write_text(
        "TOGETHER_API_KEY=newer-token\nFRED_API_KEY=newer-fred\n"
    )
    r2 = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE, label="newer")
    # Force `newer` to have a strictly later mtime
    import os as _os
    _os.utime(r1["path"], (1000, 1000))
    _os.utime(r2["path"], (2000, 2000))
    keys_setup["keys"].unlink()

    rr = await portfolio_keys_restore(passphrase=_GOOD_PASSPHRASE)
    assert rr["restored"] is True
    assert rr["path"] == r2["path"]
    # Restored keys file matches the "newer" snapshot
    text = keys_setup["keys"].read_text()
    assert "newer-token" in text


@pytest.mark.asyncio
async def test_restore_rejects_path_traversal(keys_setup, tmp_path):
    """Absolute paths outside the backups dir must be rejected."""
    # Create a fake "backup" outside the backups dir
    evil = tmp_path / "evil.bak"
    evil.write_text("not a real backup")
    rr = await portfolio_keys_restore(
        passphrase=_GOOD_PASSPHRASE, backup_path=str(evil),
    )
    assert rr.get("error") == "path_outside_backups_dir"


@pytest.mark.asyncio
async def test_restore_rejects_missing_file(keys_setup):
    rr = await portfolio_keys_restore(
        passphrase=_GOOD_PASSPHRASE, backup_path="nonexistent.bak",
    )
    assert rr.get("error") == "backup_not_found"


@pytest.mark.asyncio
async def test_restore_rejects_corrupt_blob(keys_setup):
    backups = keys_setup["backups"]
    backups.mkdir(parents=True, exist_ok=True)
    bad = backups / "keys-corrupt.bak"
    bad.write_text(
        f"{_MAGIC_HEADER}\nVersion: 1\nKDF: scrypt-N32768-r8-p1\nCipher: AES-GCM\n"
        f"\nBOGUS BASE64\n{_MAGIC_FOOTER}\n"
    )
    rr = await portfolio_keys_restore(
        passphrase=_GOOD_PASSPHRASE, backup_path="keys-corrupt.bak",
    )
    # Either bad_base64 or blob_too_short depending on what bytes b64decode
    # extracts; both are valid rejections.
    assert rr.get("error") in {"bad_base64", "blob_too_short", "decryption_failed"}


@pytest.mark.asyncio
async def test_restore_rejects_format_without_magic(keys_setup):
    backups = keys_setup["backups"]
    backups.mkdir(parents=True, exist_ok=True)
    bad = backups / "keys-not-armored.bak"
    bad.write_text("just some random content with no magic")
    rr = await portfolio_keys_restore(
        passphrase=_GOOD_PASSPHRASE, backup_path="keys-not-armored.bak",
    )
    assert rr.get("error") == "bad_format"


@pytest.mark.asyncio
async def test_restore_mirrors_into_environ(keys_setup, monkeypatch):
    """After restore, os.environ has the new keys so subprocesses pick
    them up without restart."""
    import os
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    result = await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE)
    keys_setup["keys"].unlink()
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)

    await portfolio_keys_restore(
        passphrase=_GOOD_PASSPHRASE, backup_path=result["path"],
    )
    assert os.environ.get("TOGETHER_API_KEY") == "tk-test-1234567890abcdef"
    assert os.environ.get("FRED_API_KEY") == "fred-secret-zzz-9999"


# ── Listing ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backups_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("IC_KEYS_BACKUP_DIR", str(tmp_path / "no-backups"))
    rr = await portfolio_keys_backups_list()
    assert rr["backups"] == []


@pytest.mark.asyncio
async def test_backups_list_returns_metadata_no_decrypt(keys_setup):
    """List enumerates backups without needing the passphrase."""
    await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE, label="A")
    await portfolio_keys_backup(passphrase="another-passphrase-XYZ", label="B")
    rr = await portfolio_keys_backups_list()
    assert len(rr["backups"]) == 2
    for entry in rr["backups"]:
        assert entry["filename"].startswith("keys-")
        assert entry["filename"].endswith(".bak")
        assert entry["kdf"].startswith("scrypt")
        # No decrypted content; no key_names in list response
        assert "key_names" not in entry


@pytest.mark.asyncio
async def test_backups_list_does_not_require_passphrase(keys_setup):
    """The passphrase that decrypts must NOT be needed to enumerate
    backups (so users can pick which one to restore)."""
    await portfolio_keys_backup(passphrase=_GOOD_PASSPHRASE, label="x")
    rr = await portfolio_keys_backups_list()
    assert len(rr["backups"]) == 1
    assert "passphrase" not in rr  # signature confirms no passphrase required
