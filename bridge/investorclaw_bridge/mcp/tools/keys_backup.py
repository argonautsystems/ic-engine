# SPDX-License-Identifier: Apache-2.0
"""Encrypted keys backup/restore for cross-host migration (#96).

Why this exists separately from `portfolio_export`:

  `portfolio_export` deliberately excludes key VALUES because exposing
  plaintext secrets through the agent's MCP/HTTP surface lets them flow
  into LLM provider context, conversation history, and tool-call logs.
  That posture is right for routine state snapshots.

  But the user still needs a way to MOVE keys between hosts (new
  appliance, hardware refresh, Disaster recovery from a backup file).
  This module provides that path with three security layers:

    1. The backup file is **encrypted with a user-supplied passphrase**
       (scrypt-N32768 KDF → AES-256-GCM). The plaintext keys never
       leave /data even briefly.
    2. The agent's tool surface only ever returns FILENAMES, key NAMES,
       checksums, and metadata — never values, never the passphrase.
       The passphrase passes through tool args (so it's still in the
       agent's context for that one call); SKILL.md recommends users
       invoke this from the dashboard browser when secrecy from the
       agent matters.
    3. Restore decrypts in-place inside the container; the decrypted
       keys land in /data/keys.env (mode 0600) without the values ever
       returning to the caller.

Format (armored ASCII for scp/email/clipboard friendliness):

    -----BEGIN IC-ENGINE KEYS BACKUP-----
    Version: 1
    KDF: scrypt-N32768-r8-p1
    Cipher: AES-GCM
    Created: 2026-05-07T12:34:56Z
    <blank line>
    <base64 of: salt(16) || nonce(12) || ciphertext+tag>
    -----END IC-ENGINE KEYS BACKUP-----

Threat model boundaries:

  - **In-scope**: protect against backup file leaking via untrusted
    transport (USB, cloud storage, email forwarding); protect against
    casual host filesystem snooping (file is mode 0600).
  - **Out-of-scope**: passphrase keylogging on the user's terminal;
    rubber-hose attack; weak passphrase guessing (we enforce min length
    12 to discourage trivial passphrases). scrypt parameters set so
    derivation takes ~70 ms on a 2020 laptop — interactive-friendly,
    moderately attack-resistant.
"""
from __future__ import annotations

import base64
import os
import re
import stat as _stat
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .._runtime import logger


# ── Crypto + format constants ─────────────────────────────────────────

_BACKUP_FORMAT_VERSION = 1
_SCRYPT_N = 32768   # 2^15 — ~70 ms on a 2020 laptop
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32       # AES-256
_NONCE_LEN = 12
_SALT_LEN = 16
_AAD = b"ic-engine-keys-backup-v1"

_MIN_PASSPHRASE_LEN = 12

_MAGIC_HEADER = "-----BEGIN IC-ENGINE KEYS BACKUP-----"
_MAGIC_FOOTER = "-----END IC-ENGINE KEYS BACKUP-----"

_SAFE_LABEL_RE = re.compile(r"[^a-zA-Z0-9_-]")


# ── Path helpers ─────────────────────────────────────────────────────


def _backups_dir() -> Path:
    """Where backup files live. Override via IC_KEYS_BACKUP_DIR."""
    return Path(os.environ.get("IC_KEYS_BACKUP_DIR", "/data/backups"))


def _keys_path() -> Path:
    return Path(os.environ.get("IC_KEYS_FILE", "/data/keys.env"))


def _build_filename(label: str = "") -> str:
    ts = time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())
    safe = _SAFE_LABEL_RE.sub("", label[:32]) if label else ""
    return f"keys-{ts}-{safe}.bak" if safe else f"keys-{ts}.bak"


def _key_names_from_text(text: str) -> list[str]:
    """Parse KEY=VALUE lines; return only the names (never values)."""
    names: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        names.append(line.split("=", 1)[0].strip())
    return names


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(
        salt=salt,
        length=_KEY_LEN,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
    )
    return kdf.derive(passphrase.encode("utf-8"))


# ── Tool handlers ─────────────────────────────────────────────────────


