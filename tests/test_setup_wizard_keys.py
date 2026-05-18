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
Smoke tests for setup/setup_wizard.py _detect_existing_keys (v2.2 step 5b).

Verifies that the migration-detection pass correctly reads an existing .env
file and maps provider variables to provider keys so the wizard can skip
prompts for already-configured providers.
"""

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from ic_engine.setup.setup_wizard import SetupWizard


class TestDetectExistingKeys:
    """Unit tests for SetupWizard._detect_existing_keys."""

    def test_returns_empty_when_file_missing(self, tmp_path):
        """Missing .env file returns an empty dict (no crash)."""
        result = SetupWizard._detect_existing_keys(tmp_path / "nonexistent.env")
        assert result == {}

    def test_detects_finnhub_key(self, tmp_path):
        """FINNHUB_API_KEY in .env maps to provider key 'finnhub'."""
        env_file = tmp_path / ".env"
        env_file.write_text("FINNHUB_API_KEY=test_finnhub_123\n", encoding="utf-8")
        result = SetupWizard._detect_existing_keys(env_file)
        assert "finnhub" in result
        assert result["finnhub"] == "test_finnhub_123"

    def test_detects_newsapi_key(self, tmp_path):
        """NEWSAPI_KEY maps to provider key 'newsapi'."""
        env_file = tmp_path / ".env"
        env_file.write_text("NEWSAPI_KEY=test_news_abc\n", encoding="utf-8")
        result = SetupWizard._detect_existing_keys(env_file)
        assert "newsapi" in result
        assert result["newsapi"] == "test_news_abc"

    def test_detects_fred_key(self, tmp_path):
        """FRED_API_KEY maps to provider key 'fred'."""
        env_file = tmp_path / ".env"
        env_file.write_text("FRED_API_KEY=test_fred_xyz\n", encoding="utf-8")
        result = SetupWizard._detect_existing_keys(env_file)
        assert "fred" in result
        assert result["fred"] == "test_fred_xyz"

    def test_detects_cryptopanic_key(self, tmp_path):
        """CRYPTOPANIC_API_KEY maps to provider key 'cryptopanic'."""
        env_file = tmp_path / ".env"
        env_file.write_text("CRYPTOPANIC_API_KEY=test_cp_key\n", encoding="utf-8")
        result = SetupWizard._detect_existing_keys(env_file)
        assert "cryptopanic" in result
        assert result["cryptopanic"] == "test_cp_key"

    def test_detects_multiple_keys(self, tmp_path):
        """Multiple keys in .env all detected correctly."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "FINNHUB_API_KEY=fh_key\n"
            "NEWSAPI_KEY=na_key\n"
            "FRED_API_KEY=fred_key\n"
            "POLYGON_API_KEY=poly_key\n",
            encoding="utf-8",
        )
        result = SetupWizard._detect_existing_keys(env_file)
        assert result.get("finnhub") == "fh_key"
        assert result.get("newsapi") == "na_key"
        assert result.get("fred") == "fred_key"
        assert result.get("massive") == "poly_key"

    def test_ignores_comment_lines(self, tmp_path):
        """Comment lines (# ...) are not interpreted as keys."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# FINNHUB_API_KEY=should_be_ignored\nNEWSAPI_KEY=real_key\n",
            encoding="utf-8",
        )
        result = SetupWizard._detect_existing_keys(env_file)
        assert "finnhub" not in result
        assert result.get("newsapi") == "real_key"

    def test_ignores_empty_values(self, tmp_path):
        """Keys with empty values are not reported as configured."""
        env_file = tmp_path / ".env"
        env_file.write_text("FINNHUB_API_KEY=\n", encoding="utf-8")
        result = SetupWizard._detect_existing_keys(env_file)
        assert "finnhub" not in result

    def test_alternate_finnhub_var_name(self, tmp_path):
        """FINNHUB_KEY (alternate var name) also maps to 'finnhub'."""
        env_file = tmp_path / ".env"
        env_file.write_text("FINNHUB_KEY=alt_fh_key\n", encoding="utf-8")
        result = SetupWizard._detect_existing_keys(env_file)
        assert "finnhub" in result
        assert result["finnhub"] == "alt_fh_key"
