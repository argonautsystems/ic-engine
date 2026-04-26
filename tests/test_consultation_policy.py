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
Unit tests for consultation_policy.py.

Tests the consultation decision logic without requiring an active Ollama
endpoint or any file-system state.
"""

import sys
from pathlib import Path

# Ensure the skill root is importable when pytest is run from any cwd
_skill_root = Path(__file__).parent.parent
if str(_skill_root) not in sys.path:
    sys.path.insert(0, str(_skill_root))

import pytest
from services.consultation_policy import (
    CONSULTATION_SYMBOL_LIMIT,
    get_consultation_endpoint,
    get_consultation_limit,
    get_consultation_model,
    is_consultation_enabled,
    should_inject_tier3,
)

# ---------------------------------------------------------------------------
# is_consultation_enabled
# ---------------------------------------------------------------------------


def test_enabled_default_is_false(monkeypatch):
    monkeypatch.delenv("INVESTORCLAW_CONSULTATION_ENABLED", raising=False)
    assert is_consultation_enabled() is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE"])
def test_enabled_when_set_true(monkeypatch, value):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_ENABLED", value)
    assert is_consultation_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "yes", "1", ""])
def test_not_enabled_for_non_true_values(monkeypatch, value):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_ENABLED", value)
    assert is_consultation_enabled() is False


# ---------------------------------------------------------------------------
# should_inject_tier3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["analyst", "analysts", "ratings"])
def test_tier3_injected_for_consultation_commands_when_enabled(monkeypatch, command):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_ENABLED", "true")
    assert should_inject_tier3(command) is True


@pytest.mark.parametrize("command", ["analyst", "analysts", "ratings"])
def test_tier3_not_injected_when_disabled(monkeypatch, command):
    monkeypatch.delenv("INVESTORCLAW_CONSULTATION_ENABLED", raising=False)
    # Also stub the endpoint probe — should_inject_tier3 falls back to a live
    # reachability check when the env var is unset, so tests that just clear
    # the env var fail on hosts that CAN reach a consultation endpoint
    # (e.g. WSL on TYPHON sees PYTHIA at 192.168.207.67:5002). Mock the probe
    # so the test asserts the env-var path in isolation.
    from services import consultation_policy

    monkeypatch.setattr(consultation_policy, "_probe_endpoint", lambda *a, **kw: False)
    assert should_inject_tier3(command) is False


@pytest.mark.parametrize("command", ["news", "holdings", "performance", "bonds", "setup"])
def test_tier3_not_injected_for_non_consultation_commands(monkeypatch, command):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_ENABLED", "true")
    assert should_inject_tier3(command) is False


# ---------------------------------------------------------------------------
# get_consultation_limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["analyst", "analysts", "ratings"])
def test_consultation_limit_for_tier3_commands(command):
    assert get_consultation_limit(command) == CONSULTATION_SYMBOL_LIMIT


@pytest.mark.parametrize("command", ["news", "holdings", "bonds", "setup"])
def test_consultation_limit_zero_for_non_tier3(command):
    assert get_consultation_limit(command) == 0


# ---------------------------------------------------------------------------
# Endpoint and model defaults / overrides
# ---------------------------------------------------------------------------


def test_endpoint_default(monkeypatch):
    monkeypatch.delenv("INVESTORCLAW_CONSULTATION_ENDPOINT", raising=False)
    assert get_consultation_endpoint() == "http://localhost:11434"


def test_endpoint_override(monkeypatch):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_ENDPOINT", "http://192.168.1.50:11434/")
    # Trailing slash should be stripped
    assert not get_consultation_endpoint().endswith("/")


def test_model_default(monkeypatch):
    monkeypatch.delenv("INVESTORCLAW_CONSULTATION_MODEL", raising=False)
    assert get_consultation_model() == "gemma4-consult"


def test_model_override(monkeypatch):
    monkeypatch.setenv("INVESTORCLAW_CONSULTATION_MODEL", "nemotron-super-49b")
    assert get_consultation_model() == "nemotron-super-49b"
