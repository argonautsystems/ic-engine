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
Generate standalone PWA (Progressive Web App) for OpenClaw.
Embeds portfolio data in HTML for offline-first functionality.

Usage:
    python pwa_generator.py --holdings holdings.json --output ~/.investorclaw/dashboard.html
    python pwa_generator.py --holdings holdings.json --performance perf.json --bonds bonds.json --output path/
"""

import argparse
import json
import shutil
from pathlib import Path


class PWAGenerator:
    """Generates standalone PWA with embedded portfolio data."""

    def __init__(
        self, holdings_file=None, performance_file=None, bonds_file=None, analyst_file=None
    ):
        """Initialize with portfolio data files."""
        self.data = {
            "holdings": {},
            "performance": {},
            "bonds": {},
            "analyst": {},
            "metadata": {"version": "2.1.9"},
        }

        if holdings_file:
            with open(holdings_file) as f:
                self.data["holdings"] = json.load(f)

        if performance_file:
            with open(performance_file) as f:
                self.data["performance"] = json.load(f)

        if bonds_file:
            with open(bonds_file) as f:
                self.data["bonds"] = json.load(f)

        if analyst_file:
            with open(analyst_file) as f:
                self.data["analyst"] = json.load(f)

    def generate(self, output_path):
        """
        Generate standalone PWA with embedded data.

        Args:
            output_path: Directory or file path for output
                - If directory: creates dashboard.html, assets/, manifest.json
                - If file: creates single HTML file with embedded assets
        """
        output_path = Path(output_path)

        # Create directory if needed
        if output_path.is_file() or str(output_path).endswith(".html"):
            # Single file mode
            return self._generate_single_file(output_path)
        else:
            # Directory mode
            output_path.mkdir(parents=True, exist_ok=True)
            return self._generate_directory(output_path)

    def _generate_single_file(self, html_file):
        """Generate single self-contained HTML file."""
        html_file = Path(html_file)
        html_file.parent.mkdir(parents=True, exist_ok=True)

        # Load template
        template_path = Path(__file__).parent / "pwa" / "dashboard.html"
        with open(template_path) as f:
            html = f.read()

        # Embed data as window.IC_DATA
        data_script = f"<script>window.IC_DATA = {json.dumps(self.data, indent=2)};</script>"
        html = html.replace("<!-- DATA_PLACEHOLDER -->", data_script)

        # Embed CSS
        css_path = Path(__file__).parent / "pwa" / "assets" / "styles.css"
        with open(css_path) as f:
            css = f.read()
        css_tag = f"<style>{css}</style>"
        html = html.replace('<link rel="stylesheet" href="assets/styles.css">', css_tag)

        # Embed JavaScript files
        js_files = ["data-loader.js", "charts.js", "app.js"]
        for js_file in js_files:
            js_path = Path(__file__).parent / "pwa" / "assets" / js_file
            with open(js_path) as f:
                js = f.read()
            js_tag = f"<script>{js}</script>"
            html = html.replace(f'<script src="assets/{js_file}"></script>', js_tag)

        # Embed service worker inline
        sw_path = Path(__file__).parent / "pwa" / "service-worker.js"
        with open(sw_path) as f:
            sw = f.read()
        # Service worker must be external file for registration, so we'll create it alongside
        sw_file = html_file.parent / "service-worker.js"
        with open(sw_file, "w") as f:
            f.write(sw)

        # Write final HTML
        with open(html_file, "w") as f:
            f.write(html)

        print(f"✅ Generated standalone PWA: {html_file}")
        print(f"   Size: {html_file.stat().st_size / 1024:.1f} KB")
        print(f"   Open in browser: file://{html_file.resolve()}")

        return html_file

    def _generate_directory(self, output_dir):
        """Generate directory with separate files."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Copy HTML template
        template_path = Path(__file__).parent / "pwa" / "dashboard.html"
        html_file = output_dir / "dashboard.html"
        with open(template_path) as f:
            html = f.read()

        # Embed data
        data_script = f"<script>window.IC_DATA = {json.dumps(self.data, indent=2)};</script>"
        html = html.replace("<!-- DATA_PLACEHOLDER -->", data_script)

        with open(html_file, "w") as f:
            f.write(html)

        # Copy assets directory
        assets_src = Path(__file__).parent / "pwa" / "assets"
        assets_dst = output_dir / "assets"
        if assets_dst.exists():
            shutil.rmtree(assets_dst)
        shutil.copytree(assets_src, assets_dst)

        # Copy service worker
        sw_src = Path(__file__).parent / "pwa" / "service-worker.js"
        sw_dst = output_dir / "service-worker.js"
        shutil.copy(sw_src, sw_dst)

        # Copy manifest
        manifest_src = Path(__file__).parent / "pwa" / "manifest.json"
        manifest_dst = output_dir / "manifest.json"
        shutil.copy(manifest_src, manifest_dst)

        print(f"✅ Generated PWA directory: {output_dir}")
        print("   Files:")
        print(f"   - dashboard.html ({html_file.stat().st_size / 1024:.1f} KB)")
        print("   - assets/ (shared code)")
        print("   - service-worker.js (offline support)")
        print("   - manifest.json (PWA metadata)")
        print(f"\n   Open in browser: file://{html_file.resolve()}")

        return output_dir


def main():
    parser = argparse.ArgumentParser(
        description="Generate InvestorClaw PWA with embedded portfolio data"
    )
    parser.add_argument("--holdings", help="Holdings JSON file")
    parser.add_argument("--performance", help="Performance JSON file")
    parser.add_argument("--bonds", help="Bonds JSON file")
    parser.add_argument("--analyst", help="Analyst JSON file")
    parser.add_argument("--output", required=True, help="Output directory or file path")

    args = parser.parse_args()

    # Validate input files
    for file_path in [args.holdings, args.performance, args.bonds, args.analyst]:
        if file_path and not Path(file_path).exists():
            print(f"❌ File not found: {file_path}")
            return 1

    # Generate PWA
    generator = PWAGenerator(
        holdings_file=args.holdings,
        performance_file=args.performance,
        bonds_file=args.bonds,
        analyst_file=args.analyst,
    )

    try:
        generator.generate(args.output)
        return 0
    except Exception as e:
        print(f"❌ Generation failed: {e}")
        return 1


if __name__ == "__main__":
    exit(main())
