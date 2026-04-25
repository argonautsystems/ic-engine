# portfolio llm-config (OpenClaw)

**Configure LLM provider for optional consultative analysis.**

## What It Does

Interactive setup for optional verification and synthesis features. Choose from:
- **Local Ollama** — free, requires GPU (24GB+ VRAM)
- **Local llama-server** — recommended if you have GPU (131K context)
- **NVIDIA NGC** — enterprise option for NVIDIA employees
- **Custom endpoint** — your own inference cluster
- **Together.ai** — free cloud tier (100 calls/month)
- **Skip** — basic analysis only (no consultation needed)

## Usage

```bash
/portfolio llm-config
```

Interactive setup wizard. Detects local Ollama/llama-server, probes endpoints, saves to `~/.investorclaw/.env`.

## Quick Decision Tree

| Your Situation | Recommended |
|---|---|
| Have a local GPU (24GB+) | Local Ollama or llama-server |
| Work at NVIDIA | NGC or custom cluster |
| No GPU, want cloud | Together.ai (free tier) |
| Just need basic analysis | Skip (no setup needed) |

## Provider Comparison

| Provider | Speed | Cost | GPU Required | Notes |
|---|---|---|---|---|
| **Local Ollama** | ~30 tok/s | $0 | ✅ Yes (24GB) | Data stays local, slowest |
| **llama-server** | ~73 tok/s | $0 | ✅ Yes (24GB) | Recommended if you have GPU, 131K context |
| **NGC** | 100+ tok/s | NVIDIA pay | ❌ No | Enterprise, NVIDIA only |
| **Custom** | Varies | Varies | Varies | Full control, requires setup |
| **Together.ai** | 100+ tok/s | Free tier / paid | ❌ No | Instant setup, easiest for most users |
| **Skip** | N/A | N/A | N/A | Basic analysis works fine without it |

## Example

```bash
$ /portfolio llm-config

============================================================
InvestorClaw Consultation Setup (OpenClaw)
============================================================

Choose a provider for consultative LLM (optional verification & synthesis):

  0. Skip (basic analysis only, no consultation)
  1. Local Ollama (free, 24GB+ VRAM required)
  2. Local llama-server (recommended if you have GPU)
  3. NVIDIA NGC (enterprise, NVIDIA employees only)
  4. Custom endpoint (your own cluster)
  5. Together.ai (free tier available, no GPU needed)

Select option (0-5): 5

☁️  Together.ai Setup
----------------------------------------

Together.ai offers free tier (100+ calls/month) and paid options.
Sign up at: https://www.together.ai

Recommended model: google/gemma-4-31B-it

Together.ai API key: your-api-key-here
Model name (default: google/gemma-4-31B-it): 

✅ Configuration saved to /Users/jasonperlow/.investorclaw/.env
```

## After Setup

Configuration is saved to `~/.investorclaw/.env`:

```bash
INVESTORCLAW_CONSULTATION_ENABLED=true
INVESTORCLAW_CONSULTATION_ENDPOINT=https://api.together.xyz/v1
INVESTORCLAW_CONSULTATION_API_KEY=your-api-key
INVESTORCLAW_CONSULTATION_MODEL=google/gemma-4-31B-it
```

Now consultative features (anti-fabrication analysis, synthesis enrichment) will use your configured provider.

## Troubleshooting

**Connection refused on localhost:**
```bash
# Check Ollama is running
ollama serve &

# Check port
lsof -i :11434  # Ollama
lsof -i :8080   # llama-server
```

**401 Unauthorized on cloud provider:**
```bash
# Verify API key is set (do NOT echo it — keeps key out of history)
test -n "$INVESTORCLAW_CONSULTATION_API_KEY" && echo "API key is set" || echo "API key is missing"

# Check endpoint format
# Together.ai: https://api.together.xyz/v1
# xAI: https://api.x.ai/v1
# OpenAI: https://api.openai.com/v1
```

**Model not found:**
```bash
# For local Ollama
ollama list

# For Together.ai, use full model ID
# NOT: google/gemma-4
# USE: google/gemma-4-31B-it
```

## Is It Required?

No. Basic portfolio analysis works without any consultation setup. This is optional for users who want:
- Anti-fabrication verification
- Synthesis enrichment
- Key-insight generation

## See Also

- [docs/DATA_FLOW.md](../SECURITY.md) — Data flow and provider trust model
- [config/model-recommendations.yaml](../claude/config/model-recommendations.yaml) — Model selection guide
- [config/llm-providers.yaml](../claude/config/llm-providers.yaml) — Provider endpoints
