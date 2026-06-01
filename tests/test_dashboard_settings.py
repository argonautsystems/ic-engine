# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for dashboard config snapshot import/export endpoints."""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))

from investorclaw_bridge import dashboard  # noqa: E402


def _client(*, export_config=None, import_config=None) -> TestClient:
    app = FastAPI()
    dashboard.attach_to(
        app,
        get_init_state=lambda: {"ready": False},
        get_keys_status=lambda: {"configured": [], "settable": []},
        set_key=lambda name, value: {"saved": True},
        export_config=export_config or (lambda: _snapshot()),
        import_config=import_config or (lambda snapshot: {"imported": {}}),
    )
    return TestClient(app)


def _snapshot() -> dict:
    return {
        "schema_version": "ic-engine-export/v2",
        "engine_version": "v4.3.0\r\nbad",
        "portfolios": [],
        "stonkmode_state": None,
        "configured_keys": [],
        "provider_routing": None,
        "warnings": [],
    }


def _upload_response(
    client: TestClient,
    body: bytes,
    *,
    origin: str | None = "http://testserver",
) -> object:
    headers = {"Origin": origin} if origin is not None else {}
    return client.post(
        "/dashboard/settings/import_config",
        files={"snapshot_file": ("snapshot.json", body, "application/json")},
        headers=headers,
        follow_redirects=False,
    )


def _location_message(response) -> str:
    location = response.headers["location"]
    assert "/dashboard/settings?message=" in location
    return unquote(location.split("message=", 1)[1].replace("+", " "))


def test_settings_export_config_returns_snapshot_download():
    client = _client()

    response = client.get("/dashboard/settings/export.json")

    assert response.status_code == 200
    assert response.json()["schema_version"] == "ic-engine-export/v2"
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert "\r" not in disposition
    assert "\n" not in disposition


def test_settings_import_config_rejects_missing_csrf_origin_or_referer():
    client = _client()

    response = _upload_response(client, b"{}", origin=None)

    assert response.status_code == 303
    assert "Rejected cross-origin dashboard POST" in _location_message(response)


def test_settings_import_config_accepts_valid_origin_matching_host():
    seen: list[dict] = []

    def import_config(snapshot: dict) -> dict:
        seen.append(snapshot)
        return {"imported": {"portfolios": 0, "stonkmode": False}}

    client = _client(import_config=import_config)

    response = _upload_response(client, b'{"schema_version":"ic-engine-export/v2"}')

    assert response.status_code == 303
    assert seen == [{"schema_version": "ic-engine-export/v2"}]
    assert "Snapshot imported" in _location_message(response)


def test_settings_import_config_malformed_json_redirects_with_error():
    client = _client()

    response = _upload_response(client, b"{not-json")

    assert response.status_code == 303
    message = _location_message(response)
    assert "Snapshot JSON parse failed" in message


def test_settings_import_config_oversize_body_redirects_with_error(monkeypatch):
    monkeypatch.setattr(dashboard, "_MAX_UPLOAD_BYTES", 8)
    client = _client()

    response = _upload_response(client, b"012345678")

    assert response.status_code == 303
    assert "Snapshot too large" in _location_message(response)
