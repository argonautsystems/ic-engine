#!/usr/bin/env python3
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
rendering/artifact_generator.py — Reusable HTML artifact builder.

Provides a composable ArtifactGenerator class that commands can populate
with Plotly charts, HTML tables, stonkmode narrative blocks, and Dr. Stonk
term definitions, then render to a single self-contained HTML file.

Design notes:
    - Plotly is embedded via CDN (plotly-2.35.2.min.js) — no runtime dep.
    - Dark-mode CSS matches commands/dashboard.py color variables so the
      entire fleet of artifacts feels visually coherent.
    - Per-block content is escaped defensively; embedded JSON payloads are
      escaped against </script> breakouts.
    - All methods are additive; order of insertion = order of render.

Typical command usage:

    from ic_engine.rendering.artifact_generator import ArtifactGenerator

    artifact = ArtifactGenerator(
        title="Portfolio Holdings Analysis",
        disclaimer="EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        metadata={"As of": "2026-04-17", "Total": "$1,234,567"},
    )
    artifact.add_pie_chart(["Equity", "Bond"], [800000, 200000], "Allocation")
    artifact.add_table(rows, "Top Holdings", columns=["Symbol", "Value"])
    artifact.add_narrative_block("Commentary", lead_text, persona="Big Jim")
    artifact.add_dr_stonk_box({"Sharpe Ratio": "..."})
    artifact.save(Path(tempfile.gettempdir()) / "holdings.html")
