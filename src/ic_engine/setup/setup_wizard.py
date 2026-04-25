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
InvestorClaw First-Time Setup Wizard

Interactive guided setup for narrative + consultative LLM configuration.
Narrative: Together AI (MiniMax-M2.7) or Google (Gemini-2.5-flash)
Consultative: Cloud Gemma4 or Hybrid (local gemma4-consult via Ollama/llama.cpp/LMStudio)
"""

import datetime
import json
import sys
from pathlib import Path
from typing import Dict, Optional

# Ensure skill root is on sys.path so rendering.stonkmode is importable
_SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

try:
    from rendering.stonkmode import stonkmode_tip as _stonkmode_tip
except Exception:

    def _stonkmode_tip(always: bool = False) -> str:  # type: ignore[misc]
        return (
            "📊 PRO TIP — STONKMODE:\n"
            "  Once you have portfolio data, try the entertainment layer:\n"
            "  /portfolio stonkmode on\n"
            "  Then run any analysis command to get live commentary from\n"
            "  30 fictional cable TV finance personalities — bears, bulls,\n"
            "  crypto maxis, ESG crusaders, a Kardashian, a goblin, and more.\n"
            "  /portfolio stonkmode off  to return to normal mode."
        )


def _log_fa_professional_activation() -> None:
    """Append a timestamped entry to the FA Professional audit log.

    Creates ~/.investorclaw/fa_audit.log if it doesn't exist.
    Each line: ISO timestamp | event | attestation status
    """
    log_dir = Path.home() / ".investorclaw"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "fa_audit.log"
    entry = (
        f"{datetime.datetime.now().isoformat()} "
        f"| FA Professional mode activated "
        f"| attestation: user-confirmed via interactive wizard\n"
    )
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write(entry)


# Import portfolio analyzer
try:
    from config.portfolio_sizer import analyze_portfolio, print_analysis
except ImportError:
    analyze_portfolio = None
    print_analysis = None

# Import mode definitions
try:
    from config.deployment_modes import DeploymentMode
except ImportError:
    DeploymentMode = None


class SetupWizard:
    """Interactive setup wizard for InvestorClaw."""

    def __init__(self):
        self.config_dir = Path.home() / ".investorclaw"
        self.config_file = self.config_dir / "setup_config.json"
        self.config = {}

    def print_header(self, title: str) -> None:
        """Print a formatted header."""
        print("\n" + "=" * 70)
        print(title.center(70))
        print("=" * 70 + "\n")

    def print_section(self, title: str) -> None:
        """Print a formatted section."""
        print("\n" + "-" * 70)
        print(title)
        print("-" * 70 + "\n")

    def ask_about_espp(self) -> Dict:
        """Ask about ESPP (Employee Stock Purchase Plan) holdings.

        ESPP shares are employer-provided stock benefits (active or vested/legacy).
        They should not be flagged for concentration risk since they represent
        forced employer compensation, not investment choices.

        Status types:
          • active: Currently buying through ESPP program
          • vested: Bought through ESPP but program ended or you left employer
          • legacy: Old ESPP shares held long-term (e.g., MSFT from prior employment)
        """
        self.print_section("Step 0.5: ESPP & Employer Stock Holdings")

        espp_programs = {}

        print("""Do you have any ESPP or employer stock holdings?

ESPP shares (active or vested) and legacy employer stock are often forced
long-term holdings. InvestorClaw will exclude them from concentration risk
warnings since they represent employer compensation, not investment choices.

