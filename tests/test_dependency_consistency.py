"""Guard against pyproject.toml ↔ requirements.txt drift.

`uv sync` reads pyproject.toml (canonical); `pip install -r requirements.txt`
is the legacy path some runtimes still use (zeroclaw SKILL.md, hermes
install.sh). They must resolve to the same dependency set.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

_EXTRA_RE = re.compile(r"\[[^\]]+\]$")
_DEPS_BLOCK_RE = re.compile(
    r"^dependencies\s*=\s*\[\s*\n(?P<body>.*?)^\]",
    re.DOTALL | re.MULTILINE,
)
_DEP_ITEM_RE = re.compile(r'"([^"]+)"')


def _normalize(spec: str) -> str:
    name = spec.split(";")[0].strip()
    name = re.split(r"[<>=!~ ]", name, maxsplit=1)[0]
    return _EXTRA_RE.sub("", name).lower().strip()


def _pyproject_deps() -> set[str]:
    text = PYPROJECT.read_text()
    match = _DEPS_BLOCK_RE.search(text)
    assert match, "pyproject.toml: could not locate [project] dependencies array"
    return {_normalize(s) for s in _DEP_ITEM_RE.findall(match.group("body"))}


def _requirements_deps() -> set[str]:
    deps: set[str] = set()
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            deps.add(_normalize(line))
    return deps


def test_pyproject_and_requirements_agree_on_package_set():
    py = _pyproject_deps()
    req = _requirements_deps()
    missing_from_requirements = py - req
    missing_from_pyproject = req - py
    assert not missing_from_requirements, (
        f"In pyproject.toml but not requirements.txt: {sorted(missing_from_requirements)}"
    )
    assert not missing_from_pyproject, (
        f"In requirements.txt but not pyproject.toml: {sorted(missing_from_pyproject)}"
    )