"""

from __future__ import annotations

import html as _html_mod
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# Avatar asset resolution
# ---------------------------------------------------------------------------

# Avatars live at <project-root>/assets/stonkmode-avatars/ and <project-root>/docs/assets/stonkmode-characters/
# and are indexed by an optional manifest.json that maps canonical slugs ("blitz_thunderbuy")
# to display names, archetypes, and asset paths (SVG, PNG, or JPEG).
AVATAR_DIR = Path(__file__).resolve().parent.parent / "assets" / "stonkmode-avatars"
AVATAR_MANIFEST_PATH = AVATAR_DIR / "manifest.json"


def _load_avatar_manifest() -> Dict[str, Dict[str, Any]]:
    """Load the persona → avatar manifest once; return {} if unavailable."""
    try:
        with open(AVATAR_MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


# Module-level cache so we don't reparse the manifest per ArtifactGenerator.
_AVATAR_MANIFEST: Dict[str, Dict[str, Any]] = _load_avatar_manifest()


def _slugify_persona(name: str) -> str:
    """Collapse a display name to a canonical avatar slug.

    "Blitz Thunderbuy"                 → "blitz_thunderbuy"
    'Brick "Diamond Hands" Stonksworth'→ "brick_diamond_hands_stonksworth"
    "Prescott Pennington-Smythe"       → "prescott_pennington_smythe"
    "blitz_thunderbuy"                 → "blitz_thunderbuy"
    """
    if not name:
        return ""
    n = name.lower()
    # Strip anything that isn't alnum/space/underscore (quotes, commas, etc.)
    n = re.sub(r"[^a-z0-9 _\-]+", "", n)
    # Collapse hyphens and whitespace runs into single underscores
    n = re.sub(r"[\s\-]+", "_", n).strip("_")
    n = re.sub(r"_+", "_", n)
    return n


def _resolve_avatar_path(persona_name: str) -> Optional[Path]:
    """Return the avatar Path for a persona, or None if no asset is available.

    Resolution order:
        1. Exact manifest-slug key (e.g. "blitz_thunderbuy").
        2. Manifest entry whose "name" display string matches.
        3. Filesystem fallback: assets/stonkmode-avatars/<slug>.svg
        4. Filesystem fallback: assets/stonkmode-avatars/<slug>.png
        5. Filesystem fallback: assets/stonkmode-avatars/<slug>.jpg
    """
    if not persona_name:
        return None

    slug = _slugify_persona(persona_name)

    # (1) Direct manifest-slug hit
    entry = _AVATAR_MANIFEST.get(slug) if _AVATAR_MANIFEST else None

    # (2) Manifest display-name hit
    if entry is None and _AVATAR_MANIFEST:
        for k, v in _AVATAR_MANIFEST.items():
            if isinstance(v, dict) and _slugify_persona(v.get("name", "")) == slug:
                entry = v
                # Prefer the manifest's canonical slug for the filesystem lookup
                slug = k
                break

    # Manifest asset path (relative to project root)
    if isinstance(entry, dict) and entry.get("asset"):
        asset_path = Path(entry["asset"])
        if not asset_path.is_absolute():
            asset_path = AVATAR_DIR.parent.parent / asset_path
        if asset_path.exists():
            return asset_path

    # (3) Filesystem fallback SVG
    fallback_svg = AVATAR_DIR / f"{slug}.svg"
    if fallback_svg.exists():
        return fallback_svg

    # (4) Filesystem fallback PNG
    fallback_png = AVATAR_DIR / f"{slug}.png"
    if fallback_png.exists():
        return fallback_png

    # (5) Filesystem fallback JPEG
    fallback_jpg = AVATAR_DIR / f"{slug}.jpg"
    if fallback_jpg.exists():
        return fallback_jpg

    return None


def _prefix_svg_ids(svg: str, prefix: str) -> str:
    """Prefix every `id="..."` declaration AND its references with `prefix-`.

    Avatars share a small set of ids by convention (`title`, `desc`, `bg`,
    `shadow`, `avatarClip`, etc.) so that the source SVGs are simple and
    self-documenting. When two are inlined into the same HTML document
    (the lead/foil pair on `_render_persona_pair`, or any future
    multi-avatar layout), `url(#bg)` resolves to the first SVG's
    definition, leaving the second rendered with the wrong gradient or
    missing clip path. Prefixing all declarations + references with the
    per-persona slug eliminates the collision.

    Handles:
        * Declarations:        id="X"               -> id="prefix-X"
        * URL refs:            url(#X)              -> url(#prefix-X)
        * Anchor refs:         href="#X"            -> href="#prefix-X"
        * Old-style refs:      xlink:href="#X"      -> xlink:href="#prefix-X"
        * ARIA token lists:    aria-labelledby="A B C" -> "prefix-A prefix-B prefix-C"
                               (only the tokens that ARE declared ids)
    """
    if not prefix:
        return svg

    declared_ids = re.findall(r'\bid="([^"]+)"', svg)
    if not declared_ids:
        return svg

    declared_set = set(declared_ids)

    # Replace per-id occurrences. Sort longest-first so a longer id like
    # "avatarClip" isn't partially clobbered by the prefix-pass for "avatar".
    for orig_id in sorted(declared_set, key=len, reverse=True):
        new_id = f"{prefix}-{orig_id}"
        esc = re.escape(orig_id)
        svg = re.sub(rf'\bid="{esc}"', f'id="{new_id}"', svg)
        svg = re.sub(rf"url\(#{esc}\)", f"url(#{new_id})", svg)
        svg = re.sub(rf'href="#{esc}"', f'href="#{new_id}"', svg)
        svg = re.sub(rf'xlink:href="#{esc}"', f'xlink:href="#{new_id}"', svg)

    # ARIA references are space-separated token lists; replace each token
    # individually so unrelated tokens (if ever present) survive untouched.
    def _rewrite_aria(match: "re.Match[str]") -> str:
        attr_name = match.group(1)
        tokens = match.group(2).split()
        rewritten = [f"{prefix}-{t}" if t in declared_set else t for t in tokens]
        return f'{attr_name}="{" ".join(rewritten)}"'

    svg = re.sub(r'(aria-labelledby|aria-describedby)="([^"]+)"', _rewrite_aria, svg)

    return svg


def _load_avatar_image(persona_name: str) -> Optional[str]:
    """Return inlineable avatar HTML for a persona, or None if unavailable.

    SVG files: returned as inline markup with per-persona id prefixing
    (see `_prefix_svg_ids`) so multiple avatars in the same document don't
    collide on shared ids like `bg`, `shadow`, `avatarClip`.
    PNG/JPEG files: returned as <img> with GitHub CDN URL reference.
    """
    if not persona_name:
        return None

    slug = _slugify_persona(persona_name)
    safe_name = _html_mod.escape(persona_name or "")

    # Check manifest for asset path (returns GitHub-friendly URLs for PNG/JPEG)
    entry = _AVATAR_MANIFEST.get(slug) if _AVATAR_MANIFEST else None
    if entry is None and _AVATAR_MANIFEST:
        for _k, v in _AVATAR_MANIFEST.items():
            if isinstance(v, dict) and _slugify_persona(v.get("name", "")) == slug:
                entry = v
                break

    if isinstance(entry, dict) and entry.get("asset"):
        asset_rel = entry["asset"]
        suffix = Path(asset_rel).suffix.lower()
        if suffix in (".jpg", ".jpeg", ".png"):
            # Use raw.githubusercontent.com (direct download, no 302 redirect)
            # github.com/.../raw/main returns 302 which browsers don't follow for img tags
            github_url = f"https://gitlab.com/perlowja/InvestorClaw/-/raw/main/{asset_rel}"
            return f'<img class="avatar" src="{github_url}" alt="{safe_name}" loading="lazy" />'

    # Filesystem fallback for SVG
    path = _resolve_avatar_path(persona_name)
    if path is None or path.suffix.lower() != ".svg":
        return None

    try:
        svg = path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Strip XML declaration/doctype so the SVG can be inlined safely inside HTML
    svg = re.sub(r"<\?xml[^>]*\?>", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>", "", svg)
    svg = svg.strip()
    # Inject an `avatar` class on the root <svg> for CSS styling.
    if svg.startswith("<svg"):
        if "class=" in svg[:200]:
            svg = re.sub(
                r'(<svg[^>]*?)class="([^"]*)"',
                lambda m: f'{m.group(1)}class="avatar {m.group(2)}"',
                svg,
                count=1,
            )
        else:
            svg = svg.replace("<svg", '<svg class="avatar"', 1)

    # Prefix all SVG ids + their references with the persona slug so two
    # avatars rendered in the same document don't share id="bg" /
    # id="avatarClip" / etc. and break each other's gradients/clip paths.
    svg = _prefix_svg_ids(svg, slug)
    return svg


def load_avatar_html(persona_name: str) -> Optional[str]:
    """Load a persona avatar as inlineable HTML (public wrapper for _load_avatar_image)."""
    return _load_avatar_image(persona_name)


def _lookup_archetype(persona_name: str) -> Optional[str]:
    """Return the manifest-declared archetype for a persona (if any)."""
    if not persona_name or not _AVATAR_MANIFEST:
        return None
    slug = _slugify_persona(persona_name)
    entry = _AVATAR_MANIFEST.get(slug)
    if entry is None:
        for _k, v in _AVATAR_MANIFEST.items():
            if isinstance(v, dict) and _slugify_persona(v.get("name", "")) == slug:
                entry = v
                break
    if isinstance(entry, dict):
        return entry.get("archetype")
    return None


# ---------------------------------------------------------------------------
# Palette (matches commands/dashboard.py)
# ---------------------------------------------------------------------------

PALETTE = {
    "equity": "#2563eb",
    "bond": "#059669",
    "cash": "#ca8a04",
    "margin": "#dc2626",
    "accent": "#38bdf8",
    "pos": "#4ade80",
    "neg": "#f87171",
}

# Default categorical palette for arbitrary chart slices
CHART_COLORS = [
    "#2563eb",
    "#059669",
    "#ca8a04",
    "#dc2626",
    "#38bdf8",
    "#a78bfa",
    "#f472b6",
    "#fb923c",
    "#22d3ee",
    "#84cc16",
    "#ec4899",
    "#6366f1",
]

# Persona archetype → accent color (mirrors stonkmode ANSI colors)
_ARCHETYPE_COLORS = {
    "high_energy": "#eab308",  # yellow
    "serious": "#3b82f6",  # blue
    "mentors": "#22c55e",  # green
    "policy_veterans": "#06b6d4",  # cyan
    "wildcards": "#a855f7",  # magenta
    "cosmic": "#06b6d4",
    "digital": "#a855f7",
    "bears": "#ef4444",  # red
}


# ---------------------------------------------------------------------------
# HTML template (dark mode, responsive 12-col grid)
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  :root {
    --bg:#0b1220; --card:#121a2e; --ink:#e5e7eb; --muted:#94a3b8;
    --accent:#38bdf8; --border:#1f2937;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--border); }
  header h1 { margin:0 0 6px 0; font-size:20px; font-weight:600; }
  header .meta-row { color:var(--muted); font-size:12px;
                     display:flex; flex-wrap:wrap; gap:16px; }
  header .meta-row strong { color:var(--ink); }
  .disclaimer { background:#7c2d12; color:#fed7aa; padding:10px 24px;
                font-size:12px; text-align:center; font-weight:500; }
  main { display:grid; grid-template-columns:repeat(12,1fr); gap:16px;
         padding:16px; }
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:12px; padding:16px; overflow:hidden; }
  .card h2 { margin:0 0 12px 0; font-size:14px; color:var(--muted);
             text-transform:uppercase; letter-spacing:0.05em; }
  .col-4 { grid-column: span 4; }
  .col-6 { grid-column: span 6; }
  .col-8 { grid-column: span 8; }
  .col-12 { grid-column: span 12; }
  @media (max-width:900px){
    .col-4,.col-6,.col-8 { grid-column: span 12; }
  }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { padding:6px 8px; text-align:left; border-bottom:1px solid var(--border); }
  th { color:var(--muted); font-weight:500; text-transform:uppercase;
       font-size:11px; letter-spacing:0.05em; }
  th.sortable { cursor:pointer; user-select:none; }
  th.sortable::after { content:" ⇅"; color:var(--muted); font-size:10px; opacity:0.5; }
  th.sortable.asc::after  { content:" ▲"; opacity:1; }
  th.sortable.desc::after { content:" ▼"; opacity:1; }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .pos { color:#4ade80; }
  .neg { color:#f87171; }
  .narrative { background:#0f172a; border-left:4px solid var(--accent);
               border-radius:8px; padding:14px 16px; margin-bottom:12px; }
  .narrative .persona { font-weight:600; font-size:13px;
                        text-transform:uppercase; letter-spacing:0.05em;
                        margin-bottom:6px; }
  .narrative .text { color:var(--ink); font-size:14px; line-height:1.55;
                     white-space:pre-wrap; }
  .dr-stonk { background:#0f172a; border:1px solid var(--border);
              border-radius:8px; padding:16px; }
  .dr-stonk h3 { margin:0 0 12px 0; font-size:13px; color:var(--accent);
                 text-transform:uppercase; letter-spacing:0.05em; }
  .dr-stonk dl { margin:0; }
  .dr-stonk dt { font-weight:600; color:var(--ink); margin-top:10px;
                 font-size:13px; }
  .dr-stonk dd { margin:4px 0 0 0; color:var(--muted); font-size:12px;
                 line-height:1.5; }
  .footer { text-align:center; color:var(--muted); font-size:11px;
            padding:20px; border-top:1px solid var(--border); }
  .footer .disc { color:#fca5a5; font-weight:500; margin-bottom:4px; }
  .empty-slot { color:var(--muted); font-style:italic; font-size:12px;
                padding:12px 0; }

  /* Stonkmode persona pair: two-column avatar + name + commentary */
  .stonkmode-pair {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 2rem;
    padding: 2rem;
    background: var(--card);
    border-radius: 0.5rem;
    border: 2px solid var(--accent);
    margin: 1.5rem 0;
  }
  .stonkmode-persona {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
  }
  .stonkmode-persona .avatar {
    width: 140px;
    height: 140px;
    border-radius: 50%;
    border: 3px solid var(--accent);
    margin-bottom: 1rem;
    object-fit: cover;
    background: var(--bg);
    display: block;
  }
  .stonkmode-persona.foil .avatar { border-color: #a855f7; }
  .stonkmode-persona .persona-name {
    font-weight: bold;
    font-size: 1.1rem;
    margin: 0.75rem 0 0.5rem;
    color: var(--accent);
    line-height: 1.3;
  }
  .stonkmode-persona.foil .persona-name { color: #a855f7; }
  .stonkmode-persona .persona-text {
    color: var(--muted);
    font-size: 0.95rem;
    line-height: 1.6;
    max-width: 100%;
    word-wrap: break-word;
    white-space: pre-wrap;
    text-align: left;
  }
  .stonkmode-closer {
    grid-column: 1 / -1;
    color: var(--muted);
    font-size: 0.9rem;
    line-height: 1.5;
    padding-top: 0.75rem;
    border-top: 1px dashed var(--border);
    text-align: center;
    font-style: italic;
  }
  .stonkmode-satire-note {
    grid-column: 1 / -1;
    color: var(--muted);
    font-size: 0.75rem;
    opacity: 0.75;
    text-align: center;
    font-style: italic;
  }
  @media (max-width: 768px) {
    .stonkmode-pair {
      grid-template-columns: 1fr;
      gap: 1.5rem;
      padding: 1.5rem;
    }
    .stonkmode-persona .avatar {
      width: 110px;
      height: 110px;
    }
  }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="meta-row">__METADATA__</div>
</header>
<div class="disclaimer">__DISCLAIMER__</div>

<main>__BLOCKS__</main>

<div class="footer">
  <div class="disc">__DISCLAIMER__ &middot; Not investment advice.</div>
  Generated by InvestorClaw &middot; __GENERATED_AT__ &middot;
  Fingerprint: <code>__FINGERPRINT__</code>
</div>

<script>
// ---------------------------------------------------------------------------
// Plotly chart specs
// ---------------------------------------------------------------------------
const CHART_SPECS = __CHART_SPECS__;
const PLOT_LAYOUT_BASE = {
  paper_bgcolor: 'transparent',
  plot_bgcolor:  'transparent',
  font:          {color: '#e5e7eb'},
  margin:        {t: 10, b: 40, l: 60, r: 10},
};

CHART_SPECS.forEach(spec => {
  const el = document.getElementById(spec.id);
  if (!el) return;
  const layout = Object.assign({}, PLOT_LAYOUT_BASE, spec.layout || {});
  Plotly.newPlot(spec.id, spec.data, layout,
                 {displayModeBar: false, responsive: true});
});

// ---------------------------------------------------------------------------
// Sortable tables
// ---------------------------------------------------------------------------
document.querySelectorAll('table[data-sortable="1"]').forEach(table => {
  const ths = table.querySelectorAll('th');
  ths.forEach((th, colIdx) => {
    th.classList.add('sortable');
    th.addEventListener('click', () => sortTable(table, colIdx, th));
  });
});

function sortTable(table, colIdx, th) {
  const tbody = table.querySelector('tbody');
  const rows  = Array.from(tbody.querySelectorAll('tr'));
  const asc   = !th.classList.contains('asc');

  // Clear sort indicators on all headers
  table.querySelectorAll('th').forEach(h => {
    h.classList.remove('asc'); h.classList.remove('desc');
  });
  th.classList.add(asc ? 'asc' : 'desc');

  rows.sort((a, b) => {
    const av = a.children[colIdx]?.textContent?.trim() ?? '';
    const bv = b.children[colIdx]?.textContent?.trim() ?? '';
    const an = parseFloat(av.replace(/[$,%\s+]/g, ''));
    const bn = parseFloat(bv.replace(/[$,%\s+]/g, ''));
    if (!isNaN(an) && !isNaN(bn)) {
      return asc ? an - bn : bn - an;
    }
    return asc ? av.localeCompare(bv) : bv.localeCompare(av);
  });

  rows.forEach(r => tbody.appendChild(r));
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape(value: Any) -> str:
    """HTML-escape a scalar for safe embedding."""
    return _html_mod.escape(str(value), quote=True)


def _escape_json_for_script(payload: Any) -> str:
    """Serialize to JSON and escape </script> breakouts."""
    return json.dumps(payload, default=str, separators=(",", ":")).replace("<", "\\u003c")


def _format_cell(value: Any) -> str:
    """Render a cell's value as a string with sensible defaults."""
    if value is None:
        return ""
    if isinstance(value, float):
        # Preserve 2 decimals for financial data
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _is_numeric_string(text: str) -> bool:
    """Heuristic: does a cell look numeric after stripping $/%/commas?"""
    t = text.replace("$", "").replace(",", "").replace("%", "").strip()
    if not t:
        return False
    if t.startswith(("-", "+")):
        t = t[1:]
    try:
        float(t)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# ArtifactGenerator
# ---------------------------------------------------------------------------


class ArtifactGenerator:
    """Composable HTML artifact builder.

    Call add_* methods to append blocks (charts/tables/narrative/definitions)
    in render order, then .save(path) to flush the HTML to disk.
    """

    def __init__(
        self,
        title: str,
        disclaimer: str = "EDUCATIONAL ANALYSIS - NOT INVESTMENT ADVICE",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.title = title
        self.disclaimer = disclaimer
        self.metadata: Dict[str, Any] = dict(metadata or {})
        self._blocks: List[str] = []  # HTML fragments for <main>
        self._chart_specs: List[Dict[str, Any]] = []  # Plotly specs
        self._chart_counter = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_chart_id(self, prefix: str = "chart") -> str:
        self._chart_counter += 1
        return f"{prefix}-{self._chart_counter:03d}-{uuid.uuid4().hex[:6]}"

    def _add_chart(
        self,
        chart_id: str,
        data: List[Dict[str, Any]],
        layout: Dict[str, Any],
        title: str,
        col_class: str = "col-6",
        height: int = 300,
    ) -> None:
        """Common wrapper: card + div + Plotly spec registration."""
        self._blocks.append(
            f'<section class="card {col_class}">'
            f"  <h2>{_escape(title)}</h2>"
            f'  <div id="{_escape(chart_id)}" style="height:{int(height)}px;"></div>'
            f"</section>"
        )
        self._chart_specs.append({"id": chart_id, "data": data, "layout": layout})

    # ------------------------------------------------------------------
    # Public chart methods
    # ------------------------------------------------------------------

    def add_pie_chart(
        self,
        labels: Iterable[Any],
        values: Iterable[Any],
        title: str,
        col_class: str = "col-4",
        height: int = 300,
        colors: Optional[Sequence[str]] = None,
    ) -> None:
        """Append a donut pie chart."""
        labels_list = [str(x) for x in labels]
        values_list = [float(v) for v in values]
        pie_colors = (
            list(colors)
            if colors
            else [CHART_COLORS[i % len(CHART_COLORS)] for i in range(len(labels_list))]
        )
        chart_id = self._next_chart_id("pie")
        data = [
            {
                "type": "pie",
                "labels": labels_list,
                "values": values_list,
                "hole": 0.5,
                "textinfo": "label+percent",
                "textposition": "outside",
                "marker": {"colors": pie_colors},
                "hovertemplate": "%{label}<br>%{value:,.0f}<extra></extra>",
            }
        ]
        layout = {
            "showlegend": False,
            "margin": {"t": 10, "b": 10, "l": 10, "r": 10},
        }
        self._add_chart(chart_id, data, layout, title, col_class, height)

    def add_bar_chart(
        self,
        x: Iterable[Any],
        y: Iterable[Any],
        title: str,
        x_label: str = "",
        y_label: str = "",
        col_class: str = "col-6",
        height: int = 300,
        orientation: str = "v",
        color: str = PALETTE["accent"],
    ) -> None:
        """Append a bar chart (vertical by default)."""
        x_list = [str(v) for v in x]
        y_list = [float(v) for v in y]
        chart_id = self._next_chart_id("bar")
        if orientation == "h":
            data = [
                {
                    "type": "bar",
                    "orientation": "h",
                    "x": y_list,
                    "y": x_list,
                    "marker": {"color": color},
                    "hovertemplate": "%{y}: %{x:,.2f}<extra></extra>",
                }
            ]
            layout = {
                "margin": {"t": 10, "b": 30, "l": 140, "r": 10},
                "xaxis": {"title": y_label, "gridcolor": "#1f2937"},
                "yaxis": {"title": x_label, "autorange": "reversed"},
            }
        else:
            data = [
                {
                    "type": "bar",
                    "x": x_list,
                    "y": y_list,
                    "marker": {"color": color},
                    "hovertemplate": "%{x}: %{y:,.2f}<extra></extra>",
                }
            ]
            layout = {
                "margin": {"t": 10, "b": 50, "l": 60, "r": 10},
                "xaxis": {"title": x_label, "gridcolor": "#1f2937"},
                "yaxis": {"title": y_label, "gridcolor": "#1f2937"},
            }
        self._add_chart(chart_id, data, layout, title, col_class, height)

    def add_line_chart(
        self,
        x: Iterable[Any],
        y: Iterable[Any],
        title: str,
        x_label: str = "",
        y_label: str = "",
        col_class: str = "col-6",
        height: int = 300,
        color: str = PALETTE["accent"],
    ) -> None:
        """Append a line chart."""
        x_list = [str(v) for v in x]
        y_list = [float(v) for v in y]
        chart_id = self._next_chart_id("line")
        data = [
            {
                "type": "scatter",
                "mode": "lines+markers",
                "x": x_list,
                "y": y_list,
                "line": {"color": color, "width": 2},
                "marker": {"color": color, "size": 4},
                "hovertemplate": "%{x}: %{y:,.4f}<extra></extra>",
            }
        ]
        layout = {
            "margin": {"t": 10, "b": 50, "l": 60, "r": 10},
            "xaxis": {"title": x_label, "gridcolor": "#1f2937"},
            "yaxis": {"title": y_label, "gridcolor": "#1f2937"},
        }
        self._add_chart(chart_id, data, layout, title, col_class, height)

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def add_table(
        self,
        df: Any,
        title: str,
        sortable: bool = True,
        col_class: str = "col-12",
        columns: Optional[Sequence[str]] = None,
        max_rows: Optional[int] = None,
    ) -> None:
        """Append an HTML table.

        Accepts:
            - pandas.DataFrame / polars.DataFrame (duck-typed via .to_dicts /
              .to_dict('records'))
            - list[dict]
            - list[list] with ``columns=[...]``
        """
        rows, cols = self._table_rows(df, columns)
        if max_rows is not None:
            rows = rows[:max_rows]

        if not rows:
            self._blocks.append(
                f'<section class="card {col_class}">'
                f"  <h2>{_escape(title)}</h2>"
                f'  <div class="empty-slot">No data available.</div>'
                f"</section>"
            )
            return

        sortable_attr = ' data-sortable="1"' if sortable else ""
        thead = "".join(f"<th>{_escape(c)}</th>" for c in cols)
        body_parts: List[str] = []
        for row in rows:
            cells: List[str] = []
            for c in cols:
                raw_val = row.get(c) if isinstance(row, dict) else None
                text = _format_cell(raw_val)
                cls_parts: List[str] = []
                if _is_numeric_string(text):
                    cls_parts.append("num")
                    # Colorize signed percentages
                    if "%" in text or text.startswith(("+", "-")):
                        if text.startswith("-"):
                            cls_parts.append("neg")
                        elif text.startswith("+"):
                            cls_parts.append("pos")
                cls_attr = f' class="{" ".join(cls_parts)}"' if cls_parts else ""
                cells.append(f"<td{cls_attr}>{_escape(text)}</td>")
            body_parts.append("<tr>" + "".join(cells) + "</tr>")

        table_html = (
            f"<table{sortable_attr}>"
            f"<thead><tr>{thead}</tr></thead>"
            f"<tbody>{''.join(body_parts)}</tbody>"
            f"</table>"
        )
        self._blocks.append(
            f'<section class="card {col_class}">  <h2>{_escape(title)}</h2>  {table_html}</section>'
        )

    @staticmethod
    def _table_rows(
        df: Any,
        columns: Optional[Sequence[str]],
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Normalize supported df-like inputs into (rows, columns)."""
        # pandas / polars duck-typing
        if hasattr(df, "to_dict") and callable(getattr(df, "to_dict")):
            try:
                rec = df.to_dict("records")  # pandas
                if rec and isinstance(rec, list):
                    cols = list(columns) if columns else list(rec[0].keys())
                    return rec, cols
            except (TypeError, ValueError):
                pass
        if hasattr(df, "to_dicts") and callable(getattr(df, "to_dicts")):
            try:
                rec = df.to_dicts()  # polars
                if rec and isinstance(rec, list):
                    cols = list(columns) if columns else list(rec[0].keys())
                    return rec, cols
            except Exception:
                pass

        if isinstance(df, list):
            if not df:
                return [], list(columns) if columns else []
            if isinstance(df[0], dict):
                cols = list(columns) if columns else list(df[0].keys())
                return df, cols
            if isinstance(df[0], (list, tuple)):
                if not columns:
                    raise ValueError("list-of-lists tables require `columns=[...]`")
                rows = [dict(zip(columns, r)) for r in df]
                return rows, list(columns)

        # Last-ditch: try iterating keys on a Mapping-like object
        if isinstance(df, Mapping):
            cols = list(columns) if columns else list(df.keys())
            return [dict(df)], cols

        raise TypeError(f"Unsupported table input type: {type(df).__name__}")

    # ------------------------------------------------------------------
    # Narrative / persona block (stonkmode commentary)
    # ------------------------------------------------------------------

    def add_narrative_block(
        self,
        title: str,
        content: str,
        persona: Optional[str] = None,
        archetype: Optional[str] = None,
        col_class: str = "col-12",
    ) -> None:
        """Append a stonkmode narrative block.

        `content` may include embedded newlines for multiple paragraphs.
        If `persona` or `archetype` is provided the block takes on that
        persona's accent color on its left border.
        """
        accent = (
            _ARCHETYPE_COLORS.get(archetype or "", PALETTE["accent"])
            if archetype
            else PALETTE["accent"]
        )
        persona_html = (
            f'<div class="persona" style="color:{_escape(accent)};">{_escape(persona)}</div>'
            if persona
            else ""
        )
        text_html = _escape(content or "")
        inner = (
            f'<div class="narrative" style="border-left-color:{_escape(accent)};">'
            f"  {persona_html}"
            f'  <div class="text">{text_html}</div>'
            f"</div>"
        )
        self._blocks.append(
            f'<section class="card {col_class}">  <h2>{_escape(title)}</h2>  {inner}</section>'
        )

    def add_stonkmode_pair(
        self,
        lead_name: str,
        lead_text: str,
        foil_name: str,
        foil_text: str,
        lead_archetype: Optional[str] = None,
        foil_archetype: Optional[str] = None,
        closer: Optional[str] = None,
        title: str = "Market Commentary (STONKMODE)",
        col_class: str = "col-12",
    ) -> None:
        """Append a lead + foil stonkmode narrative card with persona avatars.

        Avatars are auto-resolved from ``assets/stonkmode-avatars/`` using
        ``manifest.json`` (if present) with a filesystem fallback on the
        slugified persona name (e.g. "Blitz Thunderbuy" → "blitz_thunderbuy.svg").
        When an avatar cannot be located the block renders the persona name
        alone — the commentary still displays normally.
        """
        # Fall back to manifest-declared archetype when the caller didn't supply one
        lead_arch = lead_archetype or _lookup_archetype(lead_name)
        foil_arch = foil_archetype or _lookup_archetype(foil_name)

        lead_accent = (
            _ARCHETYPE_COLORS.get(lead_arch or "", PALETTE["accent"])
            if lead_arch
            else PALETTE["accent"]
        )
        foil_accent = _ARCHETYPE_COLORS.get(foil_arch or "", "#a855f7") if foil_arch else "#a855f7"

        lead_img = _load_avatar_image(lead_name)
        foil_img = _load_avatar_image(foil_name)

        def _persona_col(role: str, name: str, text: str, img: Optional[str], accent: str) -> str:
            avatar_html = (
                img
                if img
                else (
                    f'<div class="avatar" aria-hidden="true" '
                    f'style="display:flex;align-items:center;justify-content:center;'
                    f'color:{_escape(accent)};font-weight:700;font-size:32px;">'
                    f"{_escape((name or '?')[:2].upper())}</div>"
                )
            )
            return (
                f'<div class="stonkmode-persona {role}" '
                f'style="--accent:{_escape(accent)};">'
                f"  {avatar_html}"
                f'  <h4 class="persona-name" style="color:{_escape(accent)};">'
                f"{_escape(name or '')}</h4>"
                f'  <p class="persona-text">{_escape(text or "")}</p>'
                f"</div>"
            )

        parts: List[str] = [
            _persona_col("lead", lead_name, lead_text, lead_img, lead_accent),
            _persona_col("foil", foil_name, foil_text, foil_img, foil_accent),
        ]

        if closer:
            parts.append(
                f'<div class="stonkmode-closer">'
                f"SIGN-OFF — {_escape(lead_name)}: {_escape(closer)}"
                f"</div>"
            )

        parts.append(
            '<div class="stonkmode-satire-note">'
            "STONKMODE — entertainment satire. Not financial advice. "
            "Fictional cable TV characters only."
            "</div>"
        )

        self._blocks.append(
            f'<section class="card {col_class}">'
            f"  <h2>{_escape(title)}</h2>"
            f'  <div class="stonkmode-pair">{"".join(parts)}</div>'
            f"</section>"
        )

    # ------------------------------------------------------------------
    # Dr. Stonk educational box
    # ------------------------------------------------------------------

    def add_dr_stonk_box(
        self,
        terms_dict: Mapping[str, str],
        title: str = "Dr. Stonk — Financial Terminology",
        col_class: str = "col-12",
    ) -> None:
        """Append Dr. Stonk term definitions as a styled <dl>."""
        if not terms_dict:
            return
        items: List[str] = []
        for term in sorted(terms_dict.keys()):
            definition = terms_dict[term]
            items.append(f"<dt>{_escape(term)}</dt><dd>{_escape(definition)}</dd>")
        box = (
            '<div class="dr-stonk">'
            "  <h3>🖖 Logical explanations for terms used above</h3>"
            f"  <dl>{''.join(items)}</dl>"
            "</div>"
        )
        self._blocks.append(
            f'<section class="card {col_class}">  <h2>{_escape(title)}</h2>  {box}</section>'
        )

    # ------------------------------------------------------------------
    # Free-form HTML (escape hatch)
    # ------------------------------------------------------------------

    def add_raw_block(
        self, html: str, title: Optional[str] = None, col_class: str = "col-12"
    ) -> None:
        """Append a pre-built HTML block. Caller is responsible for escaping."""
        title_html = f"<h2>{_escape(title)}</h2>" if title else ""
        self._blocks.append(f'<section class="card {col_class}">{title_html}{html}</section>')

    # ------------------------------------------------------------------
    # Render / save
    # ------------------------------------------------------------------

    def render(self) -> str:
        """Assemble HTML string without writing to disk."""
        # Metadata row
        meta_parts: List[str] = []
        for k, v in self.metadata.items():
            meta_parts.append(f"<span>{_escape(k)}: <strong>{_escape(v)}</strong></span>")
        meta_html = "".join(meta_parts) if meta_parts else "<span>&nbsp;</span>"

        blocks_html = (
            "\n".join(self._blocks)
            if self._blocks
            else '<section class="card col-12"><div class="empty-slot">No content.</div></section>'
        )

        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        fingerprint = uuid.uuid4().hex[:12]

        html = _HTML_TEMPLATE
        html = html.replace("__TITLE__", _escape(self.title))
        html = html.replace("__DISCLAIMER__", _escape(self.disclaimer))
        html = html.replace("__METADATA__", meta_html)
        html = html.replace("__BLOCKS__", blocks_html)
        html = html.replace("__GENERATED_AT__", _escape(generated_at))
        html = html.replace("__FINGERPRINT__", _escape(fingerprint))
        html = html.replace(
            "__CHART_SPECS__",
            _escape_json_for_script(self._chart_specs),
        )
        return html

    def save(self, output_path: Union[str, Path]) -> Path:
        """Render the HTML and write it to disk. Returns the Path written."""
        path = Path(output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Convenience helpers the command modules share
# ---------------------------------------------------------------------------


def parse_artifact_flags(argv: Sequence[str]) -> Tuple[Optional[str], bool]:
    """Extract (artifact_path, stonkmode_enabled) from an argv-like list.

    Returns (None, False) if --artifact is not present.  Stonkmode is
    enabled by either --stonkmode CLI flag OR an active state file; the
    flag lets commands force-enable narration for a single artifact run
    without toggling global state.
    """
    artifact_path: Optional[str] = None
    if "--artifact" in argv:
        idx = argv.index("--artifact")
        if idx + 1 < len(argv):
            artifact_path = argv[idx + 1]

    stonk = "--stonkmode" in argv
    if not stonk:
        try:
            from ic_engine.rendering.stonkmode import is_enabled

            stonk = bool(is_enabled())
        except Exception:
            stonk = False
    return artifact_path, stonk


def extract_dr_stonk_definitions(terms: Iterable[str]) -> Dict[str, str]:
    """Pull definitions for the given terms from dr_stonk.TERM_EXPLANATIONS.

    Unknown terms are silently dropped.
    """
    try:
        from ic_engine.rendering.dr_stonk import TERM_EXPLANATIONS
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for term in terms:
        if not term:
            continue
        if term in TERM_EXPLANATIONS:
            out[term] = TERM_EXPLANATIONS[term]
    return out


def detect_terms_in_text(text: str) -> List[str]:
    """Find financial terms (from dr_stonk.TERM_EXPLANATIONS) that appear in text.

    Case-insensitive substring match, deduplicated, preserves insertion order.
    """
    try:
        from ic_engine.rendering.dr_stonk import TERM_EXPLANATIONS
    except Exception:
        return []
    if not text:
        return []
    low = text.lower()
    found: Dict[str, None] = {}
    for term in TERM_EXPLANATIONS.keys():
        if term.lower() in low and term not in found:
            found[term] = None
    return list(found.keys())


def get_stonkmode_narrative(
    command: str,
    data_summary: str,
) -> Optional[Dict[str, Any]]:
    """Generate a stonkmode narrative pair for the given command + data summary.

    Returns a dict with:
        {
          "lead":       {"name": ..., "archetype": ..., "text": ...},
          "foil":       {"name": ..., "archetype": ..., "text": ...},
          "closer":     Optional[str],
          "inference_ms": int,
        }
    or None if stonkmode state is unavailable or LLM offline.
    """
    try:
        from ic_engine.rendering.stonkmode import (
            build_foil_system_prompt,
            build_foil_user_prompt,
            build_lead_system_prompt,
            build_lead_user_prompt,
            extract_tickers_from_summary,
            generate_narration,
            get_persona,
            load_state,
            select_cohost_mode,
        )
    except Exception:
        return None

    state = load_state() or {}
    lead_id = state.get("lead_id")
    foil_id = state.get("foil_id")
    if not lead_id or not foil_id:
        return None

    try:
        lead = get_persona(lead_id)
        foil = get_persona(foil_id)
    except KeyError:
        return None

    history = state.get("session_message_history", []) or []
    foil_history = [m.get("foil", "") for m in history if m.get("foil")]
    previous_foil = foil_history[-1] if foil_history else None
    cohost_mode = select_cohost_mode(has_previous_foil_message=bool(previous_foil))
    ticker_allowlist = extract_tickers_from_summary(data_summary)

    t0 = time.monotonic()
    lead_sys = build_lead_system_prompt(
        lead, foil, command, cohost_mode, previous_foil, ticker_allowlist
    )
    lead_user = build_lead_user_prompt(
        lead, foil, command, data_summary, cohost_mode, previous_foil, history
    )
    lead_text = generate_narration(lead_sys, lead_user, max_tokens=1000)
    if not lead_text:
        return None

    foil_sys = build_foil_system_prompt(lead, foil, command, ticker_allowlist)
    foil_user = build_foil_user_prompt(lead, foil, lead_text, foil_history)
    foil_text = generate_narration(foil_sys, foil_user, max_tokens=900)
    if not foil_text:
        foil_text = "(no response)"

    return {
        "lead": {
            "id": lead_id,
            "name": lead["name"],
            "archetype": lead.get("archetype", "wildcards"),
            "text": lead_text,
        },
        "foil": {
            "id": foil_id,
            "name": foil["name"],
            "archetype": foil.get("archetype", "wildcards"),
            "text": foil_text,
        },
        "closer": None,
        "inference_ms": int((time.monotonic() - t0) * 1000),
    }