async def portfolio_keys_backup(passphrase: str, label: str = "") -> dict[str, Any]:
    """Encrypt /data/keys.env with a passphrase and write to /data/backups/.

    Returns metadata only — never key values. The agent surfaces the
    `path` to the user so they can scp it off-host for safekeeping.

    Args:
        passphrase: user-supplied encryption passphrase. Min 12 chars.
        label: optional short label appended to the filename
            (alphanumeric/underscore/hyphen, ≤32 chars).

    Returns:
        On success:
            {"path", "filename", "size_bytes", "key_names", "kdf"}
        On error:
            {"error": "...", ...}  — never raises.
    """
    if not isinstance(passphrase, str) or len(passphrase) < _MIN_PASSPHRASE_LEN:
        return {
            "error": "passphrase_too_short",
            "min_length": _MIN_PASSPHRASE_LEN,
            "detail": (
                f"Passphrase must be at least {_MIN_PASSPHRASE_LEN} characters. "
                "Use a passphrase you can remember — without it the backup is "
                "unrecoverable. Consider a 4-5 word diceware-style phrase."
            ),
        }

    keys_path = _keys_path()
    if not keys_path.exists():
        return {"error": "no_keys_file", "path": str(keys_path)}

    # Defense-in-depth: surface a warning if /data/keys.env is more
    # permissive than 0600. The backup proceeds (the operator explicitly
    # asked for it; refusing here would leave them stranded with an
    # already-too-permissive file), but the warning lands in structlog
    # AND the result dict so they can chmod 600 the file before the
    # next regenerate / agent action picks it up.
    try:
        mode = _stat.S_IMODE(keys_path.stat().st_mode)
    except OSError:
        mode = None
    mode_warning: str | None = None
    if mode is not None and (mode & 0o077):
        mode_warning = (
            f"/data/keys.env is mode {oct(mode)} — should be 0600 "
            f"(owner read/write only). Run: chmod 600 {keys_path}"
        )
        logger.warning(
            "keys_backup.permissive_mode",
            path=str(keys_path),
            mode=oct(mode),
        )

    plaintext = keys_path.read_bytes()
    if not plaintext.strip():
        return {"error": "keys_file_empty", "path": str(keys_path)}

    # Encrypt
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    derived = _derive_key(passphrase, salt)
    aesgcm = AESGCM(derived)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=_AAD)

    # Armored format — base64 wrapped at 76 chars per line
    body = base64.b64encode(salt + nonce + ciphertext).decode("ascii")
    body_lines = [body[i : i + 76] for i in range(0, len(body), 76)]
    armored = "\n".join([
        _MAGIC_HEADER,
        f"Version: {_BACKUP_FORMAT_VERSION}",
        f"KDF: scrypt-N{_SCRYPT_N}-r{_SCRYPT_R}-p{_SCRYPT_P}",
        "Cipher: AES-GCM",
        f"Created: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "",
        *body_lines,
        _MAGIC_FOOTER,
        "",
    ])

    # Write
    backups = _backups_dir()
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = _build_filename(label)
    target = backups / filename
    target.write_text(armored)
    target.chmod(0o600)

    names = _key_names_from_text(plaintext.decode("utf-8", errors="replace"))
    logger.info(
        "keys_backup",
        path=str(target),
        key_names=names,
        size_bytes=len(armored),
    )
    warnings: list[str] = []
    if mode_warning:
        warnings.append(mode_warning)
    return {
        "path": str(target),
        "filename": filename,
        "size_bytes": len(armored),
        "key_names": names,
        "kdf": f"scrypt-N{_SCRYPT_N}-r{_SCRYPT_R}-p{_SCRYPT_P}",
        "warnings": warnings,
        "next_steps": [
            (
                "Copy this file off-host with `scp` or equivalent. The "
                "encrypted blob is safe to transport over untrusted media; "
                "an attacker without the passphrase cannot recover keys."
            ),
            (
                "On the destination host, place the file under "
                f"{_backups_dir()} (or pass an explicit path) and call "
                "`portfolio_keys_restore(passphrase, backup_path)`."
            ),
            (
                "Save the passphrase somewhere durable (password manager, "
                "secure note). Without it the backup is permanently "
                "unrecoverable — this is by design."
            ),
        ],
    }


