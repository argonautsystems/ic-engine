# SPDX-License-Identifier: Apache-2.0
"""Tests for portfolio_templates — starter portfolio CSV writer."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))

from investorclaw_bridge.portfolio_templates import (  # noqa: E402
    PORTFOLIO_TEMPLATES,
    apply_template,
    list_templates,
)
from investorclaw_bridge.dashboard import _settings_tab  # noqa: E402


VALID_ASSET_TYPES = {"equity", "bond", "cash", "derivative"}
ADVERSARIAL_JS_NAME = "single' double\" backslash\\ </script> line\nnext"


def _assert_adversarial_name_js_escaped(html: str) -> None:
    assert "</script>" not in html
    assert 'double"' not in html
    assert "backslash\\\\ </script>" not in html
    assert "backslash\\\\ &lt;/script&gt;" in html
    assert "double\\&quot;" in html


@pytest.mark.parametrize("template", PORTFOLIO_TEMPLATES, ids=lambda t: t["slug"])
def test_template_definition_well_formed(template):
    """Each registered template has the required fields + valid row shape."""
    assert template["slug"]
    assert "-" in template["slug"] or template["slug"].isalpha()
    assert template["slug"].islower()
    assert template["name"]
    assert template["description"]
    assert template["rationale"]
    assert template["rows"], "templates must have at least one row"
    for row in template["rows"]:
        assert set(row.keys()) == {"symbol", "shares", "price", "asset_type"}
        assert row["symbol"]
        assert isinstance(row["shares"], (int, float))
        assert row["shares"] > 0
        assert isinstance(row["price"], (int, float))
        assert row["price"] > 0
        assert row["asset_type"] in VALID_ASSET_TYPES


def test_template_slugs_are_unique():
    slugs = [t["slug"] for t in PORTFOLIO_TEMPLATES]
    assert len(slugs) == len(set(slugs)), f"duplicate template slugs: {slugs}"


def test_list_templates_returns_metadata_only():
    out = list_templates()
    assert len(out) == len(PORTFOLIO_TEMPLATES)
    for t in out:
        # Metadata fields present
        assert "slug" in t
        assert "name" in t
        assert "description" in t
        assert "rationale" in t
        assert "positions" in t
        assert "notional" in t
        assert "row_count" in t
        # Detail rows NOT included (would bloat the response)
        assert "rows" not in t


def test_list_templates_notional_sane():
    """Each template's notional value should be roughly $100k."""
    for t in list_templates():
        assert 50_000 < t["notional"] < 200_000, (
            f"{t['slug']} notional={t['notional']} outside $50k–$200k starter range"
        )


def test_apply_template_writes_csv_with_canonical_headers(tmp_path):
    """apply_template writes a CSV that matches ic-engine's expected header format."""
    result = apply_template("boglehead-3fund", portfolio_dir=tmp_path)
    assert result["applied"] is True
    assert result["filename"].endswith(".csv")
    assert result["filename"].startswith("template-")

    written = tmp_path / result["filename"]
    assert written.exists()

    with written.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == ["symbol", "shares", "price", "asset_type"]
        rows = list(reader)
    assert rows, "CSV must have at least one row"
    for row in rows:
        assert row["symbol"]
        # CSV writes everything as strings; verify they parse
        assert float(row["shares"]) > 0
        assert float(row["price"]) > 0
        assert row["asset_type"] in VALID_ASSET_TYPES


def test_settings_template_confirm_js_escapes_apostrophe_name():
    html = _settings_tab(
        lambda: {"configured": [], "settable": []},
        templates=[
            {
                "slug": "retiree-choice",
                "name": "Retiree's Choice",
                "description": "Synthetic test template.",
                "rationale": "Regression fixture.",
                "positions": "VTI 1",
                "notional": 1,
            }
        ],
    )

    assert (
        "return confirm('Drop the ' + &quot;Retiree&#x27;s Choice&quot; + "
        "' template into /data/portfolios/ and queue regenerate?');"
    ) in html
    assert "Drop the Retiree&#x27;s Choice template" not in html


def test_settings_template_confirm_js_escapes_adversarial_name():
    html = _settings_tab(
        lambda: {"configured": [], "settable": []},
        templates=[
            {
                "slug": "adversarial-choice",
                "name": ADVERSARIAL_JS_NAME,
                "description": "Synthetic test template.",
                "rationale": "Regression fixture.",
                "positions": "VTI 1",
                "notional": 1,
            }
        ],
    )

    assert "Drop the ' + &quot;" in html
    _assert_adversarial_name_js_escaped(html)


def test_settings_key_delete_confirm_js_escapes_apostrophe_name():
    html = _settings_tab(
        lambda: {
            "configured": ["ODD'KEY"],
            "settable": ["ODD'KEY"],
        },
    )

    assert "return confirm('Delete ' + &quot;ODD&#x27;KEY&quot; + '?');" in html
    assert "return confirm('Delete ODD&#x27;KEY?');" not in html


