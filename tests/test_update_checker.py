#!/usr/bin/env python3
# Copyright 2026 InvestorClaw Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for setup.update_checker — runtime detection + message composition.

Hermetic: no real HTTP, no real filesystem writes outside tmp_path.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ic_engine.setup.update_checker import (
    RuntimeContext,
    _is_newer,
    backup_env,
    compose_check_failed,
    compose_message,
    compose_up_to_date,
    detect_runtime,
    fetch_latest_version,
)

# ─────────────────────────────────────────────────────────────────────────
# Version comparison
# ─────────────────────────────────────────────────────────────────────────


class IsNewerTests(unittest.TestCase):
    def test_newer_patch(self):
        self.assertTrue(_is_newer("2.1.1", "2.1.0"))

    def test_newer_minor(self):
        self.assertTrue(_is_newer("2.2.0", "2.1.0"))

    def test_newer_major(self):
        self.assertTrue(_is_newer("3.0.0", "2.1.0"))

    def test_same(self):
        self.assertFalse(_is_newer("2.1.0", "2.1.0"))

    def test_older(self):
        self.assertFalse(_is_newer("2.0.0", "2.1.0"))


# ─────────────────────────────────────────────────────────────────────────
# Runtime detection
# ─────────────────────────────────────────────────────────────────────────


class DetectRuntimeTests(unittest.TestCase):
    def test_claude_code_plugin(self):
        r = detect_runtime(Path.home() / ".claude" / "plugins" / "investorclaw" / "investorclaw")
        self.assertEqual(r.cluster, "claude-code")
        self.assertEqual(r.runtime_name, "Claude Code plugin")
        self.assertEqual(r.update_command, "/plugin update investorclaw@investorclaw")
        self.assertIsNone(r.installer_url)

    def test_openclaw_skill(self):
        r = detect_runtime(Path.home() / ".openclaw" / "workspace" / "skills" / "investorclaw")
        self.assertEqual(r.cluster, "reinstall")
        self.assertIn("Openclaw", r.runtime_name)
        self.assertIn("openclaw/install.sh", r.installer_url)
        self.assertIsNone(r.update_command)

    def test_zeroclaw_skill(self):
        r = detect_runtime(
            Path.home() / ".zeroclaw" / "workspace" / "skills" / "investorclaw-pruned"
        )
        self.assertEqual(r.cluster, "reinstall")
        self.assertIn("Zeroclaw", r.runtime_name)
        self.assertIn("zeroclaw/install.sh", r.installer_url)

    def test_hermes_skill_under_skills_dir(self):
        r = detect_runtime(Path.home() / ".hermes" / "skills" / "investorclaw")
        self.assertEqual(r.cluster, "reinstall")
        self.assertIn("Hermes", r.runtime_name)
        self.assertIn("hermes/install.sh", r.installer_url)

    def test_standalone_git_clone(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / ".git").mkdir()
            r = detect_runtime(td_path)
        self.assertEqual(r.cluster, "git")
        self.assertIn("git", r.runtime_name)
        self.assertIsNone(r.update_command)
        self.assertIsNone(r.installer_url)

    def test_unknown_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            r = detect_runtime(Path(td))
        self.assertEqual(r.cluster, "unknown")


# ─────────────────────────────────────────────────────────────────────────
# .env backup
# ─────────────────────────────────────────────────────────────────────────