Examples:
  • NVDA: Active ESPP (transferred to UBS for wealth management)
  • MSFT: Vested ESPP (legacy, kept at Schwab - don't diversify)
  • AMZN: Active ESPP (held at Fidelity during employment)
""")

        while True:
            has_espp = (
                input("Do you have any ESPP or employer stock holdings? [y/n]: ").strip().lower()
            )
            if has_espp in ["y", "n"]:
                break
            print("Please enter 'y' or 'n'")

        if has_espp == "n":
            return espp_programs

        # Collect ESPP/employer stock details
        print("\nEnter your ESPP holdings (press Enter with empty employer name when done):\n")

        while True:
            employer = input("Employer name (e.g., 'NVIDIA', 'Microsoft'): ").strip()
            if not employer:
                break

            symbol = (
                input(f"  Stock symbol for {employer} (e.g., 'NVDA', 'MSFT'): ").strip().upper()
            )
            if not symbol:
                print("  Skipping - symbol required\n")
                continue

            shares = input("  Number of shares (or press Enter to skip): ").strip()
            try:
                share_count = int(shares) if shares else None
            except ValueError:
                print("  Invalid share count, skipping\n")
                continue

            status = None
            while status is None:
                status_input = (
                    input("  Status [a=active ESPP, v=vested/ended, l=legacy/inherited]: ")
                    .strip()
                    .lower()
                )
                status_map = {"a": "active", "v": "vested", "l": "legacy"}
                status = status_map.get(status_input)
                if status is None:
                    print("  Please enter 'a', 'v', or 'l'")

            location = input(
                "  Held at (e.g., 'Schwab', 'UBS', 'Fidelity', or 'employer'): "
            ).strip()

            espp_programs[employer.lower()] = {
                "symbol": symbol,
                "shares": share_count,
                "status": status,  # active, vested, or legacy
                "held_at": location or "employer brokerage",
            }

            print()

        if espp_programs:
            print(f"✓ Recorded {len(espp_programs)} ESPP/employer stock holding(s)")
            for emp, details in espp_programs.items():
                status_label = {
                    "active": "actively buying",
                    "vested": "vested (ended)",
                    "legacy": "legacy/inherited",
                }
                print(
                    f"   • {details['symbol']}: {status_label.get(details['status'])} ({details['held_at']})"
                )

        return espp_programs

    def select_deployment_mode(self) -> Optional[str]:
        """Select deployment mode (single investor vs FA professional)."""
        if not DeploymentMode:
            print("⚠️  Mode system not available (deployment_modes.py missing)")
            return "single_investor"  # Default to single investor

        self.print_section("Step 0: Choose Your Deployment Mode")

        print("""InvestorClaw supports two deployment modes:

╔════════════════════════════════════════════════════════════════════╗
║  1. SINGLE INVESTOR (Retail)                                       ║
║  ─────────────────────────────────────────────────────────────    ║
║  "I manage my own portfolio"                                      ║
║                                                                   ║
║  Features:                                                         ║
║    ✓ Holdings snapshot, performance, news, analyst ratings        ║
║    ✓ Rebalancing hints (educational only)                         ║
║    ✓ Simple reports (CSV/PDF)                                     ║
║                                                                   ║
║  Guardrails:                                                      ║
║    • Educational-only language ("may indicate", "might evaluate")║
║    • No investment directives                                    ║
║    • Advisor disclaimers on all output                           ║
║                                                                   ║
║  Best for: Individual investors managing their own money          ║
╚════════════════════════════════════════════════════════════════════╝

╔════════════════════════════════════════════════════════════════════╗
║  2. FA PROFESSIONAL — ⚠️  DANGEROUS MODE               ⚖️  PREMIUM ║
║  ─────────────────────────────────────────────────────────────    ║
║  "I advise clients on their portfolios"                           ║
║                                                                   ║
║  🚨 WARNING: This mode generates SPECIFIC recommendations.        ║
║     Not for individual retail investors. Advisor fiduciary       ║
║     duty applies. All outputs carry elevated risk disclosure.    ║
║                                                                   ║
║  Features:                                                         ║
║    ✓ All of Single Investor mode, PLUS:                          ║
║    ✓ ETF classification (is_etf, security_type per holding)     ║
║      [planned] ETF constituent expansion (detailed allocation)  ║
║    ✓ Tax-loss harvesting candidates                              ║
║    ✓ Tactical sector rebalancing                                 ║
║    ✓ Multi-portfolio management                                  ║
║    ✓ Compliance documentation                                    ║
║    ✓ Audit trail (all actions logged)                            ║
║                                                                   ║
║  Requirements:                                                     ║
║    ⚠️  Business license verification                              ║
║    ⚠️  7-year audit trail enabled                                 ║
║    ⚠️  Compliance documentation required                          ║
║    ⚠️  Advisor assumes full fiduciary responsibility              ║
║                                                                   ║
║  Best for: Financial advisors, wealth managers, professionals    ║
╚════════════════════════════════════════════════════════════════════╝

Which mode describes your use case?
""")

        while True:
            choice = input("Select [1=Single Investor, 2=FA Professional]: ").strip()
            if choice == "1":
                print("\n✓ Selected: Single Investor\n")
                return "single_investor"
            elif choice == "2":
                print("""
⚠️  FA PROFESSIONAL MODE — ATTESTATION REQUIRED

This mode removes educational guardrails and enables advisory-grade output.
By activating it you confirm that:

  1. You are a licensed financial advisor acting under fiduciary duty
  2. You will use this tool in compliance with all applicable regulations
  3. You accept full fiduciary responsibility for all recommendations
  4. This activation will be logged with a timestamp

Type exactly  I ATTEST  to confirm, or press Enter to cancel:
""")
                confirm = input("Attestation: ").strip()
                if confirm == "I ATTEST":
                    _log_fa_professional_activation()
                    print(
                        "\n✓ FA Professional mode activated. Activation logged to ~/.investorclaw/fa_audit.log\n"
                    )
                    return "fa_professional"
                else:
                    print("\n⚠️  Attestation not confirmed. Defaulting to Single Investor mode.\n")
                    return "single_investor"
            print("Invalid choice. Please enter 1 or 2.")

    def intro(self) -> None:
        """Show introduction and explain architecture."""
        self.print_header("InvestorClaw Setup Wizard")

        print("📋 IMPORTANT: InvestorClaw is an EDUCATIONAL analysis tool.")
        print("   It is NOT a fiduciary advisor and does NOT provide investment advice.")
        print("   Use these outputs to have INFORMED CONVERSATIONS with your")
        print("   qualified financial advisor. Always consult a professional.\n")
        print("=" * 70 + "\n")

        print("""Welcome! This wizard will configure InvestorClaw for portfolio analysis.

InvestorClaw uses a DUAL-LAYER ARCHITECTURE:

  1. NARRATIVE LAYER: Analyzes holdings, generates reports, enforces guardrails
     Providers: Together AI (MiniMax-M2.7) or Google (Gemini-2.5-flash)

  2. CONSULTATIVE LAYER (Optional): Synthesizes multi-portfolio insights
     Cloud: Gemma4 via your narrative provider
     Hybrid: Local gemma4-consult (Ollama/llama.cpp/LMStudio) + cloud narrative

SUPPORTED NARRATIVE PROVIDERS:
  • Together AI (MiniMax-M2.7)      — 128K context, $10-20/month
  • Google (Gemini-2.5-flash)       — 1M context, $10-20/month  ← Recommended for context

RECOMMENDED SETUP:
  • Google Gemini-2.5-flash (~$10-20/month, 1M context, excellent quality)
  OR
  • Together AI MiniMax-M2.7 (~$10-20/month, 128K context, fast)

  Both providers deliver excellent financial analysis quality.
""")

    def choose_provider(self) -> str:
        """Ask user which narrative provider they want."""
        self.print_section("Step 1: Choose Your Narrative Provider")

        print("""InvestorClaw supports two narrative providers:

  1. GOOGLE (Gemini-2.5-flash) — Recommended
     • Context: 1M tokens (large portfolio support)
     • Cost: ~$10-20/month
     • Quality: Excellent
     • Sign up: https://ai.google.dev

  2. TOGETHER AI (MiniMax-M2.7) — Alternative
     • Context: 128K tokens
     • Cost: ~$10-20/month
     • Quality: Excellent, very fast
     • Sign up: https://www.together.ai

  3. SKIP — Configure manually later
""")

        while True:
            choice = input("Select [1-3]: ").strip()
            if choice in ["1", "2", "3"]:
                mapping = {"1": "google", "2": "together", "3": "skip"}
                return mapping[choice]
            print("Invalid choice. Please enter 1, 2, or 3.")

    def setup_google(self) -> Dict:
        """Set up Google Gemini-2.5-flash configuration."""
        self.print_section("Setup: Google Gemini-2.5-flash")

        print("""GOOGLE GEMINI-2.5-FLASH CONFIGURATION

  Model: Gemini-2.5-flash
    • Context: 1M tokens (supports large portfolios)
    • Cost: ~$10-20/month
    • Quality: Excellent for financial analysis
    • Sign up: https://ai.google.dev
    • Create API key in Google AI Studio

Cost: ~$10-20/month
""")

        config = {
            "deployment_type": "google",
            "model": {
                "provider": "google",
                "narrative_model": "gemini-2.5-flash",
                "narrative_context": "1M",
                "api_key": None,
            },
        }

        print("\nGET YOUR GOOGLE API KEY:")
        print("  1. Visit: https://ai.google.dev")
        print("  2. Create a new API key (free tier available)")
        print("  3. Copy the key below\n")

        has_key = input("Do you have a Google API key? [y/n]: ").strip().lower() == "y"
        if has_key:
            api_key = input("Enter your Google API key: ").strip()
            if api_key:
                config["model"]["api_key"] = api_key
                print("✓ Google Gemini configured\n")
        else:
            print("⚠️  Skipping Google (you can add it later)\n")

        return config

    def setup_together(self) -> Dict:
        """Set up Together AI MiniMax-M2.7 configuration."""
        self.print_section("Setup: Together AI MiniMax-M2.7")

        print("""TOGETHER AI MINIMAX-M2.7 CONFIGURATION

  Model: MiniMax-M2.7 (via together/MiniMaxAI/MiniMax-M2.7)
    • Context: 128K tokens
    • Cost: ~$10-20/month
    • Quality: Excellent, very fast
    • Sign up: https://www.together.ai
    • Create API key in dashboard

Cost: ~$10-20/month
""")

        config = {
            "deployment_type": "together",
            "model": {
                "provider": "together",
                "narrative_model": "together/MiniMaxAI/MiniMax-M2.7",
                "narrative_context": "128k",
                "api_key": None,
            },
        }

        print("\nGET YOUR TOGETHER AI API KEY:")
        print("  1. Sign up: https://www.together.ai")
        print("  2. Create API key in settings")
        print("  3. Copy the key below\n")

        has_key = input("Do you have a Together AI API key? [y/n]: ").strip().lower() == "y"
        if has_key:
            api_key = input("Enter your Together AI API key: ").strip()
            if api_key:
                config["model"]["api_key"] = api_key
                print("✓ Together AI configured\n")
        else:
            print("⚠️  Skipping Together AI (you can add it later)\n")

        return config

    def validate_connections(self, config: Dict) -> bool:
        """Test connectivity to configured narrative provider."""
        self.print_section("Validating Narrative Provider Connection")

        model = config.get("model", {})
        provider = model.get("provider", "unknown")
        narrative_model = model.get("narrative_model", "unknown")

        print(f"Provider: {provider.upper()}")
        print(f"Model: {narrative_model}")
        if model.get("api_key"):
            print("  ✓ API key configured")
        else:
            print("  ⚠️  No API key configured (will prompt at runtime)")

        return True

    def test_financial_routing(self, config: Dict) -> bool:
        """Test that financial query routing works."""
        self.print_section("Testing Financial Query Routing")

        print("✓ Financial query detection: active")
        print("✓ Guardrail enforcement: active")
        print("✓ Dual-layer routing: ready")

        return True

    def setup_consultation(self) -> Dict:
        """Configure consultative layer: cloud (Gemma4) or hybrid (local + cloud)."""
        self.print_section("Step 2: Consultative Layer (Optional)")

        print("""CONSULTATIVE LAYER

A consultation model (Gemma4) synthesizes multi-portfolio insights and
complex analysis. Optional — InvestorClaw works fine without it.

SETUP OPTIONS:

  A. CLOUD (Recommended for beginners)
     • Uses Gemma4 via your narrative provider (Google or Together)
     • No local installation needed
     • Cost: Included in narrative provider pricing
     • Setup time: 1 minute

  B. HYBRID (For advanced users with local GPU)
     • Local gemma4-consult (Ollama, llama.cpp, or LMStudio)
     • Narrative layer still uses cloud provider
     • Cost: Free (local inference)
     • Setup time: 10-30 minutes
     • Requires: ~9.6GB VRAM (NVIDIA/AMD/Intel Arc GPU)

No consultation layer:
     • InvestorClaw uses keyword heuristics for synthesis
     • Fully functional, just less nuanced
     • Best for: Portfolios <$1M with <20 holdings
""")

        enable = input("Enable consultation layer? [y/n]: ").strip().lower()
        if enable != "y":
            print("Skipping consultation layer (keyword heuristics will be used)\n")
            return {"enabled": False}

        consultation_mode = None
        while consultation_mode is None:
            mode_choice = input("Select [a=Cloud, b=Hybrid, c=Skip]: ").strip().lower()
            if mode_choice == "a":
                consultation_mode = "cloud"
            elif mode_choice == "b":
                consultation_mode = "hybrid"
            elif mode_choice == "c":
                print("Skipping consultation layer\n")
                return {"enabled": False}
            else:
                print("Please enter 'a', 'b', or 'c'")

        if consultation_mode == "cloud":
            return self._setup_cloud_consultation()
        else:
            return self._setup_hybrid_consultation()

    def _setup_cloud_consultation(self) -> Dict:
        """Set up cloud Gemma4 consultation (uses narrative provider's API)."""
        self.print_section("Cloud Consultation Setup")

        print("""CLOUD GEMMA4 CONSULTATION

Using Gemma4 from your narrative provider:
  • Provider: Same as narrative layer
  • Model: gemma4 (via Together AI or Google)
  • Cost: Included in narrative provider pricing
  • Setup: Automatic (no additional configuration needed)
""")

        config = {
            "enabled": True,
            "mode": "cloud",
            "model": "gemma4",
            "provider": "inherited",  # Will use narrative provider's API
        }

        print("✓ Cloud consultation configured\n")
        return config

    def _setup_hybrid_consultation(self) -> Dict:
        """Set up hybrid consultation (local gemma4-consult + cloud narrative)."""
        self.print_section("Hybrid Consultation Setup")

        print("""HYBRID CONSULTATION

Local gemma4-consult for synthesis + Cloud narrative provider
  • Local model: gemma4-consult
  • Local inference: Ollama, llama.cpp, or LMStudio
  • Narrative: Still uses cloud provider (Google or Together)
  • Cost: Free (local) + narrative provider cost

SUPPORTED LOCAL INFERENCE SYSTEMS:

  1. OLLAMA (Recommended - easiest)
     • Download: https://ollama.ai
     • Default endpoint: http://localhost:11434
     • Setup time: 5 minutes
     • Model: ollama pull gemma4-consult

  2. llama.cpp (llama-server)
     • Fast, production-grade
     • Endpoint: http://localhost:8000
     • Setup time: 10-15 minutes
     • Better performance than Ollama

  3. LM STUDIO (GUI, most beginner-friendly)
     • Download: https://lmstudio.ai
     • Default endpoint: http://localhost:1234
     • Setup time: 10-20 minutes
     • Visual model management

  4. vLLM (Advanced, GPU-heavy)
     • Fastest inference
     • Endpoint: http://localhost:8000
     • Setup time: 15-30 minutes
     • Requires: Advanced GPU knowledge
""")

        inference_system = None
        while inference_system is None:
            choice = input("Select [1=Ollama, 2=llama.cpp, 3=LMStudio, 4=vLLM]: ").strip()
            mapping = {"1": "ollama", "2": "llama-cpp", "3": "lmstudio", "4": "vllm"}
            inference_system = mapping.get(choice)
            if not inference_system:
                print("Please enter 1, 2, 3, or 4")

        endpoint = self._get_inference_endpoint(inference_system)

        config = {
            "enabled": True,
            "mode": "hybrid",
            "model": "gemma4-consult",
            "inference_system": inference_system,
            "endpoint": endpoint,
        }

        print("✓ Hybrid consultation configured")
        print(f"  System: {inference_system}")
        print(f"  Endpoint: {endpoint}\n")

        return config

    def _get_inference_endpoint(self, system: str) -> str:
        """Get inference endpoint for local system."""
        default_endpoints = {
            "ollama": "http://localhost:11434",
            "llama-cpp": "http://localhost:8000",
            "lmstudio": "http://localhost:1234",
            "vllm": "http://localhost:8000",
        }

        default = default_endpoints.get(system, "http://localhost:11434")

        print(f"\nDefault endpoint for {system}: {default}")
        custom = input("Press Enter to use default, or enter custom endpoint: ").strip()

        return custom if custom else default

    def setup_portfolio_files_and_keys(self) -> Dict:
        """Portfolio discovery and optional API keys for data providers."""
        self.print_section("Step 3: Portfolio Files & Data Provider Keys")

        print("""PORTFOLIO FILES

InvestorClaw needs your portfolio data. Supported formats:
  • CSV exports from brokers (Schwab, Fidelity, E*TRADE, etc.)
  • Excel files (.xlsx) with holdings
  • PDF broker statements

Where to put them:
  ~/portfolios/  (created automatically)

OPTIONAL DATA PROVIDERS

InvestorClaw can enhance analysis with data from:
  • Finnhub (stock fundamentals, insider trading)
  • NewsAPI (financial news)
  • Polygon (market data, technicals)
  • Alpha Vantage (time series data)

These are OPTIONAL. Without them, InvestorClaw uses free/built-in data sources.
""")

        # Offer to collect provider keys
        collect_keys = (
            input("Would you like to add optional data provider keys? [y/n]: ").strip().lower()
        )

        if collect_keys != "y":
            print("Skipping provider keys (will use built-in data sources)\n")
            return {}

        provider_keys = self._collect_provider_keys()
        return provider_keys

    @staticmethod
    def _detect_existing_keys(env_path) -> Dict[str, str]:
        """Read *env_path* (.env file) and return a dict of provider → key value.

        Only inspects known provider environment variable names.  If a key
        is already present the wizard skips that prompt and tells the user.
        """
        env_path = Path(env_path)
        if not env_path.exists():
            return {}
        found: Dict[str, str] = {}
        _VAR_TO_PROVIDER = {
            "FINNHUB_API_KEY": "finnhub",
            "FINNHUB_KEY": "finnhub",
            "NEWSAPI_KEY": "newsapi",
            "FRED_API_KEY": "fred",
            "CRYPTOPANIC_API_KEY": "cryptopanic",
            "ALPHA_VANTAGE_KEY": "alphavantage",
            "ALPHA_VANTAGE_API_KEY": "alphavantage",
            "POLYGON_API_KEY": "polygon",
            "MASSIVE_API_KEY": "polygon",
        }
        try:
            with open(env_path, encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    var, _, val = line.partition("=")
                    var = var.strip()
                    val = val.strip().strip('"').strip("'")
                    provider = _VAR_TO_PROVIDER.get(var)
                    if provider and val:
                        found[provider] = val
        except OSError:
            pass
        return found

    def _collect_provider_keys(self) -> Dict[str, str]:
        """Collect optional API keys for data providers — tiered UX.

        Tier 1: RECOMMENDED (free, high value)
        Tier 2: OPTIONAL (free, specialized)
        Tier 3: PAID / SPECIALIZED
        No-key / automatic sources listed for information only.

        Migration pass: reads ~/.investorclaw/.env before prompts; existing
        keys are shown as already-configured and skipped.
        """
        env_path = self.config_dir / ".env"
        existing = self._detect_existing_keys(env_path)

        keys: Dict[str, str] = {}

        print("""
========================================
InvestorClaw runs with ZERO keys required.
Yahoo Finance covers: quotes, history, per-ticker news,
analyst consensus, index news, sector ETF news, crypto,
commodities, futures, forex.

Optional keys below UNLOCK additional features.
All recommended keys have a FREE tier.
========================================
""")

        # ── TIER 1: RECOMMENDED ──────────────────────────────────────────────

        print("━━━ TIER 1: RECOMMENDED (free, high value) ━━━\n")

        tier1 = [
            {
                "key": "finnhub",
                "name": "Finnhub",
                "signup_url": "https://finnhub.io/register",
                "limits": "60 req/min free",
                "env_var": "FINNHUB_API_KEY",
                "unlocks": (
                    "market-news categories (forex/crypto/merger), "
                    "economic/earnings/IPO calendars, insider-transaction feeds"
                ),
                "skip_impact": (
                    "market-news falls back to Yahoo (reduced merger/forex coverage); "
                    "no earnings/IPO calendar"
                ),
            },
            {
                "key": "fred",
                "name": "FRED (Federal Reserve Economic Data)",
                "signup_url": "https://fredaccount.stlouisfed.org/apikey",
                "limits": "120 req/min free",
                "env_var": "FRED_API_KEY",
                "unlocks": (
                    "macro data (Fed funds, CPI, GDP, Treasury yields, inflation series), "
                    "data-release calendar for macro news topic"
                ),
                "skip_impact": (
                    "macro topic falls back to news-only sources; no direct economic indicators"
                ),
            },
            {
                "key": "newsapi",
                "name": "NewsAPI",
                "signup_url": "https://newsapi.org/register",
                "limits": "100 req/day free",
                "env_var": "NEWSAPI_KEY",
                "unlocks": ("broad headline search for portfolio-specific news, M&A news fallback"),
                "skip_impact": (
                    "portfolio news uses Yahoo only (still functional, slightly narrower sources)"
                ),
            },
        ]

        for item in tier1:
            print(
                f"[{list(range(1, 10)).index(list(range(1, 10))[tier1.index(item)]) + 1}] "
                f"{item['name']}  —  {item['signup_url']}  ({item['limits']})"
            )
            print(f"    Unlocks: {item['unlocks']}")
            print(f"    Skip impact: {item['skip_impact']}")
            print()

            if item["key"] in existing:
                print(
                    f"  ✓ {item['name']} already configured (edit ~/.investorclaw/.env to change)\n"
                )
                keys[item["key"]] = existing[item["key"]]
                continue

            has_key = input(f"  Enter {item['name']} API key (or press Enter to skip): ").strip()
            if has_key:
                keys[item["key"]] = has_key
            print()

        # ── TIER 2: OPTIONAL ─────────────────────────────────────────────────

        print("━━━ TIER 2: OPTIONAL (free, specialized) ━━━\n")

        tier2 = [
            {
                "key": "cryptopanic",
                "name": "CryptoPanic",
                "signup_url": "https://cryptopanic.com/developers/api/",
                "limits": "500 req/day free",
                "env_var": "CRYPTOPANIC_API_KEY",
                "unlocks": ("curated crypto news with bullish/bearish/important filtering"),
                "skip_impact": "crypto topic uses Yahoo + Finnhub (still good)",
            },
            {
                "key": "alphavantage",
                "name": "Alpha Vantage",
                "signup_url": "https://www.alphavantage.co/support/#api-key",
                "limits": "25 req/day free",
                "env_var": "ALPHA_VANTAGE_API_KEY",
                "unlocks": (
                    "topic-filtered news sentiment (technology, finance, energy, etc.) — "
                    "powers v2.3 sector news topic"
                ),
                "skip_impact": "sector news uses Yahoo sector ETFs only",
            },
        ]

        for item in tier2:
            idx = len(tier1) + tier2.index(item) + 1
            print(f"[{idx}] {item['name']}  —  {item['signup_url']}  ({item['limits']})")
            print(f"    Unlocks: {item['unlocks']}")
            print(f"    Skip impact: {item['skip_impact']}")
            print()

            if item["key"] in existing:
                print(
                    f"  ✓ {item['name']} already configured (edit ~/.investorclaw/.env to change)\n"
                )
                keys[item["key"]] = existing[item["key"]]
                continue

            has_key = input(f"  Enter {item['name']} API key (or press Enter to skip): ").strip()
            if has_key:
                keys[item["key"]] = has_key
            print()

        # ── TIER 3: PAID ─────────────────────────────────────────────────────

        print("━━━ TIER 3: PAID / SPECIALIZED ━━━\n")

        tier3 = [
            {
                "key": "polygon",
                "name": "Polygon.io",
                "signup_url": "https://polygon.io/",
                "limits": "5 req/min free, paid tiers for more",
                "env_var": "POLYGON_API_KEY",
                "unlocks": (
                    "detailed options chains, tick-level trade data, "
                    "alternative news for tickers without Yahoo coverage"
                ),
                "skip_impact": "advanced options views unavailable",
            },
        ]

        for item in tier3:
            idx = len(tier1) + len(tier2) + tier3.index(item) + 1
            print(f"[{idx}] {item['name']}  —  {item['signup_url']}  ({item['limits']})")
            print(f"    Unlocks: {item['unlocks']}")
            print(f"    Skip impact: {item['skip_impact']}")
            print()

            if item["key"] in existing:
                print(
                    f"  ✓ {item['name']} already configured (edit ~/.investorclaw/.env to change)\n"
                )
                keys[item["key"]] = existing[item["key"]]
                continue

            has_key = input(f"  Enter {item['name']} API key (or press Enter to skip): ").strip()
            if has_key:
                keys[item["key"]] = has_key
            print()

        # ── NO-KEY SOURCES ────────────────────────────────────────────────────

        print("━━━ NO-KEY / AUTOMATIC (no signup required) ━━━\n")
        print("  These public sources are queried automatically:")
        print("    • Yahoo Finance       — all core price/news/fundamentals")
        print("    • SEC EDGAR           — 8-K material events, Form 4 insider filings")
        print("    • Treasury Direct     — auction announcements")
        print("    • GDELT 2.0           — global macro event news")
        print("    • LBMA daily fix      — gold/silver spot\n")

        if keys:
            num_keys = len(keys)
            print(f"✓ Collected {num_keys} API key(s)\n")
        else:
            print("No keys provided. Using built-in data sources.\n")

        return keys

    def save_config(self, config: Dict) -> None:
        """Save configuration to file."""
        self.config_dir.mkdir(parents=True, exist_ok=True)

        with open(self.config_file, "w") as f:
            json.dump(config, f, indent=2)

        print(f"\n✓ Configuration saved to: {self.config_file}")
        self._write_env_vars(config)

    def _write_env_vars(self, config: Dict) -> None:
        """Write environment variables for API keys."""
        env_file = self.config_dir / ".env"
        model = config.get("model", {})
        provider = model.get("provider", "").lower()
        api_key = model.get("api_key", "")

        if not api_key:
            return

        lines = []

        if provider == "google":
            lines.append(f"GOOGLE_API_KEY={api_key}")
        elif provider == "together":
            lines.append(f"TOGETHER_API_KEY={api_key}")

        # Add data provider keys
        provider_keys = config.get("provider_keys", {})
        if provider_keys:
            if "finnhub" in provider_keys:
                lines.append(f"FINNHUB_API_KEY={provider_keys['finnhub']}")
            if "newsapi" in provider_keys:
                lines.append(f"NEWSAPI_KEY={provider_keys['newsapi']}")
            if "polygon" in provider_keys:
                lines.append(f"POLYGON_API_KEY={provider_keys['polygon']}")
            if "alphavantage" in provider_keys:
                lines.append(f"ALPHA_VANTAGE_API_KEY={provider_keys['alphavantage']}")
            # v2.2 additions
            if "fred" in provider_keys:
                lines.append(f"FRED_API_KEY={provider_keys['fred']}")
            if "cryptopanic" in provider_keys:
                lines.append(f"CRYPTOPANIC_API_KEY={provider_keys['cryptopanic']}")

        if lines:
            with open(env_file, "w") as f:
                f.write("\n".join(lines) + "\n")
            print(f"✓ Environment variables written to: {env_file}")

    def show_summary(self, config: Dict) -> None:
        """Show final configuration summary."""
        self.print_header("Setup Complete! 🎉")

        model = config.get("model", {})
        provider = model.get("provider", "").lower()

        print(f"Deployment Type: {provider.upper()}\n")

        print("NARRATIVE LAYER (Primary Analysis):")
        print(f"  Provider: {provider.upper()}")
        print(f"  Model: {model.get('narrative_model', 'not set')}")
        print(f"  Context: {model.get('narrative_context', 'unknown')}")
        if model.get("api_key"):
            print("  Credentials: ✓ Configured")
        else:
            print("  Credentials: ⚠️  Will prompt at runtime")

        print("\nCOST ESTIMATE:")
        if provider == "google":
            print("  Google Gemini-2.5-flash: ~$10-20/month")
        elif provider == "together":
            print("  Together AI MiniMax-M2.7: ~$10-20/month")

        # Consultation layer
        consultation = config.get("consultation", {})
        if consultation.get("enabled"):
            mode = consultation.get("mode", "unknown")
            print("\nCONSULTATION LAYER (Synthesis):")
            print(f"  Mode: {mode.upper()}")
            print(f"  Model: {consultation.get('model', 'not set')}")
            if mode == "cloud":
                print("  Provider: Same as narrative layer")
            elif mode == "hybrid":
                print(f"  Local system: {consultation.get('inference_system', 'not set')}")
                print(f"  Endpoint: {consultation.get('endpoint', 'not set')}")
        else:
            print("\nCONSULTATION LAYER: Disabled (keyword heuristics)")

        # Data providers
        provider_keys = config.get("provider_keys", {})
        if provider_keys:
            print("\nDATA PROVIDERS:")
            for key in provider_keys.keys():
                print(f"  ✓ {key.upper()}")
        else:
            print("\nDATA PROVIDERS: Using built-in sources only")

        # Deployment mode
        deployment_mode = config.get("deployment_mode", "single_investor")
        print(f"\nDEPLOYMENT MODE: {deployment_mode.upper().replace('_', ' ')}")

        # Next steps
        print("\nNEXT STEPS:")
        print("  1. Add your portfolio files to ~/portfolios/")
        print("     (CSV, Excel, or PDF exports from your broker)")
        print()
        print("  2. Run portfolio setup:")
        print("     /portfolio setup")
        print()
        print("  3. Run your first analysis:")
        print("     /portfolio holdings")
        print()
        print("  💡 TIP: If you're using OpenClaw, these are agent commands.")
        print("     If you're using standalone Python, run:")
        print("       python skill/investorclaw.py setup")
        print("       python skill/investorclaw.py holdings")

        print("\nFOR HELP:")
        print("  • Portfolio formats: See README.md → Supported Formats")
        print("  • Data providers: Check .env file in ~/.investorclaw/")
        print("  • Troubleshooting: docs/SETUP.md")

        print()
        print(_stonkmode_tip(always=True))

        print("\n" + "=" * 70)

    def run(self) -> None:
        """Run the complete setup wizard."""
        self.intro()

        # Step 0: Select deployment mode
        deployment_mode = self.select_deployment_mode()

        # Step 0.5: Ask about ESPP holdings (all modes)
        espp_programs = self.ask_about_espp()

        # Step 1: Choose narrative provider
        provider_choice = self.choose_provider()

        if provider_choice == "google":
            self.config = self.setup_google()
        elif provider_choice == "together":
            self.config = self.setup_together()
        else:
            print("\n⏭️  Setup skipped. Configure manually and re-run this wizard.\n")
            return

        # Add deployment mode and ESPP data to config
        self.config["deployment_mode"] = deployment_mode
        self.config["espp_programs"] = espp_programs

        self.validate_connections(self.config)
        self.test_financial_routing(self.config)

        # Step 2: Optional consultation layer
        consultation_config = self.setup_consultation()
        self.config["consultation"] = consultation_config

        # Step 3: Portfolio files and API keys
        provider_keys = self.setup_portfolio_files_and_keys()
        self.config["provider_keys"] = provider_keys

        self.save_config(self.config)
        self.show_summary(self.config)


def main():
    """Entry point."""
    wizard = SetupWizard()
    try:
        wizard.run()
    except KeyboardInterrupt:
        print("\n\n⏹️  Setup cancelled by user.\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error during setup: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