def _resolve_backup_path(backup_path: str) -> tuple[Path | None, str | None]:
    """Resolve a possibly-relative path under /data/backups, with safety
    checks. Returns (path, error). Either is None on the success/fail
    branch."""
    backups = _backups_dir()
    if not backup_path:
        if not backups.is_dir():
            return None, "no_backups_dir"
        candidates = sorted(
            backups.glob("keys-*.bak"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None, "no_backups_found"
        return candidates[0], None

    path = Path(backup_path)
    if not path.is_absolute():
        path = backups / path
    # Path containment — reject ../ traversal
    try:
        path.resolve().relative_to(backups.resolve())
    except ValueError:
        return None, "path_outside_backups_dir"
    if not path.exists():
        return None, "backup_not_found"
    return path, None


def _parse_armored(armored: str) -> dict[str, Any]:
    """Parse the armored body. Returns {salt, nonce, ciphertext, kdf,
    created} or {"error": "..."}."""
    if _MAGIC_HEADER not in armored or _MAGIC_FOOTER not in armored:
        return {"error": "bad_format"}

    lines = armored.splitlines()
    in_body = False
    metadata_done = False
    body_lines: list[str] = []
    metadata: dict[str, str] = {}

    for line in lines:
        if line == _MAGIC_HEADER:
            in_body = True
            continue
        if line == _MAGIC_FOOTER:
            break
        if not in_body:
            continue
        if not metadata_done:
            if line == "":
                metadata_done = True
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                metadata[k.strip()] = v.strip()
            continue
        body_lines.append(line)

    try:
        blob = base64.b64decode("".join(body_lines))
    except Exception as exc:  # noqa: BLE001
        return {"error": "bad_base64", "detail": str(exc)}

    if len(blob) < _SALT_LEN + _NONCE_LEN + 16:
        return {"error": "blob_too_short"}

    return {
        "salt": blob[:_SALT_LEN],
        "nonce": blob[_SALT_LEN : _SALT_LEN + _NONCE_LEN],
        "ciphertext": blob[_SALT_LEN + _NONCE_LEN :],
        "kdf": metadata.get("KDF", "unknown"),
        "created": metadata.get("Created", "unknown"),
        "version": metadata.get("Version", "unknown"),
    }


async def portfolio_keys_restore(
    passphrase: str, backup_path: str = ""
) -> dict[str, Any]:
    """Decrypt a backup and replace /data/keys.env.

    The decrypted plaintext NEVER returns to the caller; only the list
    of key NAMES + the source path. After a successful restore the
    bridge mirrors the new keys into os.environ so the next
    portfolio_ask sees them without restart.

    Args:
        passphrase: passphrase used at backup time. Same passphrase
            recovers the same backup; wrong passphrase → AES-GCM
            authentication fails → `decryption_failed` error.
        backup_path: optional. If omitted, the most-recently-modified
            keys-*.bak file under /data/backups/ is auto-selected.
            If relative, resolved under /data/backups/. Path traversal
            outside that dir is rejected.

    Returns:
        On success: {"restored": true, "path", "key_names", "kdf",
                     "created"}
        On error:   {"error": "...", ...}
    """
    if not isinstance(passphrase, str) or not passphrase:
        return {"error": "passphrase_required"}

    target, err = _resolve_backup_path(backup_path)
    if err is not None:
        return {"error": err, "backups_dir": str(_backups_dir())}
    assert target is not None

    armored = target.read_text(errors="replace")
    parsed = _parse_armored(armored)
    if "error" in parsed:
        return {"error": parsed["error"], "path": str(target),
                "detail": parsed.get("detail")}

    derived = _derive_key(passphrase, parsed["salt"])
    aesgcm = AESGCM(derived)
    try:
        plaintext = aesgcm.decrypt(
            parsed["nonce"], parsed["ciphertext"], associated_data=_AAD,
        )
    except InvalidTag:
        return {
            "error": "decryption_failed",
            "hint": "wrong passphrase or corrupt backup",
            "path": str(target),
        }

    text = plaintext.decode("utf-8", errors="replace")
    names = _key_names_from_text(text)
    if not names:
        return {
            "error": "decrypted_content_invalid",
            "hint": "no KEY=VALUE lines found in decrypted plaintext",
            "path": str(target),
        }

    # Write to /data/keys.env, mode 0600
    keys_path = _keys_path()
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    keys_path.write_bytes(plaintext)
    keys_path.chmod(0o600)

    # Mirror into env so subprocesses pick up
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

    logger.info(
        "keys_restore",
        path=str(target),
        key_names=names,
        kdf=parsed["kdf"],
    )
    return {
        "restored": True,
        "path": str(target),
        "key_names": names,
        "kdf": parsed["kdf"],
        "created": parsed["created"],
    }


async def portfolio_keys_backups_list() -> dict[str, Any]:
    """Enumerate available encrypted backups under /data/backups/.

    Returns metadata only (filename, size, mtime, KDF, created-from-
    header). Does NOT decrypt — listing requires no passphrase.
    """
    backups = _backups_dir()
    if not backups.is_dir():
        return {"backups": [], "backups_dir": str(backups)}

    items: list[dict[str, Any]] = []
    for p in sorted(
        backups.glob("keys-*.bak"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            stat = p.stat()
            armored = p.read_text(errors="replace")
            kdf = "unknown"
            created = "unknown"
            for line in armored.splitlines()[:10]:
                if line.startswith("KDF:"):
                    kdf = line.split(":", 1)[1].strip()
                elif line.startswith("Created:"):
                    # `Created: 2026-...` — we want everything after the FIRST colon,
                    # but the value contains colons too (timestamps). split only once.
                    created = line.split(":", 1)[1].strip()
            items.append({
                "path": str(p),
                "filename": p.name,
                "size_bytes": stat.st_size,
                "modified_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)
                ),
                "kdf": kdf,
                "created_meta": created,
            })
        except Exception as exc:
            items.append({
                "path": str(p),
                "error": f"{type(exc).__name__}: {exc}",
            })

    return {"backups": items, "backups_dir": str(backups)}


# ── Tool descriptors ──────────────────────────────────────────────────


def _tool(description: str, parameters: dict, required: list[str], handler) -> dict:
    return {
        "description": description,
        "parameters": parameters,
        "required": required,
        "handler": handler,
    }


KEYS_BACKUP_TOOLS: dict[str, dict[str, Any]] = {
    "portfolio_keys_backup": _tool(
        description=(
            "Create a passphrase-encrypted backup of the container's API "
            "key store (/data/keys.env). The backup uses scrypt KDF + "
            "AES-256-GCM and is written to /data/backups/keys-<ts>[-label]"
            ".bak as armored ASCII (safe to scp/email/clipboard). Returns "
            "filename + size + key NAMES + KDF — never key values. "
            "Recommended usage: invoke this immediately after configuring "
            "your API keys, save the file off-host (scp to a trusted "
            "location), and remember the passphrase. Without the "
            "passphrase the backup is permanently unrecoverable."
        ),
        parameters={
            "passphrase": {
                "type": "string",
                "description": (
                    "User-supplied encryption passphrase. Min 12 chars. "
                    "Use a passphrase you can remember and store durably "
                    "(password manager, secure note). Loss of passphrase "
                    "= loss of backup."
                ),
            },
            "label": {
                "type": "string",
                "description": (
                    "Optional short label appended to the filename "
                    "(alphanumeric/underscore/hyphen, max 32 chars). "
                    "E.g. 'pre-upgrade' or 'host-A'."
                ),
            },
        },
        required=["passphrase"],
        handler=portfolio_keys_backup,
    ),
    "portfolio_keys_restore": _tool(
        description=(
            "Decrypt a backup file produced by portfolio_keys_backup and "
            "replace /data/keys.env. Existing keys are overwritten. The "
            "decrypted plaintext NEVER returns to the caller — only the "
            "list of key NAMES restored. After a successful restore the "
            "bridge mirrors the new keys into os.environ so the next "
            "portfolio_ask sees them without restart. Use this on the "
            "destination host during cross-host migration: scp the .bak "
            "file into /data/backups/ on the new host, then call this "
            "tool with the same passphrase used at backup time."
        ),
        parameters={
            "passphrase": {
                "type": "string",
                "description": (
                    "Same passphrase used at backup time. Wrong "
                    "passphrase → `decryption_failed` (AES-GCM auth tag)."
                ),
            },
            "backup_path": {
                "type": "string",
                "description": (
                    "Optional path to the .bak file. If omitted, the "
                    "most-recently-modified keys-*.bak under "
                    "/data/backups/ is auto-selected. Relative paths "
                    "resolve under /data/backups/; traversal outside is "
                    "rejected."
                ),
            },
        },
        required=["passphrase"],
        handler=portfolio_keys_restore,
    ),
    "portfolio_keys_backups_list": _tool(
        description=(
            "Enumerate available encrypted backups under /data/backups/. "
            "Returns filename + size + mtime + KDF + created timestamp "
            "for each. Does NOT decrypt — listing requires no "
            "passphrase. Use this to let the user choose which backup "
            "to restore."
        ),
        parameters={},
        required=[],
        handler=portfolio_keys_backups_list,
    ),
}