def test_settings_key_delete_confirm_js_escapes_adversarial_name():
    html = _settings_tab(
        lambda: {
            "configured": [ADVERSARIAL_JS_NAME],
            "settable": [ADVERSARIAL_JS_NAME],
        },
    )

    assert "Delete ' + &quot;" in html
    _assert_adversarial_name_js_escaped(html)


def test_apply_unknown_template_returns_error(tmp_path):
    result = apply_template("nope-fake-slug", portfolio_dir=tmp_path)
    assert "error" in result
    assert result["error"] == "unknown_template"
    assert "available" in result


def test_apply_template_rejects_path_traversal(tmp_path):
    """A slug with a path separator must be rejected as unknown_template."""
    for evil in ("../etc/passwd", "../../escape", "spx-indexer/../../oops"):
        result = apply_template(evil, portfolio_dir=tmp_path)
        assert result.get("error") == "unknown_template", (
            f"path-traversal slug accepted: {evil!r}"
        )


def test_apply_template_rejects_empty_slug(tmp_path):
    for empty in ("", None, "   "):
        # Coerce None to empty string for the regex — caller would already
        # have stringified it, but cover it explicitly.
        slug = empty or ""
        result = apply_template(slug.strip() if slug else "", portfolio_dir=tmp_path)
        assert result.get("error") == "unknown_template"


def test_apply_template_filename_is_namespaced(tmp_path):
    """Template files use a 'template-<slug>.csv' name to avoid clobbering user uploads."""
    result = apply_template("spx-indexer", portfolio_dir=tmp_path)
    assert result["applied"] is True
    assert result["filename"] == "template-spx-indexer.csv"


def test_apply_template_overwrites_existing_template(tmp_path):
    """Re-applying the same template overwrites the prior file (not append)."""
    apply_template("spx-indexer", portfolio_dir=tmp_path)
    result = apply_template("spx-indexer", portfolio_dir=tmp_path)
    assert result["applied"] is True

    written = tmp_path / "template-spx-indexer.csv"
    with written.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Should have exactly one VOO row, not two from append.
    assert len(rows) == 1


def test_apply_template_leaves_no_tmp_file_after_success(tmp_path):
    result = apply_template("spx-indexer", portfolio_dir=tmp_path)

    assert result["applied"] is True
    assert not (tmp_path / "template-spx-indexer.csv.tmp").exists()


def test_apply_template_cleans_tmp_and_leaves_dest_absent_on_write_failure(
    tmp_path, monkeypatch
):
    from investorclaw_bridge import portfolio_templates

    class FailingDictWriter(csv.DictWriter):
        def writerow(self, rowdict):
            raise OSError("simulated partial write")

    monkeypatch.setattr(portfolio_templates.csv, "DictWriter", FailingDictWriter)

    result = apply_template("spx-indexer", portfolio_dir=tmp_path)

    assert result["error"] == "csv_write_failed"
    assert not (tmp_path / "template-spx-indexer.csv.tmp").exists()
    assert not (tmp_path / "template-spx-indexer.csv").exists()


def test_apply_template_preserves_existing_dest_on_write_failure(tmp_path, monkeypatch):
    from investorclaw_bridge import portfolio_templates

    dest = tmp_path / "template-spx-indexer.csv"
    original = "symbol,shares,price,asset_type\nCASH,1,1,cash\n"
    dest.write_text(original, encoding="utf-8")

    class FailingDictWriter(csv.DictWriter):
        def writerow(self, rowdict):
            raise OSError("simulated partial write")

    monkeypatch.setattr(portfolio_templates.csv, "DictWriter", FailingDictWriter)

    result = apply_template("spx-indexer", portfolio_dir=tmp_path)

    assert result["error"] == "csv_write_failed"
    assert not (tmp_path / "template-spx-indexer.csv.tmp").exists()
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == original


def test_apply_template_cleans_tmp_when_existing_dest_write_fails(tmp_path, monkeypatch):
    from investorclaw_bridge import portfolio_templates

    dest = tmp_path / "template-spx-indexer.csv"
    original = "symbol,shares,price,asset_type\nBND,2,73,bond\n"
    dest.write_text(original, encoding="utf-8")

    class FailingDictWriter(csv.DictWriter):
        def writerow(self, rowdict):
            raise OSError("simulated partial write")

    monkeypatch.setattr(portfolio_templates.csv, "DictWriter", FailingDictWriter)

    result = apply_template("spx-indexer", portfolio_dir=tmp_path)

    assert result["error"] == "csv_write_failed"
    assert not (tmp_path / "template-spx-indexer.csv.tmp").exists()
    assert dest.read_text(encoding="utf-8") == original


def test_apply_template_creates_dir_if_missing(tmp_path):
    """portfolio_dir gets mkdir -p semantics."""
    target = tmp_path / "fresh" / "portfolios"
    assert not target.exists()
    result = apply_template("sixty-forty", portfolio_dir=target)
    assert result["applied"] is True
    assert target.is_dir()
    assert (target / "template-sixty-forty.csv").exists()