class BackupEnvTests(unittest.TestCase):
    def test_backup_created_when_env_present(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env = td_path / ".env"
            env.write_text("FRED_API_KEY=secret-fred\nFINNHUB_KEY=secret-finnhub\n")

            with tempfile.TemporaryDirectory() as backup_home:
                with patch(
                    "ic_engine.setup.update_checker.BACKUP_DIR",
                    Path(backup_home) / ".investorclaw-backups",
                ):
                    result = backup_env(td_path)

                self.assertIsNotNone(result)
                self.assertTrue(result.exists())
                self.assertEqual(result.read_text(), env.read_text())

    def test_backup_returns_none_when_env_missing(self):
        with tempfile.TemporaryDirectory() as td:
            result = backup_env(Path(td))
        self.assertIsNone(result)

    def test_backup_file_mode_restricted(self):
        """Backup must be chmod 600 — contains API keys."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / ".env").write_text("SECRET=yes")
            with tempfile.TemporaryDirectory() as backup_home:
                with patch(
                    "ic_engine.setup.update_checker.BACKUP_DIR",
                    Path(backup_home) / ".investorclaw-backups",
                ):
                    result = backup_env(td_path)

                # Mode bits: last three octal digits are user/group/other
                mode = result.stat().st_mode & 0o777
                self.assertEqual(mode, 0o600)


# ─────────────────────────────────────────────────────────────────────────
# Message composition
# ─────────────────────────────────────────────────────────────────────────


class ComposeMessageTests(unittest.TestCase):
    def _ctx(self, cluster: str, runtime_name: str = "test rt", **kwargs) -> RuntimeContext:
        defaults = {
            "update_command": None,
            "installer_url": None,
            "install_path": Path("/tmp/investorclaw"),
        }
        defaults.update(kwargs)
        return RuntimeContext(cluster=cluster, runtime_name=runtime_name, **defaults)

    def test_claude_code_message_contains_slash_command(self):
        ctx = self._ctx(
            "claude-code",
            runtime_name="Claude Code plugin",
            update_command="/plugin update investorclaw@investorclaw",
        )
        msg = compose_message("2.1.0", "2.1.1", ctx, backup_path=Path("/bak/env.20260423"))
        self.assertIn("/plugin update investorclaw@investorclaw", msg)
        self.assertIn("2.1.1 available", msg)
        self.assertIn("you have 2.1.0", msg)
        self.assertIn("/bak/env.20260423", msg)  # backup referenced
        self.assertIn("Claude Code plugin", msg)

    def test_reinstall_message_contains_installer_and_restore(self):
        ctx = self._ctx(
            "reinstall",
            runtime_name="Openclaw skill",
            installer_url="https://gitlab.com/perlowja/InvestorClaw/-/raw/main/openclaw/install.sh",
            install_path=Path("/home/user/.openclaw/workspace/skills/investorclaw"),
        )
        backup = Path("/bak/env.20260423")
        msg = compose_message("2.1.0", "2.1.1", ctx, backup_path=backup)
        self.assertIn("delete investorclaw", msg.lower())
        self.assertIn(
            "curl -sSL https://gitlab.com/perlowja/InvestorClaw/-/raw/main/openclaw/install.sh",
            msg,
        )
        self.assertIn(f"cp {backup}", msg)
        self.assertIn(str(ctx.install_path), msg)

    def test_git_message_uses_git_pull(self):
        ctx = self._ctx(
            "git", runtime_name="standalone git clone", install_path=Path("/home/user/InvestorClaw")
        )
        msg = compose_message("2.1.0", "2.1.1", ctx, backup_path=None)
        self.assertIn("git pull origin main", msg)
        self.assertIn("uv sync", msg)
        self.assertIn(str(ctx.install_path), msg)

    def test_unknown_cluster_lists_all_options(self):
        ctx = self._ctx("unknown", runtime_name="unknown runtime")
        msg = compose_message("2.1.0", "2.1.1", ctx, backup_path=None)
        self.assertIn("openclaw/install.sh", msg)
        self.assertIn("zeroclaw/install.sh", msg)
        self.assertIn("hermes/install.sh", msg)
        self.assertIn("git pull", msg)

    def test_no_backup_omits_restore_lines(self):
        ctx = self._ctx(
            "claude-code",
            runtime_name="Claude Code plugin",
            update_command="/plugin update investorclaw@investorclaw",
        )
        msg = compose_message("2.1.0", "2.1.1", ctx, backup_path=None)
        self.assertNotIn("backed up", msg)
        self.assertNotIn("restore", msg.lower())

    def test_up_to_date_message_shape(self):
        msg = compose_up_to_date("2.1.0")
        self.assertIn("2.1.0", msg)
        self.assertIn("current", msg.lower())

    def test_check_failed_message_shape(self):
        msg = compose_check_failed("2.1.0")
        self.assertIn("2.1.0 installed", msg)
        self.assertIn("skipped", msg)


# ─────────────────────────────────────────────────────────────────────────
# Fetch (network-mocked)
# ─────────────────────────────────────────────────────────────────────────


class FetchLatestVersionTests(unittest.TestCase):
    def test_fetch_success(self):
        import json

        payload = json.dumps({"version": "2.1.1"}).encode()

        class _MockResp:
            def __init__(self, data):
                self._data = data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self._data

        with patch("urllib.request.urlopen", return_value=_MockResp(payload)):
            result = fetch_latest_version()
        self.assertEqual(result, "2.1.1")

    def test_fetch_returns_none_on_network_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("network unreachable")):
            result = fetch_latest_version()
        self.assertIsNone(result)

    def test_fetch_returns_none_on_malformed_json(self):
        class _BadResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"not json"

        with patch("urllib.request.urlopen", return_value=_BadResp()):
            result = fetch_latest_version()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
